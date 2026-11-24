"""M3: find candidate clip moments in a VOD from chat spikes + audio peaks,
and flag cutscene-heavy windows.

Run:
  python pipeline/detect.py --campaign asianjeff --vod-id 2933231152

Reads   data/<campaign>/<vod_id>/vod.mp4 and chat.json
Writes  data/<campaign>/<vod_id>/candidates.json  (ranked, plus rejected list)
Caches  audio_loudness.npy, keyframes.npz in the same folder (delete to recompute).
vod.mp4 can be any resolution: frames are scaled to 128x72 for the cutscene check, so the
cloud run uses the small 160p rendition to save download time and disk.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.tools import FFMPEG, ROOT  # noqa: E402

# Defaults; a campaign YAML can override any of these under `detect:`.
DEFAULTS = {
    "chat_lag_s": 8,          # chat reacts this many seconds after the moment
    "pre_roll_s": 25,         # window starts this long before the moment
    "post_roll_s": 20,        # and ends this long after it
    "chat_weight": 0.4,
    "audio_weight": 0.6,
    "min_gap_s": 90,          # two candidates must be at least this far apart
    "top_n": 25,
    "skip_start_s": 0,        # ignore the "stream starting" intro if needed
    "skip_end_s": 0,          # ignore the goodbye / outro at the end
    "cutscene_reject_frac": 0.5,
    "audio_min_spread_db": 3.0,
}

HYPE = re.compile(
    r"\b(clip|clip it|lmao+|lmf?ao|lol+|kekw|lul|omegalul|w+|dub|no way|nah+|bro+|wtf|omg|"
    r"holy|insane|crazy|goat|bruh|ayo+|dead|💀|😂|🤣)\b|!{2,}|\?{2,}",
    re.I,
)
EMOTE_HYPE = re.compile(r"(lul|kek|pog|kreygasm|sob|hype|troll|outofchair|fullbox|dance)", re.I)


# ---------- signals ----------

BOTS = {"fossabot", "streamelements", "nightbot", "moobot", "streamlabs", "wizebot", "sery_bot"}


def chat_signal(chat_path, duration, user_cooldown_s=10):
    """Per-second weighted chat activity. Hype words, emotes and caps count extra.
    Bots are ignored, and each person counts at most once per cooldown, so one
    spammer cannot fake a spike."""
    comments = json.load(open(chat_path))["comments"]
    w = np.zeros(int(duration) + 1)
    msgs = []
    last_counted = {}
    for c in comments:
        t = int(c["content_offset_seconds"])
        user = c["commenter"]["name"].lower()
        if t > duration or user in BOTS:
            continue
        body = c["message"]["body"]
        msgs.append((t, c["commenter"]["display_name"], body))
        if t - last_counted.get(user, -1e9) < user_cooldown_s:
            continue
        last_counted[user] = t
        weight = 1.0
        if HYPE.search(body):
            weight += 1.5
        frags = c["message"].get("fragments") or []
        if any(f.get("emoticon") and EMOTE_HYPE.search(f["text"]) for f in frags):
            weight += 1.0
        letters = [ch for ch in body if ch.isalpha()]
        if len(letters) >= 4 and sum(ch.isupper() for ch in letters) / len(letters) > 0.7:
            weight += 0.5
        w[t] += weight
    return w, msgs


def audio_loudness(vod, cache):
    """Per-second loudness in dB from a mono 8 kHz decode of the whole VOD."""
    if cache.exists():
        return np.load(cache)
    print("Decoding audio for loudness (one-off, a minute or two)...", flush=True)
    sr = 8000
    p = subprocess.Popen([FFMPEG, "-v", "error", "-i", str(vod), "-vn", "-ac", "1", "-ar", str(sr),
                          "-f", "s16le", "-"], stdout=subprocess.PIPE)
    out = []
    while True:
        buf = p.stdout.read(sr * 2 * 60)  # one minute at a time
        if not buf:
            break
        a = np.frombuffer(buf, dtype=np.int16).astype(np.float32)
        n = len(a) // sr
        if n:
            sec = a[: n * sr].reshape(n, sr)
            out.append(20 * np.log10(np.sqrt((sec ** 2).mean(axis=1)) + 1.0))
    p.wait()
    db = np.concatenate(out)
    np.save(cache, db)
    return db


def keyframes(vod, cache):
    """Tiny grayscale keyframes (about one every 2 s) with timestamps, for cutscene detection."""
    if cache.exists():
        z = np.load(cache)
        return z["t"], z["f"]
    print("Scanning keyframes for cutscenes (one-off, a few minutes)...", flush=True)
    W, H = 128, 72
    p = subprocess.Popen([FFMPEG, "-v", "info", "-skip_frame", "nokey", "-i", str(vod), "-an",
                          "-vf", f"scale={W}:{H},format=gray,showinfo", "-fps_mode", "passthrough",
                          "-f", "rawvideo", "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    import threading
    times = []

    def read_err():
        for line in p.stderr:
            m = re.search(rb"pts_time:([\d.]+)", line)
            if m and b"Parsed_showinfo" in line:
                times.append(float(m.group(1)))

    th = threading.Thread(target=read_err)
    th.start()
    raw = p.stdout.read()
    p.wait()
    th.join()
    f = np.frombuffer(raw, dtype=np.uint8).reshape(-1, H, W)
    t = np.array(times[: len(f)])
    np.savez_compressed(cache, t=t, f=f[: len(t)])
    return t, f


def letterbox_flags(frames):
    """True where a frame has cinematic black bars top and bottom (cutscene sign).
    Bars measured on AsianJeff's 720p layout scaled to 128x72; top band avoids the
    facecam (left) and chat overlay (right)."""
    top = frames[:, 0:3, 36:88].reshape(len(frames), -1).astype(np.float32)
    bot = frames[:, 69:72, 36:120].reshape(len(frames), -1).astype(np.float32)
    inner = frames[:, 10:60, 36:120].reshape(len(frames), -1).astype(np.float32)
    bars_black = (top.mean(1) < 14) & (bot.mean(1) < 14) & (top.std(1) < 4) & (bot.std(1) < 4)
    # Dark night gameplay is dark everywhere; real bars are darker than the picture inside.
    return bars_black & (inner.mean(1) > top.mean(1) + 6)


# ---------- helpers ----------

def rolling(x, win, fn):
    """Rolling statistic, centred, edges handled by shrinking the window."""
    from numpy.lib.stride_tricks import sliding_window_view
    h = win // 2
    pad = np.pad(x, (h, h), mode="edge")
    return fn(sliding_window_view(pad, win), axis=1)[: len(x)]


def smooth(x, win):
    return np.convolve(x, np.ones(win) / win, mode="same")


def robust_z(x, base_win, min_spread):
    med = rolling(x, base_win, np.median)
    mad = rolling(np.abs(x - med), base_win, np.median) * 1.4826
    # Floor the spread so a quiet or silent stretch cannot turn normal sound into a huge spike.
    return (x - med) / np.maximum(mad, min_spread)


def fmt(t):
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--vod-id", required=True)
    args = ap.parse_args()

    cfg = yaml.safe_load(open(ROOT / "campaigns" / f"{args.campaign}.yaml"))
    P = {**DEFAULTS, **(cfg.get("detect") or {})}
    d = ROOT / "data" / args.campaign / args.vod_id
    vod, chat = d / "vod.mp4", d / "chat.json"

    db = audio_loudness(vod, d / "audio_loudness.npy")
    dur = len(db)

    # Chat: 10 s rolling sum, shifted back by the chat lag, vs a 5-minute baseline.
    cw, msgs = chat_signal(chat, dur - 1)
    cw = smooth(cw, 10) * 10
    cw = np.roll(cw, -P["chat_lag_s"])
    chat_base = rolling(cw, 301, np.median)
    chat_z = (cw - chat_base) / np.sqrt(chat_base + 1.0)  # Poisson-style, works for quiet chats

    # Audio: 3 s loudness vs a 2-minute baseline. Silent seconds (muted / stream over)
    # are filled with the typical level so they do not distort the baseline.
    silent = db < 20
    db_f = db.copy()
    db_f[silent] = np.median(db[~silent]) if (~silent).any() else 0
    audio_z = robust_z(smooth(db_f, 3), 121, P["audio_min_spread_db"])
    audio_z[smooth(silent.astype(float), 5) > 0] = 0  # no peaks next to silence

    score = P["chat_weight"] * np.clip(chat_z, 0, 8) + P["audio_weight"] * np.clip(audio_z, 0, 8)
    score = smooth(score, 5)
    score[: P["skip_start_s"]] = 0
    if P["skip_end_s"]:
        score[-P["skip_end_s"]:] = 0

    # Peak picking with a minimum gap between candidates.
    order = np.argsort(score)[::-1]
    taken = []
    for t in order:
        if score[t] <= 0 or len(taken) >= P["top_n"] * 3:
            break
        if all(abs(t - u) >= P["min_gap_s"] for u in taken):
            taken.append(int(t))

    kt, kf = keyframes(vod, d / "keyframes.npz")
    lb = letterbox_flags(kf)

    cands, rejected = [], []
    for t in taken:
        s, e = max(0, t - P["pre_roll_s"]), min(dur, t + P["post_roll_s"])
        inwin = (kt >= s) & (kt <= e)
        cut_frac = float(lb[inwin].mean()) if inwin.any() else 0.0
        chat_lines = [f"{fmt(mt)} {n}: {b}" for mt, n, b in msgs if s <= mt <= e + P["chat_lag_s"] + 10]
        c = {
            "peak": t, "start": s, "end": e,
            "peak_hms": fmt(t), "start_hms": fmt(s), "end_hms": fmt(e),
            "score": round(float(score[t]), 2),
            "chat_z": round(float(chat_z[t]), 2),
            "audio_z": round(float(audio_z[t]), 2),
            "cutscene_frac": round(cut_frac, 2),
            "chat": chat_lines[:40],
        }
        if cut_frac >= P["cutscene_reject_frac"]:
            c["reject_reason"] = f"cutscene-heavy ({cut_frac:.0%} of keyframes letterboxed)"
            rejected.append(c)
        elif len(cands) < P["top_n"]:
            cands.append(c)

    for i, c in enumerate(cands, 1):
        c["rank"] = i

    out = {
        "vod_id": args.vod_id, "campaign": args.campaign, "duration_s": dur,
        "params": P,
        "cutscene_minutes_total": round(float(lb.mean()) * dur / 60, 1),
        "candidates": cands, "rejected": rejected,
    }
    (d / "candidates.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))

    print(f"\n{len(cands)} candidates, {len(rejected)} rejected as cutscenes "
          f"(about {out['cutscene_minutes_total']} min of the VOD looks like cutscenes)\n")
    print(f"{'#':>2}  {'window':19} {'score':>5} {'chat':>5} {'audio':>5} {'cut':>4}")
    for c in cands:
        print(f"{c['rank']:>2}  {c['start_hms']}-{c['end_hms']} {c['score']:>5} {c['chat_z']:>5} "
              f"{c['audio_z']:>5} {c['cutscene_frac']:>4}")
    print(f"\nWrote {d / 'candidates.json'}")


if __name__ == "__main__":
    main()
