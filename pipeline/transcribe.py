"""M4 step 1: transcribe each candidate window with Whisper (word timestamps)
and build a compact review packet for ranking.

Run:
  python pipeline/transcribe.py --campaign asianjeff --vod-id 2933231152

Backend: mlx-whisper on Apple Silicon, faster-whisper elsewhere (see pipeline/tools.py).
Reads   data/<campaign>/<vod_id>/candidates.json, vod.mp4, chat.json
Writes  transcripts.json  (words with absolute VOD timestamps, per candidate)
        rank_packet.md    (transcript + chat per candidate, for the ranker)
Already-transcribed candidates are skipped, so it is safe to rerun.
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.tools import FFMPEG, ROOT, WHISPER_BACKEND, WHISPER_MODEL  # noqa: E402

# Transcribe a bit wider than the candidate window so the ranker can move the
# clip boundaries (clips may be 15 to 60 s).
PAD_BEFORE_S = 10
PAD_AFTER_S = 20


def fmt(t):
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


_FW = None


def _run_whisper(wav):
    """Segments as [{start, end, text, words: [{word, start, end}]}] from either backend."""
    global _FW
    if WHISPER_BACKEND == "mlx":
        import mlx_whisper
        r = mlx_whisper.transcribe(str(wav), path_or_hf_repo=WHISPER_MODEL, word_timestamps=True,
                                   language="en", condition_on_previous_text=False)
        return r["segments"]
    from faster_whisper import WhisperModel
    if _FW is None:
        _FW = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    segments, _ = _FW.transcribe(str(wav), language="en", word_timestamps=True,
                                 condition_on_previous_text=False)
    return [{"start": s.start, "end": s.end, "text": s.text,
             "words": [{"word": w.word, "start": w.start, "end": w.end} for w in (s.words or [])]}
            for s in segments]


def transcribe_window(vod, start, end):
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "a.wav"
        subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", str(start), "-t", str(end - start),
                        "-i", str(vod), "-vn", "-ac", "1", "-ar", "16000", str(wav)], check=True)
        raw = _run_whisper(wav)
    segs = []
    for s in raw:
        words = [{"w": w["word"].strip(), "s": round(start + w["start"], 2), "e": round(start + w["end"], 2)}
                 for w in s.get("words", [])]
        segs.append({"s": round(start + s["start"], 2), "e": round(start + s["end"], 2),
                     "text": s["text"].strip(), "words": words})
    return segs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--vod-id", required=True)
    args = ap.parse_args()
    d = ROOT / "data" / args.campaign / args.vod_id
    cands = json.load(open(d / "candidates.json"))
    dur = cands["duration_s"]
    out_path = d / "transcripts.json"
    done = json.load(open(out_path)) if out_path.exists() else {}
    print(f"Whisper backend: {WHISPER_BACKEND} ({WHISPER_MODEL})", flush=True)

    for c in cands["candidates"]:
        key = str(c["peak"])
        if key in done:
            continue
        s, e = max(0, c["start"] - PAD_BEFORE_S), min(dur, c["end"] + PAD_AFTER_S)
        print(f"#{c['rank']:>2} {fmt(s)}-{fmt(e)} transcribing...", flush=True)
        done[key] = {"rank": c["rank"], "t_start": s, "t_end": e, "segments": transcribe_window(d / "vod.mp4", s, e)}
        out_path.write_text(json.dumps(done, indent=1, ensure_ascii=False))

    # Review packet: one block per candidate, transcript lines then chat.
    lines = [f"# Rank packet: {args.campaign} VOD {args.vod_id}\n",
             "Transcript is the mixed stream audio: AsianJeff's voice AND game dialogue, unlabelled.\n"]
    for c in cands["candidates"]:
        t = done[str(c["peak"])]
        lines.append(f"\n## #{c['rank']}  window {c['start_hms']}-{c['end_hms']}  peak {c['peak_hms']}  "
                     f"(transcribed {fmt(t['t_start'])}-{fmt(t['t_end'])})  "
                     f"score {c['score']} chat {c['chat_z']} audio {c['audio_z']} cutscene {c['cutscene_frac']}")
        lines.append("### Transcript")
        for s in t["segments"]:
            lines.append(f"[{fmt(s['s'])}] {s['text']}")
        lines.append("### Chat")
        lines.extend(c["chat"])
    (d / "rank_packet.md").write_text("\n".join(lines) + "\n")
    print(f"Wrote {out_path} and {d / 'rank_packet.md'}")


if __name__ == "__main__":
    main()
