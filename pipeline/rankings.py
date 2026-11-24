"""M4 step 2: check rankings.json and make contact sheets of each kept clip.

Run:
  python pipeline/rankings.py --campaign asianjeff --vod-id 2933231152           # validate only
  python pipeline/rankings.py --campaign asianjeff --vod-id 2933231152 --sheets  # + contact sheets

Validation follows pipeline/rank_rules.md. Contact sheets (data/<c>/<id>/sheets/) show frames
at 0.5 s, 3.5 s and 6.5 s (the YouTube no-violence window), the middle and the end of each kept
clip, so a human or Claude can check the opening, the facecam and leftover cutscenes.
"""
import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.tools import FFMPEG, ROOT  # noqa: E402


def fmt(t):
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


MOODS = ("funny", "fail", "shock", "hype", "wholesome")
OUTROS = ("reaction_freeze", "reaction_replay", "reaction_zoom", "none")


def edit_fields(c):
    """The M6 edit fields with safe defaults: anything missing or invalid switches that effect
    off, it never stops a render. Times are absolute VOD seconds. Returns (fields, warnings)."""
    warns = []
    hooks = c.get("hooks") or [""]
    s, e = c["start"], c["end"]

    def text(key, limit, default):
        v = c.get(key)
        if isinstance(v, str) and 0 < len(v.strip()) <= limit:
            return v.strip()
        if v is not None:
            warns.append(f"{key} must be 1 to {limit} characters, using {default!r}")
        return default

    def time(key, default, first):
        v = c.get(key)
        if v is None:
            return default
        if type(v) in (int, float) and math.isfinite(v) and first <= v <= e - 0.3:
            return float(v)
        warns.append(f"{key} {v!r} must be between {first} and {e - 0.3:.1f}, ignored")
        return default

    def choice(key, options, default):
        v = c.get(key)
        if v in options:
            return v
        if v is not None:
            warns.append(f"{key} {v!r} is not one of {', '.join(options)}, using {default}")
        return default

    # No beat before 7 s on a violent opening, and never on top of the 1.5 s hook.
    punch = time("punchline_ts", None, s + (7 if c.get("violence_early_risk") else 1.5))
    react = time("reaction_ts", punch, s)
    mood = choice("mood", MOODS, None)
    outro = choice("outro_type", OUTROS, "none")
    if outro != "none" and react is None:
        warns.append("outro_type needs punchline_ts or reaction_ts, no outro")
        outro = "none"
    return {"hook_text": text("hook_text", 50, hooks[0]), "punchline_ts": punch, "reaction_ts": react,
            "mood": mood, "outro_type": outro, "outro_text": text("outro_text", 28, "")}, warns


def validate(r, cands, trans):
    errors, warns = [], []
    by_rank = {c["rank"]: c for c in cands["candidates"]}
    seen = set()
    for c in r["clips"]:
        k = c.get("candidate_rank")
        tag = f"#{k}"
        if k not in by_rank:
            errors.append(f"{tag}: no such candidate")
            continue
        seen.add(k)
        if not c.get("keep"):
            continue
        s, e = c["start"], c["end"]
        t = trans.get(str(by_rank[k]["peak"]))
        if t and not (t["t_start"] <= s < e <= t["t_end"]):
            errors.append(f"{tag}: {fmt(s)}-{fmt(e)} is outside its transcribed range "
                          f"{fmt(t['t_start'])}-{fmt(t['t_end'])}")
        if not 15 <= e - s <= 60:
            errors.append(f"{tag}: length {e - s:.1f}s, must be 15 to 60")
        if c.get("score", 0) <= 5:
            errors.append(f"{tag}: kept with score {c.get('score')} (rules say reject at 5 or below)")
        hooks = c.get("hooks") or []
        if not 2 <= len(hooks) <= 3:
            errors.append(f"{tag}: needs 2 to 3 hooks, has {len(hooks)}")
        for h in hooks:
            if len(h) > 50:
                warns.append(f"{tag}: hook over 50 chars: {h!r}")
        if c.get("spoiler_risk"):
            errors.append(f"{tag}: kept but marked spoiler_risk")
        warns += [f"{tag}: {w}" for w in edit_fields(c)[1]]
        for key in ("hook_text", "punchline_ts", "mood", "outro_type"):
            if c.get(key) is None:
                warns.append(f"{tag}: no {key}, the edit layer will use its safe default")
        if c.get("violence_early_risk"):
            warns.append(f"{tag}: violence_early_risk, check the first 7 s before posting to YouTube")
    missing = set(by_rank) - seen
    if missing:
        warns.append(f"candidates with no decision: {sorted(missing)}")
    return errors, warns


def sheet(vod, c, out):
    s, e = c["start"], c["end"]
    times = [s + 0.5, s + 3.5, s + 6.5, (s + e) / 2, e - 1]
    labels = ["+0.5s", "+3.5s", "+6.5s", "mid", "end"]
    inputs, filters = [], []
    for i, (t, lab) in enumerate(zip(times, labels)):
        inputs += ["-ss", f"{t:.2f}", "-i", str(vod)]
        filters.append(f"[{i}:v]scale=480:-2,drawtext=text='{lab}':x=10:y=10:fontsize=28:"
                       f"fontcolor=yellow:box=1:boxcolor=black@0.7[v{i}]")
    filters.append("".join(f"[v{i}]" for i in range(5)) + "hstack=inputs=5")
    subprocess.run([FFMPEG, "-v", "error", "-y", *inputs, "-filter_complex", ";".join(filters),
                    "-frames:v", "1", str(out)], check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--vod-id", required=True)
    ap.add_argument("--sheets", action="store_true")
    args = ap.parse_args()
    d = ROOT / "data" / args.campaign / args.vod_id
    r = json.load(open(d / "rankings.json"))
    cands = json.load(open(d / "candidates.json"))
    trans = json.load(open(d / "transcripts.json"))

    errors, warns = validate(r, cands, trans)
    kept = sorted([c for c in r["clips"] if c.get("keep")], key=lambda c: -c["score"])
    print(f"{len(kept)} kept, {len(r['clips']) - len(kept)} rejected\n")
    for c in kept:
        print(f"  #{c['candidate_rank']:>2} score {c['score']}  {fmt(c['start'])}-{fmt(c['end'])} "
              f"({c['end'] - c['start']:.0f}s)  {c['hooks'][0] if c.get('hooks') else ''}")
    for w in warns:
        print("WARN ", w)
    for e in errors:
        print("ERROR", e)

    if args.sheets and not errors:
        (d / "sheets").mkdir(exist_ok=True)
        for c in kept:
            out = d / "sheets" / f"cand{c['candidate_rank']:02d}.png"
            sheet(d / "vod.mp4", c, out)
        print(f"\nContact sheets in {d / 'sheets'}")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
