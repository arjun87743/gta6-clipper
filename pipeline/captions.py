"""Post captions for clips: per-platform text built from the campaign config.

`build()` makes the human-readable caption file (review page, manual posting).
`for_platform()` returns the exact text the auto-poster uses for one platform.
Campaign wording comes from campaigns/<campaign>.yaml; any field still set to TODO makes the
caption INCOMPLETE, and the auto-poster refuses to post it.
"""
from datetime import date

PLATFORM_NAMES = {"tiktok": "TikTok", "instagram": "Instagram Reels",
                  "youtube_shorts": "YouTube Shorts", "x": "X"}

# Rotated per clip so captions are not identical (hard rule 10).
TEMPLATES = ["{hook}", "{hook} 😭", "{hook} | {streamer} plays GTA 6"]


def _todo(v):
    return v is None or (isinstance(v, str) and v.strip().upper() == "TODO") or \
        (isinstance(v, list) and any(_todo(x) for x in v))


def platforms(cfg, meta):
    """Where this clip may be posted: allowed for the clip when rendered AND still in the campaign config."""
    return [p for p in meta["platforms_ok"] if p in cfg.get("platforms", [])]


def missing_fields(cfg):
    cap = cfg.get("caption", {})
    missing = [k for k in ("required_text", "required_hashtags") if _todo(cap.get(k))]
    if _todo(cfg.get("campaign_url")):
        missing.append("campaign_url")
    return missing


def tag_list(cfg, platform):
    cap = cfg.get("caption", {})
    tags = [cap.get("disclosure_caption", "#ad")]
    req = cap.get("required_hashtags") or []
    tags += [t for t in req if not _todo(t)]
    tags += cap.get("extra_hashtags") or []
    if platform == "youtube_shorts":
        tags.append("#shorts")
    out = []
    for t in tags:  # dedupe, keep order
        if t and t.lower() not in [x.lower() for x in out]:
            out.append(t)
    return " ".join(out)


def for_platform(cfg, meta, hook, platform):
    """{'title', 'text'} ready to post. Raises if campaign fields are incomplete."""
    missing = missing_fields(cfg)
    if missing:
        raise ValueError(f"campaign caption fields incomplete: {', '.join(missing)}")
    cap = cfg.get("caption", {})
    streamer = cfg.get("source", {}).get("channel", "")
    line = TEMPLATES[meta["candidate_rank"] % len(TEMPLATES)].format(hook=hook, streamer=streamer)
    req_text = "" if _todo(cap.get("required_text")) else cap["required_text"].strip()
    body = " ".join(x for x in [line, req_text, cap.get("mention", "")] if x)
    if platform == "youtube_shorts":
        return {"title": f"{hook} #ad"[:100], "text": f"{body}\n{tag_list(cfg, platform)}"}
    if platform == "x":
        return {"title": "", "text": f"{body} {tag_list(cfg, platform)}"[:280]}
    return {"title": "", "text": f"{body}\n\n{tag_list(cfg, platform)}"}


def build(cfg, meta, hook):
    """Return (text, complete). text is the whole caption file."""
    meta = {**meta, "platforms_ok": platforms(cfg, meta)}
    missing = missing_fields(cfg)

    parts = []
    if missing:
        parts.append("!!! INCOMPLETE: DO NOT POST YET !!!")
        parts.append(f"Fill these in campaigns/{cfg['id']}.yaml from the campaign page, then re-approve: "
                     + ", ".join(missing))
        parts.append("")
    parts += [
        f"Clip: {meta['clip_id']}   Campaign: {cfg.get('name', cfg['id'])}   Approved: {date.today().isoformat()}",
        f"Video: output/approved/{meta['clip_id']}.mp4   Length: {meta['duration_s']:.0f}s",
        f"Post on: {', '.join(PLATFORM_NAMES.get(p, p) for p in meta['platforms_ok'])}",
    ]
    skipped = [p for p in cfg.get("platforms", []) if p not in meta["platforms_ok"]]
    if skipped:
        parts.append(f"Do NOT post on: {', '.join(PLATFORM_NAMES.get(p, p) for p in skipped)} "
                     "(violence in the first 7 s)")
    parts.append("")

    if not missing:
        for p in meta["platforms_ok"]:
            parts.append(f"===== {PLATFORM_NAMES.get(p, p)} =====")
            t = for_platform(cfg, meta, hook, p)
            if t["title"]:
                parts.append(f"Title: {t['title']}")
                parts.append(f"Description: {t['text']}")
            else:
                parts.append(t["text"])
            parts.append("")

    parts += [
        "===== Checklist =====",
        "[ ] Watched the whole clip once more (captions, no spoilers, facecam visible)",
        "[ ] Posted from the account connected to Content Rewards",
        f"[ ] Submitted the post URL on the campaign page within {cfg.get('rules', {}).get('submit_within_minutes', 30)} minutes",
        "[ ] Logged post URL and time (tracking)",
        "[ ] This exact clip is used for THIS campaign only (re-cut for any other campaign)",
    ]
    return "\n".join(parts) + "\n", not missing
