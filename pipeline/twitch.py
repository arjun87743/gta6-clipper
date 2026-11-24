"""Find new VODs on the streamer's channel and download what the pipeline needs.

No Twitch API key: yt-dlp reads the public channel page. The cloud run downloads the smallest
rendition (160p) for detection and audio matching; render.py fetches only the clip sections at 1080p.
"""
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.tools import ROOT, TWITCH_DL, YTDLP  # noqa: E402

GTA6 = re.compile(r"\bGTA\s*(6|VI)\b", re.I)


def is_live(channel):
    """True while the channel is live. A VOD of a stream still in progress is not finished yet."""
    p = subprocess.run([YTDLP, "-q", "--skip-download", "--print", "is_live",
                        f"https://www.twitch.tv/{channel}"], capture_output=True, text=True)
    return p.returncode == 0 and p.stdout.strip() == "True"


def list_vods(channel, limit=15):
    """Newest past broadcasts: [{id, title, date (UTC iso), duration_s}]."""
    p = subprocess.run([YTDLP, "-J", "--flat-playlist", "--playlist-end", str(limit),
                        f"https://www.twitch.tv/{channel}/videos?filter=archives&sort=time"],
                       capture_output=True, text=True, check=True)
    out = []
    for e in json.loads(p.stdout).get("entries") or []:
        vid = str(e.get("id", "")).lstrip("v")
        if not vid.isdigit():
            continue
        ts = e.get("timestamp")
        out.append({"id": vid, "title": e.get("title") or "",
                    "date": datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None,
                    "duration_s": e.get("duration")})
    return out


def new_vods(cfg, done_ids):
    """VODs that match the campaign (title filter, date) and have not been processed."""
    src = cfg["source"]
    since = cfg.get("rules", {}).get("source_from")
    vods = list_vods(src["channel"])
    if vods and is_live(src["channel"]):
        vods = vods[1:]  # newest one is still being recorded
    keep = []
    for v in vods:
        if v["id"] in done_ids:
            continue
        if src.get("vod_filter") and not GTA6.search(v["title"]):
            continue
        if since and v["date"] and v["date"][:10] < since:
            continue
        keep.append(v)
    return keep


def download(vod_id, d):
    """160p video+audio as vod.mp4, and chat.json. Safe to rerun."""
    d.mkdir(parents=True, exist_ok=True)
    if not (d / "vod.mp4").exists():
        subprocess.run([YTDLP, "-q", "--no-progress", "-f", "160p30/160p/360p30/360p/worst",
                        "-o", str(d / "vod.%(ext)s"), "--remux-video", "mp4",
                        f"https://www.twitch.tv/videos/{vod_id}"], check=True)
    if not (d / "chat.json").exists():
        subprocess.run([TWITCH_DL, "chatdownload", "--id", vod_id, "-o", str(d / "chat.json"),
                        "--collision", "Overwrite"], check=True, capture_output=True)


if __name__ == "__main__":
    import yaml
    cfg = yaml.safe_load(open(ROOT / "campaigns" / f"{sys.argv[1] if len(sys.argv) > 1 else 'asianjeff'}.yaml"))
    for v in new_vods(cfg, set()):
        print(v)
