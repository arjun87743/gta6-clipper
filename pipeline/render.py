"""M5: render kept clips from rankings.json as 1080x1920 vertical videos.

Run:
  python pipeline/render.py --campaign asianjeff --vod-id 2933231152            # all kept clips
  python pipeline/render.py --campaign asianjeff --vod-id 2933231152 --only 12  # one clip
  python pipeline/render.py --campaign asianjeff --vod-id 2933231152 --edit     # M6 edited versions
      ... --edit --without sfx outro                                           # A/B: layers switched off

Plain renders go to output/rendered/, edited ones to output/edited/ (see pipeline/edit_layer.py).

For each kept clip:
  1. Download just that section at 1080p60 (cached in data/<c>/<id>/segments/), and check its
     timing against the full VOD by matching audio, so captions stay in sync.
  2. Work out which facecam box the streamer is using in this clip (campaign config lists them).
  3. Render: facecam + gameplay (crop avoids the cam and the chat overlay), hook text,
     word-highlight captions (profanity masked), "Ad" label from frame 0, loudness to -14 LUFS.
  4. Write output/rendered/<name>.mp4 and <name>.json (what was used, for review and tracking).

Layouts, caption styles and hook styles rotate across clips (hard rule 10).
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import rankings  # noqa: E402
from pipeline.tools import FFMPEG, FFPROBE, YTDLP, ROOT, font_family, font_file  # noqa: E402

W, H = 1080, 1920
FPS = 30
DECISIONS_PATH = ROOT / "review" / "decisions.json"
DECISIONS = json.load(open(DECISIONS_PATH)) if DECISIONS_PATH.exists() else {}
SEG_PAD = 3.0  # seconds downloaded either side of the clip (widened automatically if needed)

LAYOUTS = ["cam_top", "game_top", "game_top_tall"]  # all stacked: facecam never covers gameplay
CAPTION_STYLES = {
    # name: (font family, size, highlight colour in ASS BGR, uppercase)
    "bold_yellow": (font_family("Arial Black"), 74, "&H0000FFFF&", True),
    "impact_green": (font_family("Impact"), 92, "&H0000FF66&", True),
    "futura_cyan": (font_family("Futura"), 80, "&H00FFD000&", False),
}

PROFANITY = {
    r"\bmotherfuck": "motherf*ck", r"\bfuck": "f*ck", r"\bshit": "sh*t", r"\bbitch": "b*tch",
    r"\bnigg\w*": "***", r"\bcunt": "c*nt", r"\bdick": "d*ck", r"\bpussy": "p*ssy",
}


# ---------- small helpers ----------

def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed:\n{p.stderr[-2000:]}")
    return p.stdout


def pcm(path, ss=None, t=None, sr=8000):
    cmd = [FFMPEG, "-v", "error"] + (["-ss", str(ss)] if ss is not None else []) + ["-i", str(path)]
    cmd += (["-t", str(t)] if t else []) + ["-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    return np.frombuffer(subprocess.run(cmd, capture_output=True).stdout, dtype=np.int16).astype(np.float32)


def scale_box(b, s):
    return {k: int(round(v * s)) for k, v in b.items()}


def mask(text):
    for pat, rep in PROFANITY.items():
        text = re.sub(pat, rep, text, flags=re.I)
    return text


def wrap_balanced(text, max_width=18):
    """Wrap into as few lines as max_width allows, with similar line lengths (no orphan words)."""
    import math
    n = max(1, math.ceil(len(text) / max_width))
    for width in range(math.ceil(len(text) / n), len(text) + 1):
        lines = wrap(text, width)
        if len(lines) <= n:
            return lines
    return [text]


def wrap(text, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


def ass_time(t):
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def rotation(idx):
    return LAYOUTS[idx % 3], list(CAPTION_STYLES)[(idx + idx // 3) % 3]  # 8 clips -> 8 different pairs


def hook_for(name, c):
    """A hook chosen or typed on the review page wins over the ranker's hook_text."""
    return (DECISIONS.get(name) or {}).get("hook") or rankings.edit_fields(c)[0]["hook_text"]


