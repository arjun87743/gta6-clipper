"""Hands-off entry point for GitHub Actions (and usable on the Mac too).

  python pipeline/cloud.py watch   --campaign asianjeff      # print new VOD ids as JSON (for the workflow)
  python pipeline/cloud.py process --campaign asianjeff --vod-id ID
        download -> detect -> transcribe -> rank (Claude, headless) -> render edited clips -> queue
  python pipeline/cloud.py seed    --campaign asianjeff --vod-id ID
        same, but uses seed/<campaign>/<id>/rankings.json instead of ranking (already-approved clips)
  python pipeline/cloud.py post    --campaign asianjeff      # post whatever the schedule says is due
  python pipeline/cloud.py status  --campaign asianjeff      # one-screen summary

Clips are posted with no human approval (Arjun's choice, 2026-11-24). Arjun is only told about
things that need him: submitting a new post on Content Rewards, a YouTube video stuck private,
or a failure. Those arrive as GitHub issues (GitHub app push notification and email).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import traceback
from datetime import timedelta, timezone, datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import captions, state  # noqa: E402
from pipeline.tools import ROOT  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
CLIPS_DIR = ROOT / "output" / "clips"   # uploaded as the run's artifact
PLATFORM_NAMES = captions.PLATFORM_NAMES


def cfg_for(campaign):
    return yaml.safe_load(open(ROOT / "campaigns" / f"{campaign}.yaml"))


def py(*args):
    subprocess.run([sys.executable, *args], cwd=ROOT, check=True)


# ---------- notifications ----------

def notify(title, body, labels=("clipper",)):
    """A GitHub issue: the GitHub mobile app pushes it, and GitHub emails it. Falls back to the log."""
    print(f"\n=== NOTIFY: {title}\n{body}\n", flush=True)
    state.log("notify", title=title)
    if not os.environ.get("GH_TOKEN") or not shutil.which("gh"):
        return
    subprocess.run(["gh", "label", "create", labels[0], "--force", "--color", "FFD400"],
                   cwd=ROOT, capture_output=True)
    body += "\n\n@" + os.environ.get("NOTIFY_USER", "arjun87743")
    subprocess.run(["gh", "issue", "create", "--title", title, "--body", body,
                    *sum((["--label", lab] for lab in labels), [])], cwd=ROOT, capture_output=True)


# ---------- watch ----------

def cmd_watch(a):
    from pipeline import twitch
    done = state.load("processed.json", {})
    finished = {k for k, v in done.items() if v.get("status") in ("done", "failed")}
    vods = twitch.new_vods(cfg_for(a.campaign), finished)
    vods = sorted(vods, key=lambda v: v["date"] or "")[: a.limit]  # oldest first, a few per run
    print(json.dumps([v["id"] for v in vods]))


# ---------- process ----------

def seed_inputs(d, campaign, vod_id):
    """Rankings from seed/ plus a minimal candidates.json covering just those clips."""
    r = json.load(open(ROOT / "seed" / campaign / vod_id / "rankings.json"))
    cands = []
    for c in r["clips"]:
        cands.append({"rank": c["candidate_rank"], "peak": int(c["start"]), "start": c["start"] - 5,
                      "end": c["end"], "start_hms": "", "end_hms": "", "peak_hms": "", "score": c["score"],
                      "chat_z": 0, "audio_z": 0, "cutscene_frac": 0, "chat": []})
    dur = max(c["end"] for c in r["clips"]) + 120
    (d / "candidates.json").write_text(json.dumps({"vod_id": vod_id, "campaign": campaign, "duration_s": dur,
                                                   "candidates": cands, "rejected": []}, indent=1))
    (d / "rankings.json").write_text(json.dumps(r, indent=1, ensure_ascii=False))


def cmd_process(a, seed=False):
    cfg = cfg_for(a.campaign)
    d = ROOT / "data" / a.campaign / a.vod_id
    done = state.load("processed.json", {})
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    try:
        from pipeline import twitch
        print(f"== download {a.vod_id}", flush=True)
        twitch.download(a.vod_id, d)
        if seed:
            seed_inputs(d, a.campaign, a.vod_id)
        else:
            print("== detect", flush=True)
            py("pipeline/detect.py", "--campaign", a.campaign, "--vod-id", a.vod_id)
        print("== transcribe", flush=True)
        py("pipeline/transcribe.py", "--campaign", a.campaign, "--vod-id", a.vod_id)
        if not seed:
            print("== rank (Claude, headless)", flush=True)
            py("pipeline/autorank.py", "--campaign", a.campaign, "--vod-id", a.vod_id)
        print("== render", flush=True)
        queued = render_and_queue(cfg, d, a.campaign, a.vod_id, run_id)
        done[a.vod_id] = {"status": "done", "at": state.iso(), "run_id": run_id, "clips": queued,
                          "seed": seed}
        state.save("processed.json", done)
        state.log("processed", vod_id=a.vod_id, clips=len(queued), run_id=run_id)
        print(f"\n{len(queued)} clips queued from VOD {a.vod_id}")
    except Exception as e:
        tries = done.get(a.vod_id, {}).get("tries", 0) + 1
        done[a.vod_id] = {"status": "failed" if tries >= 3 else "retry", "tries": tries, "at": state.iso(),
                          "error": str(e)[-500:]}
        state.save("processed.json", done)
        state.log("process_failed", vod_id=a.vod_id, error=str(e)[-300:])
        if tries >= 3:
            notify(f"Clipper: VOD {a.vod_id} failed 3 times",
                   f"Processing stopped for this stream after 3 attempts.\n\n```\n{traceback.format_exc()[-2500:]}\n```")
        raise


def render_and_queue(cfg, d, campaign, vod_id, run_id):
    from pipeline import edit_layer, rankings
    r = json.load(open(d / "rankings.json"))
    trans = json.load(open(d / "transcripts.json"))
    trans_by_rank = {v["rank"]: v for v in trans.values()}
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    queue = state.load("queue.json", [])
    known = {q["clip_id"] for q in queue}
    kept = sorted([c for c in r["clips"] if c.get("keep")], key=lambda c: -c["score"])
    out = []
    for idx, c in enumerate(kept):
        clip_id = f"{campaign}_{vod_id}_c{c['candidate_rank']:02d}"
        if clip_id in known:
            continue
        try:
            video = edit_layer.render_edited(d, cfg, r, c, idx, trans_by_rank, CLIPS_DIR)
        except Exception as e:  # one bad clip must not sink the batch
            print(f"  render failed for {clip_id}: {e}", flush=True)
            state.log("render_failed", clip_id=clip_id, error=str(e)[-300:])
            continue
        meta = json.load(open(video.with_suffix(".json")))
        hook = meta["hook_used"] or rankings.edit_fields(c)[0]["hook_text"]
        auto = cfg.get("autopost", {}).get("platforms", [])
        targets = [p for p in captions.platforms(cfg, meta) if p in auto]
        texts = {p: captions.for_platform(cfg, meta, hook, p) for p in targets}
        queue.append({"clip_id": clip_id, "vod_id": vod_id, "campaign": campaign, "run_id": run_id,
                      "file": video.name, "score": c["score"], "angle": (c.get("angle") or "").lower(),
                      "summary": c.get("summary", ""), "hook": hook, "duration_s": meta["duration_s"],
                      "added_at": state.iso(), "pending": targets, "texts": texts, "posts": {}})
        out.append(clip_id)
        state.save("queue.json", queue)
    return out


# ---------- post ----------

def due_platforms(cfg, posts_log, now_ist):
    """Platforms that may post right now: inside a posting hour, under the daily cap, gap respected."""
    ap = cfg.get("autopost", {})
    if now_ist.hour not in ap.get("post_hours_ist", []) and not os.environ.get("CLIPPER_POST_NOW"):
        return []
    out = []
    for p in ap.get("platforms", []):
        mine = [x for x in posts_log if x["platform"] == p and x.get("ok")]
        last = max((state.parse(x["at"]) for x in mine), default=None)
        today = [x for x in mine if state.parse(x["at"]).astimezone(IST).date() == now_ist.date()]
        if len(today) >= ap.get("per_day", 2):
            continue
        if last and state.now() - last < timedelta(hours=ap.get("min_gap_hours", 6)) - timedelta(minutes=20):
            continue
        out.append(p)
    return out


def pick(queue, platform, last_angle, avoid_repeat):
    options = [q for q in queue if platform in q["pending"]]
    if avoid_repeat and last_angle:
        fresh = [q for q in options if q["angle"] != last_angle]
        options = fresh or options
    # Best score first; older clips first on ties so a stream's clips go out in order.
    return sorted(options, key=lambda q: (-q["score"], q["added_at"]))[0] if options else None


def fetch_video(item):
    path = CLIPS_DIR / item["file"]
    if path.exists():
        return path
    if item["run_id"] == "local":
        raise FileNotFoundError(path)
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(["gh", "run", "download", item["run_id"], "-n", f"clips-{item['vod_id']}", "-D", str(CLIPS_DIR)],
                   cwd=ROOT, check=True)
    return path


def cmd_post(a):
    from pipeline import platforms
    cfg = cfg_for(a.campaign)
    queue = state.load("queue.json", [])
    posts_log = state.load("posts.json", [])
    now_ist = state.now().astimezone(IST)
    plats = due_platforms(cfg, posts_log, now_ist)
    if not plats or not any(q["pending"] for q in queue):
        print(f"nothing due at {now_ist:%H:%M} IST (platforms due: {plats or 'none'}, "
              f"queued: {sum(bool(q['pending']) for q in queue)})")
        return
    avoid = cfg.get("autopost", {}).get("avoid_same_angle_back_to_back", True)
    new_posts = []
    for p in plats:
        last = next((x for x in reversed(posts_log) if x["platform"] == p and x.get("ok")), None)
        item = pick(queue, p, last and last.get("angle"), avoid)
        if not item:
            continue
        t = item["texts"][p]
        try:
            video = fetch_video(item)
            res = platforms.post(p, video, t["title"], t["text"])
            entry = {"at": state.iso(), "platform": p, "clip_id": item["clip_id"], "angle": item["angle"],
                     "ok": True, **res}
            item["posts"][p] = res["url"]
            new_posts.append(entry)
            print(f"posted {item['clip_id']} to {p}: {res['url']}", flush=True)
        except Exception as e:
            entry = {"at": state.iso(), "platform": p, "clip_id": item["clip_id"], "ok": False, "error": str(e)[-500:]}
            item.setdefault("failures", {}).setdefault(p, 0)
            item["failures"][p] += 1
            print(f"FAILED {item['clip_id']} on {p}: {e}", flush=True)
            if item["failures"][p] >= 3:
                notify(f"Clipper: posting to {PLATFORM_NAMES.get(p, p)} keeps failing",
                       f"Clip `{item['clip_id']}` failed 3 times on {p}; it is skipped there now.\n\n"
                       f"Last error:\n```\n{str(e)[-1500:]}\n```")
        if entry["ok"] or item.get("failures", {}).get(p, 0) >= 3:
            item["pending"] = [x for x in item["pending"] if x != p]
        posts_log.append(entry)
        state.save("queue.json", queue)
        state.save("posts.json", posts_log)
        state.log("post", platform=p, clip_id=item["clip_id"], ok=entry["ok"])

    ok = [x for x in new_posts if x.get("ok")]
    if ok:
        mins = cfg.get("rules", {}).get("submit_within_minutes", 30)
        lines = [f"- **{PLATFORM_NAMES.get(x['platform'], x['platform'])}**: {x['url']}"
                 + (f"\n  - ⚠️ {x['note']}" if x.get("note") else "") for x in ok]
        notify(f"Submit now: {ok[0]['clip_id']} ({len(ok)} post{'s' if len(ok) > 1 else ''})",
               f"New posts are live. Submit each link on Content Rewards within {mins} minutes:\n"
               f"{cfg.get('campaign_url')}\n\n" + "\n".join(lines)
               + "\n\nClose this issue once submitted.")


# ---------- status ----------

def cmd_status(a):
    queue = state.load("queue.json", [])
    posts = state.load("posts.json", [])
    done = state.load("processed.json", {})
    ok = [p for p in posts if p.get("ok")]
    print(f"VODs processed: {sum(v.get('status') == 'done' for v in done.values())}   "
          f"failed: {sum(v.get('status') == 'failed' for v in done.values())}")
    print(f"Clips queued (still to post somewhere): {sum(bool(q['pending']) for q in queue)}")
    print(f"Posts: {len(ok)} ok, {len(posts) - len(ok)} failed")
    for p in ok[-10:]:
        print(f"  {p['at'][:16]}  {p['platform']:<15} {p['clip_id']}  {p['url']}")
    nxt = sorted([q for q in queue if q["pending"]], key=lambda q: (-q["score"], q["added_at"]))[:3]
    for q in nxt:
        print(f"  next: {q['clip_id']} ({q['score']}) -> {', '.join(q['pending'])}  {q['hook']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["watch", "process", "seed", "post", "status"])
    ap.add_argument("--campaign", default="asianjeff")
    ap.add_argument("--vod-id")
    ap.add_argument("--limit", type=int, default=1)
    a = ap.parse_args()
    if a.cmd in ("process", "seed") and not a.vod_id:
        ap.error("--vod-id is required")
    {"watch": cmd_watch, "process": cmd_process, "seed": lambda x: cmd_process(x, seed=True),
     "post": cmd_post, "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    main()
