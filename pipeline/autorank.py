"""Rank candidates without a person: runs Claude Code headless with pipeline/rank_rules.md.

Run (needs the `claude` CLI and CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`, which uses the
Claude subscription, no API key):
  python pipeline/autorank.py --campaign asianjeff --vod-id 2933231152

Reads rank_packet.md, writes rankings.json, validates it with pipeline/rankings.py. If validation
fails, Claude gets one retry with the errors. Clips still failing after that are switched to
keep=false (with the reason), so a bad entry drops one clip instead of stopping the run.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import rankings  # noqa: E402
from pipeline.tools import ROOT  # noqa: E402

PROMPT = """You are ranking clip candidates for a paid clipping campaign. Nobody will review your
output before the clips are posted publicly, so when in doubt, reject.

1. Read {rules} carefully and follow it exactly.
2. Read {packet} (transcript and chat for each candidate).
3. Write {out} as valid JSON in the format the rules describe, with "ranked_by": "claude-code-headless"
   and "ranked_at": "{today}". Every candidate in the packet must get an entry, kept or rejected.
   Keep at most {max_keep} clips. Vary outro_type and angle across the kept clips.
{extra}
Write only that one file. Do not run commands."""


def call_claude(prompt, cwd):
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("claude CLI not found (npm install -g @anthropic-ai/claude-code)")
    if not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")):
        raise RuntimeError("set CLAUDE_CODE_OAUTH_TOKEN (claude setup-token) as a GitHub secret")
    p = subprocess.run([exe, "-p", prompt, "--allowedTools", "Read,Write", "--max-turns", "12",
                        "--output-format", "text"], cwd=cwd, capture_output=True, text=True, timeout=1500)
    if p.returncode != 0:
        raise RuntimeError(f"claude failed ({p.returncode}): {(p.stderr or p.stdout)[-1500:]}")
    return p.stdout


def prune(r, cands, trans):
    """Switch off kept clips that still fail validation. Returns the list of dropped messages."""
    errors, _ = rankings.validate(r, cands, trans)
    dropped = []
    for c in r["clips"]:
        mine = [e for e in errors if e.startswith(f"#{c.get('candidate_rank')}:")]
        if mine and c.get("keep"):
            c["keep"] = False
            c["reason"] = f"auto-dropped: {'; '.join(mine)}"
            dropped += mine
    r["clips"] = [c for c in r["clips"] if c.get("candidate_rank") in {x["rank"] for x in cands["candidates"]}]
    return dropped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--vod-id", required=True)
    ap.add_argument("--max-keep", type=int, default=8)
    args = ap.parse_args()
    from datetime import date
    d = ROOT / "data" / args.campaign / args.vod_id
    out = d / "rankings.json"
    cands = json.load(open(d / "candidates.json"))
    trans = json.load(open(d / "transcripts.json"))
    base = dict(rules=os.path.relpath(ROOT / "pipeline" / "rank_rules.md", ROOT),
                packet=os.path.relpath(d / "rank_packet.md", ROOT), out=os.path.relpath(out, ROOT),
                today=date.today().isoformat(), max_keep=args.max_keep)

    extra = ""
    for attempt in (1, 2):
        out.unlink(missing_ok=True)
        call_claude(PROMPT.format(**base, extra=extra), ROOT)
        try:
            r = json.load(open(out))
            errors, _ = rankings.validate(r, cands, trans)
        except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError) as e:
            r, errors = None, [f"rankings.json missing or not valid JSON: {e}"]
        if not errors:
            break
        print(f"attempt {attempt}: {len(errors)} validation errors", flush=True)
        extra = "\nYour previous attempt failed validation with these errors; fix them:\n- " + "\n- ".join(errors)
    if r is None:
        raise SystemExit("ranking failed twice: no usable rankings.json")
    dropped = prune(r, cands, trans)
    out.write_text(json.dumps(r, indent=1, ensure_ascii=False))
    kept = [c for c in r["clips"] if c.get("keep")]
    print(f"ranked: {len(kept)} kept, {len(r['clips']) - len(kept)} rejected, {len(dropped)} auto-dropped")
    for m in dropped:
        print("  dropped", m)


if __name__ == "__main__":
    main()