def platforms_for(cfg, c):
    skip = {"youtube_shorts"} if c.get("violence_early_risk") else set()
    return [p for p in cfg.get("platforms", []) if p not in skip]


# ---------- step 1: 1080p segment ----------

def _covers(t0, seg_dur, c):
    return t0 <= c["start"] - 0.05 and t0 + seg_dur >= c["end"] + 0.05


def fetch_segment(d, vod_id, c):
    """Download the clip's section at 1080p. Twitch cuts on keyframes, so the real start can
    drift by a second or two: measure it by audio matching and widen the margin if needed."""
    seg_dir = d / "segments"
    seg_dir.mkdir(exist_ok=True)
    out = seg_dir / f"cand{c['candidate_rank']:02d}.mp4"
    meta = seg_dir / f"cand{c['candidate_rank']:02d}.json"
    if meta.exists() and out.exists():
        m = json.load(open(meta))
        if _covers(m["t0"], m["dur"], c):
            return out, m["t0"]
    for pad in (SEG_PAD, SEG_PAD * 2, SEG_PAD * 4):
        s0, e0 = c["start"] - pad, c["end"] + pad
        print(f"  downloading 1080p section {s0:.1f}-{e0:.1f}s ...", flush=True)
        out.unlink(missing_ok=True)
        run([YTDLP, "-q", "-f", "1080p60/1080p/best", "--download-sections", f"*{s0}-{e0}",
             "--force-keyframes-at-cuts", "-o", str(out), f"https://www.twitch.tv/videos/{vod_id}"])
        seg = pcm(out)
        ref = pcm(d / "vod.mp4", s0 - 5, (e0 - s0) + 10)
        n = len(ref) + len(seg)
        corr = np.fft.irfft(np.fft.rfft(ref, n) * np.conj(np.fft.rfft(seg, n)), n)
        t0 = round(s0 - 5 + int(np.argmax(corr[: len(ref)])) / 8000, 3)
        seg_dur = len(seg) / 8000
        if _covers(t0, seg_dur, c):
            json.dump({"requested": [s0, e0], "t0": t0, "dur": seg_dur}, open(meta, "w"))
            return out, t0
        print(f"  segment {t0:.2f}-{t0 + seg_dur:.2f}s does not cover the clip, widening margin")
    raise RuntimeError(f"could not get a 1080p segment covering #{c['candidate_rank']}")


def probe_size(path):
    w, h = run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                "-of", "csv=p=0", str(path)]).strip().split(",")
    return int(w), int(h)


# ---------- step 2: which facecam box ----------

def detect_cam(seg, boxes, scale, n=6):
    """Pick the facecam box whose right and bottom edges are consistently sharp across frames.
    The cam border stays put while the game moves, so the real box edge has strong contrast."""
    dur = float(run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(seg)]))
    sw, sh = probe_size(seg)
    frames = []
    for i in range(n):
        t = dur * (i + 0.5) / n
        raw = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{t:.2f}", "-i", str(seg), "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True).stdout
        frames.append(np.frombuffer(raw, dtype=np.uint8).reshape(sh, sw).astype(np.float32))
    scores = {}
    for name, b in boxes.items():
        b = scale_box(b, scale)
        xr, yb = b["x"] + b["w"], b["y"] + b["h"]
        ys = slice(b["y"] + 10, yb - 10)
        xs = slice(b["x"] + 10, xr - 10)
        edge = []
        for f in frames:
            right = np.abs(f[ys, min(xr + 2, sw - 1)] - f[ys, max(xr - 3, 0)]).mean()
            bottom = np.abs(f[min(yb + 2, sh - 1), xs] - f[max(yb - 3, 0), xs]).mean()
            edge.append(right + bottom)
        scores[name] = float(np.median(edge))
    best = max(scores, key=scores.get)
    return best, scores


# ---------- step 3: geometry ----------

def cam_crop(box, aspect):
    """Crop the cam box (inset 2 px to drop its border) to the slot's aspect, centred."""
    x, y, w, h = box["x"] + 2, box["y"] + 2, box["w"] - 4, box["h"] - 4
    if w / h > aspect:
        nw = int(h * aspect) // 2 * 2
        x += (w - nw) // 2
        w = nw
    else:
        nh = int(w / aspect) // 2 * 2
        y += (h - nh) // 2
        h = nh
    return {"x": x, "y": y, "w": w // 2 * 2, "h": h // 2 * 2}


def game_crop(aspect, cam_right, chat_bottom, sw, sh):
    """Largest crop of the given aspect right of the facecam and below the chat overlay."""
    margin = 6
    x_min, y0 = cam_right + margin, chat_bottom + margin
    h = sh - y0
    w = aspect * h
    if w > sw - x_min:
        w = sw - x_min
        h = w / aspect
        y0 = y0 + (sh - y0 - h) / 2
    cx = sw / 2  # third-person camera keeps the character near screen centre
    x0 = min(max(cx - w / 2, x_min), sw - w)
    return {"x": int(x0) // 2 * 2, "y": int(y0) // 2 * 2, "w": int(w) // 2 * 2, "h": int(h) // 2 * 2}


def layout_plan(layout, cam_box, chat_bottom, sw, sh):
    """Return the ffmpeg filter for the picture plus where hook and captions go."""
    if layout == "cam_top":
        bar, cam_h = 260, 810
        game_h = H - bar - cam_h
        cc = cam_crop(cam_box, W / cam_h)
        gc = game_crop(W / game_h, cam_box["x"] + cam_box["w"], chat_bottom, sw, sh)
        f = (f"[0:v]split[a][b];"
             f"[a]crop={cc['w']}:{cc['h']}:{cc['x']}:{cc['y']},scale={W}:{cam_h}:flags=lanczos[cam];"
             f"[b]crop={gc['w']}:{gc['h']}:{gc['x']}:{gc['y']},scale={W}:{game_h}:flags=lanczos[game];"
             f"color=c=0x101014:s={W}x{bar}:r={FPS}[bar];"
             f"[bar][cam][game]vstack=inputs=3[pic]")
        return f, {"hook_y": 92, "hook_box": False, "cap_y": bar + cam_h, "ad": (24, 20)}, cc, gc
    # game_top and game_top_tall: gameplay on top, facecam strip below. Never overlap the two
    # (Arjun, 2026-11-20: an inset facecam over the gameplay covered the action).
    game_h = 1110 if layout == "game_top" else 1240
    cam_h = H - game_h
    cc = cam_crop(cam_box, W / cam_h)
    gc = game_crop(W / game_h, cam_box["x"] + cam_box["w"], chat_bottom, sw, sh)
    f = (f"[0:v]split[a][b];"
         f"[a]crop={cc['w']}:{cc['h']}:{cc['x']}:{cc['y']},scale={W}:{cam_h}:flags=lanczos[cam];"
         f"[b]crop={gc['w']}:{gc['h']}:{gc['x']}:{gc['y']},scale={W}:{game_h}:flags=lanczos[game];"
         f"[game][cam]vstack=inputs=2[pic]")
    return f, {"hook_y": 140, "hook_box": True, "cap_y": game_h, "ad": (30, 24)}, cc, gc


# ---------- captions ----------

def words_in(transcript_entry, s, e):
    ws = []
    for seg in transcript_entry["segments"]:
        for w in seg["words"]:
            if s - 0.05 <= w["s"] < e:
                txt = re.sub(r"[,.]+$", "", w["w"]).strip()
                if txt:
                    ws.append({"w": txt, "s": w["s"] - s, "e": min(w["e"], e) - s})
    return ws


def chunk_words(ws, max_words=3, max_gap=0.6, max_dur=1.6):
    chunks, cur = [], []
    for w in ws:
        if cur and (len(cur) >= max_words or w["s"] - cur[-1]["e"] > max_gap or w["e"] - cur[0]["s"] > max_dur):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def write_ass(path, ws, style_name, cap_y, dur):
    font, size, hl, upper = CAPTION_STYLES[style_name]
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,1,0,1,7,3,5,"
        f"60,60,0,1", "",
        "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    chunks = chunk_words(ws)
    for ci, ch in enumerate(chunks):
        chunk_end = ch[-1]["e"] + 0.35
        if ci + 1 < len(chunks):
            chunk_end = min(chunk_end, chunks[ci + 1][0]["s"])
        for wi, w in enumerate(ch):
            st = w["s"]
            en = ch[wi + 1]["s"] if wi + 1 < len(ch) else chunk_end
            if en <= st or st >= dur:
                continue
            parts = []
            for wj, x in enumerate(ch):
                t = mask(x["w"])
                t = t.upper() if upper else t
                t = t.replace("{", "(").replace("}", ")")
                parts.append(f"{{\\c{hl}}}{t}{{\\c&H00FFFFFF&}}" if wj == wi else t)
            lines.append(f"Dialogue: 0,{ass_time(st)},{ass_time(min(en, dur))},Cap,,0,0,0,,"
                         f"{{\\pos({W // 2},{cap_y})}}" + " ".join(parts))
    Path(path).write_text("\n".join(lines) + "\n")


# ---------- render ----------

def esc_path(p):
    return str(p).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def render_clip(d, cfg, r, c, idx, trans_by_rank, out_dir):
    rank = c["candidate_rank"]
    name = f"{r['campaign']}_{r['vod_id']}_c{rank:02d}"
    layout, style = rotation(idx)
    hook = hook_for(name, c)
    print(f"#{rank}: layout {layout}, captions {style}, hook {hook!r}")

    seg, t0 = fetch_segment(d, r["vod_id"], c)
    sw, sh = probe_size(seg)
    lay = cfg["layout"]
    scale = sw / lay["source_resolution"][0]
    cam_name, cam_scores = detect_cam(seg, lay["facecam"], scale)
    cam_box = scale_box(lay["facecam"][cam_name], scale)
    chat_bottom = int(round((lay["exclude"]["chat_overlay"]["y"] + lay["exclude"]["chat_overlay"]["h"]) * scale))
    print(f"  facecam: {cam_name} (edge scores {', '.join(f'{k} {v:.1f}' for k, v in cam_scores.items())})")

    pic, pos, cc, gc = layout_plan(layout, cam_box, chat_bottom, sw, sh)
    dur = round(c["end"] - c["start"], 2)
    ss = f"{c['start'] - t0:.3f}"
    # Loudness pass 1: measure, so pass 2 can hit -14 LUFS accurately even on quiet clips.
    m = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-ss", ss, "-i", str(seg), "-t", f"{dur}", "-vn",
                        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    ln = json.loads(m[m.rindex("{"):m.rindex("}") + 1])
    loudnorm = (f"loudnorm=I=-14:TP=-1.5:LRA=11:measured_I={ln['input_i']}:measured_TP={ln['input_tp']}:"
                f"measured_LRA={ln['input_lra']}:measured_thresh={ln['input_thresh']}:"
                f"offset={ln['target_offset']}:linear=true")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        ws = words_in(trans_by_rank[rank], c["start"], c["end"])
        write_ass(td / "cap.ass", ws, style, pos["cap_y"], dur)
        # Narrow wrap keeps the centred hook clear of the "Ad" label in the top-left corner.
        hook_lines = wrap_balanced(mask(hook).upper() if layout != "game_top_tall" else mask(hook), 18)
        (td / "hook.txt").write_text("\n".join(hook_lines))
        ad_x, ad_y = pos["ad"]
        bold = font_file("Arial Black")
        hook_font = font_file("Impact" if layout == "game_top" else "Arial Black")
        hook_draw = (f"drawtext=fontfile='{esc_path(hook_font)}':textfile='{esc_path(td / 'hook.txt')}':"
                     f"fontsize=60:line_spacing=10:text_align=C:fontcolor=white:x=(w-tw)/2:y={pos['hook_y']}:"
                     + ("box=1:boxcolor=black@0.72:boxborderw=22" if pos["hook_box"] else "borderw=0"))
        # Ad disclosure: large for the first 3 s, then a smaller label for the rest of the clip.
        ad_big = (f"drawtext=fontfile='{esc_path(bold)}':text='AD':fontsize=58:fontcolor=black:"
                  f"box=1:boxcolor=0xFFD400:boxborderw=14:x={ad_x}:y={ad_y}:enable='lt(t,3)'")
        ad_small = (f"drawtext=fontfile='{esc_path(bold)}':text='Ad':fontsize=38:fontcolor=black:"
                    f"box=1:boxcolor=0xFFD400@0.9:boxborderw=10:x={ad_x}:y={ad_y}:enable='gte(t,3)'")
        vf = (f"{pic};[pic]fps={FPS},subtitles='{esc_path(td / 'cap.ass')}',"
              f"{hook_draw},{ad_big},{ad_small},format=yuv420p[v]")
        out = out_dir / f"{name}.mp4"
        run([FFMPEG, "-v", "error", "-y", "-ss", ss, "-i", str(seg), "-t", f"{dur}",
             "-filter_complex", vf, "-map", "[v]", "-map", "0:a",
             "-af", loudnorm, "-ar", "48000",
             "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-profile:v", "high",
             "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)])

    platforms = platforms_for(cfg, c)
    meta = {
        "clip_id": name, "campaign": r["campaign"], "vod_id": r["vod_id"], "candidate_rank": rank,
        "vod_start": c["start"], "vod_end": c["end"], "duration_s": dur,
        "score": c["score"], "summary": c["summary"], "hook_used": hook, "hooks": c["hooks"],
        "layout": layout, "caption_style": style, "facecam_box": cam_name,
        "source": f"segments/{seg.name} ({sw}x{sh}), t0={t0}",
        "cam_crop": cc, "game_crop": gc,
        "violence_early_risk": c.get("violence_early_risk", False), "platforms_ok": platforms,
        "status": "rendered",
    }
    (out_dir / f"{name}.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    print(f"  -> {out}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--vod-id", required=True)
    ap.add_argument("--only", type=int, nargs="*", help="candidate ranks to render")
    ap.add_argument("--edit", action="store_true", help="render the M6 edited version into output/edited/")
    ap.add_argument("--without", nargs="+", default=[], choices=["hook", "punchline", "outro", "captions", "sfx"],
                    help="with --edit: switch these layers off")
    args = ap.parse_args()
    if args.without and not args.edit:
        ap.error("--without needs --edit")

    cfg = yaml.safe_load(open(ROOT / "campaigns" / f"{args.campaign}.yaml"))
    d = ROOT / "data" / args.campaign / args.vod_id
    r = json.load(open(d / "rankings.json"))
    trans = json.load(open(d / "transcripts.json"))
    trans_by_rank = {v["rank"]: v for v in trans.values()}
    out_dir = ROOT / "output" / ("edited" if args.edit else "rendered")
    out_dir.mkdir(parents=True, exist_ok=True)

    kept = sorted([c for c in r["clips"] if c.get("keep")], key=lambda c: -c["score"])
    for idx, c in enumerate(kept):  # idx fixes the layout/style rotation even with --only
        if args.only and c["candidate_rank"] not in args.only:
            continue
        if args.edit:
            from pipeline import edit_layer
            edit_layer.render_edited(d, cfg, r, c, idx, trans_by_rank, out_dir, args.without)
        else:
            render_clip(d, cfg, r, c, idx, trans_by_rank, out_dir)


if __name__ == "__main__":
    main()
