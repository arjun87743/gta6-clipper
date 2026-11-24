"""M6: viral edit layer. Used by `python pipeline/render.py ... --edit`.

Layers (each can be switched off with --without, for A/B tests):
  hook       big hook text for 1.5 s plus a punch-in on the facecam
  punchline  at punchline_ts: 0.6 s freeze (fail, shock) or a fast zoom (other moods)
  outro      1.8 to 2.2 s reaction card built from the streamer's own facecam
  captions   animated word-by-word captions with key words coloured
  sfx        whoosh, punchline sound and outro sound from assets/manifest.json

What the code guarantees whatever the settings:
  - the "Ad" label is drawn last, on every frame from frame 0, outro included
  - on clips with violence_early_risk the gameplay is never zoomed or frozen, only the facecam
  - the facecam stays in its own slot
  - campaign `edit:` flags that are missing count as "not allowed"
  - no music is added, and only assets with an allowed licence are used
Edited clips go to output/edited/.
"""
import copy
import json
import re
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from pipeline import rankings
from pipeline import render as base
from pipeline.tools import FFMPEG, ROOT, font_file

W, H, FPS = base.W, base.H, base.FPS
LAYERS = ("hook", "punchline", "outro", "captions", "sfx")
PUNCH_STYLES = ("auto", "zoom", "freeze")
HOOK_S = 1.5
LICENCES = ("own-work", "cc0", "public-domain", "royalty-free")
FREEZE_MOODS = ("fail", "shock")
OUTRO_S = {"reaction_freeze": 2.0, "reaction_replay": 2.2, "reaction_zoom": 1.8}
OUTRO_WORDS = {"funny": "bro...", "fail": "we go again", "shock": "wait, what?", "hype": "W.", "wholesome": "we take those"}
# ASS colours are BGR. The key-word colour always differs from that style's active-word colour.
KEY_COLOUR = {"bold_yellow": "&H00B469FF&", "impact_green": "&H0000D7FF&", "futura_cyan": "&H004BE1FF&"}
KEY_WORDS = re.compile(
    r"^([lr][123]|w|w's|controller|joystick|sprint\w*|died|die|dead|fail\w*|restart|back|heal|help|scared|"
    r"trolling|jump|cops|escape\w*|crash\w*|box|binds|physique|abs|veins|alike|six|pack|slow|turbo)$", re.I)


# ---------- settings ----------

def allowed_layers(cfg, without):
    flags = {k: cfg.get("edit", {}).get(k) is True for k in ("allow_sfx", "allow_music", "allow_overlays")}
    on = set(LAYERS) - set(without)
    if not flags["allow_sfx"]:
        on.discard("sfx")
    if not flags["allow_overlays"]:
        on.discard("outro")
    return on, flags


def pick_sound(role, mood, idx):
    """A sound for this role and mood from the manifest, rotated by clip."""
    path = ROOT / "assets" / "manifest.json"
    if not path.exists():
        return None
    found = [a for a in json.loads(path.read_text())["assets"]
             if a.get("kind") == "sfx" and a.get("role") == role and a.get("licence") in LICENCES
             and (role == "hook" or mood in a.get("moods", []))
             and (ROOT / "assets" / a["file"]).is_file()]
    return found[idx % len(found)] if found else None


def apply_overrides(c, ov, trans, cfg):
    """Apply review-page changes (a version's spec) to a ranked clip. Invalid values are dropped
    with a warning, never fatal. Times in the spec are absolute VOD seconds, like rankings.json.
    Returns (clip, settings, warnings)."""
    c, warns = copy.deepcopy(c), []
    rules = cfg.get("rules", {})
    lo, hi = trans["t_start"], trans["t_end"]
    s = float(ov.get("start", c["start"]))
    e = float(ov.get("end", c["end"]))
    if not (lo <= s < e <= hi and rules.get("min_clip_s", 15) <= e - s <= rules.get("max_clip_s", 60)):
        warns.append(f"cut {s:.1f}-{e:.1f} must be {rules.get('min_clip_s', 15)} to {rules.get('max_clip_s', 60)} s "
                     f"inside the transcribed {lo:.0f}-{hi:.0f}, kept {c['start']:.1f}-{c['end']:.1f}")
    else:
        c["start"], c["end"] = round(s, 2), round(e, 2)
    for key in ("punchline_ts", "reaction_ts", "mood", "outro_type", "outro_text"):
        if key in ov:
            c[key] = ov[key]
    st = {"layout": ov.get("layout"), "caption_style": ov.get("caption_style"), "hook": ov.get("hook"),
          "caption_scale": ov.get("caption_scale", 1.0), "sfx_gain": ov.get("sfx_gain", 1.0),
          "punch_style": ov.get("punch_style", "auto"), "without": ov.get("without", [])}
    checks = {"layout": lambda v: v is None or v in base.LAYOUTS,
              "caption_style": lambda v: v is None or v in base.CAPTION_STYLES,
              "hook": lambda v: v is None or (isinstance(v, str) and len(v) <= 60),
              "caption_scale": lambda v: type(v) in (int, float) and 0.7 <= v <= 1.5,
              "sfx_gain": lambda v: type(v) in (int, float) and 0 <= v <= 2.5,
              "punch_style": lambda v: v in PUNCH_STYLES,
              "without": lambda v: isinstance(v, list) and set(v) <= set(LAYERS)}
    defaults = {"caption_scale": 1.0, "sfx_gain": 1.0, "punch_style": "auto", "without": []}
    for k, ok in checks.items():
        if not ok(st[k]):
            warns.append(f"{k} {st[k]!r} is not valid, ignored")
            st[k] = defaults.get(k)
    return c, st, warns


# ---------- facecam over time ----------

def line_contrast(strip):
    """How much a straight vertical line in the middle of this strip stands out from its neighbours."""
    step = np.abs(np.diff(strip, axis=1)).mean(0)
    return step[4:11].max() - np.median(step)


def cam_timeline(seg, ss, dur, boxes, sample_fps=4):
    """[(time, box name), ...]. The streamer can change cam size mid-clip (clip #13 does at 20 s),
    so score the top and right border of every box on every sample and smooth over 2 s.
    A border scores by how much sharper it is than the lines next to it, so a busy room or game
    texture inside a wrong box scores near zero."""
    sw, sh = base.probe_size(seg)
    raw = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{ss:.3f}", "-i", str(seg), "-t", f"{dur}",
                          "-vf", f"fps={sample_fps},format=gray", "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, sh, sw).astype(np.int16)
    names = list(boxes)
    scores = np.zeros((len(frames), len(names)))
    for j, name in enumerate(names):
        b = boxes[name]
        x, y, r, btm = b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]
        for i, f in enumerate(frames):
            scores[i, j] = (line_contrast(f[y + 10:btm - 10, r - 8:r + 8])
                            + line_contrast(f[y - 8:y + 8, x + 10:r - 10].T))
    half = sample_fps
    best = [int(np.median(scores[max(0, i - half):i + half + 1], axis=0).argmax()) for i in range(len(frames))]
    area = lambda n: boxes[n]["w"] * boxes[n]["h"]  # noqa: E731
    out = [(0.0, names[best[0]])]
    for i in range(1, len(best)):
        new = names[best[i]]
        if new == out[-1][1]:
            continue
        if not all(b == best[i] for b in best[i:i + 2 * sample_fps]):  # must hold for 2 s
            continue
        # The cam animates between sizes: use the smaller box during the change so no gameplay leaks in.
        shift = -0.1 if area(new) < area(out[-1][1]) else 0.6
        out.append((round(max(out[-1][0], i / sample_fps + shift), 2), new))
    return out


def cam_at(timeline, t):
    return [name for at, name in timeline if at <= max(t, 0)][-1]


# ---------- picture ----------

def pulse(start, rise, end, fall):
    """Expression that goes 0 to 1 at start and back to 0 at end (t is zoompan's `it`)."""
    return f"max(0,min(1,min((it-{start:.2f})/{rise},({end:.2f}-it)/{fall})))"


def zoom(expr, w, h, anchor_y):
    # Work at 2x so the slow part of the zoom does not jitter on whole pixels.
    return (f"scale={2 * w}:{2 * h}:flags=bicubic,zoompan=z='{expr}':x='(iw-iw/zoom)/2':"
            f"y='(ih-ih/zoom)*{anchor_y}':d=1:s={w}x{h}:fps={FPS}")


def body_graph(layout, boxes, timeline, chat_bottom, sw, sh, dur, punch, mood, on, risky, punch_style="auto"):
    used = list(dict.fromkeys(name for _, name in timeline))
    # The game crop must clear the widest cam box this clip uses.
    widest = max(used, key=lambda n: boxes[n]["x"] + boxes[n]["w"])
    _, pos, _, gc = base.layout_plan(layout, boxes[widest], chat_bottom, sw, sh)
    bar = 260 if layout == "cam_top" else 0
    game_h = {"cam_top": H - 260 - 810, "game_top": 1110, "game_top_tall": 1240}[layout]
    cam_h = H - bar - game_h

    cam_z, game_z = [], []
    if "hook" in on:
        cam_z.append(f"0.22*{pulse(0, 0.15, HOOK_S, 0.5)}")
    freeze = None
    if punch is not None and "punchline" in on:
        p = pulse(punch, 0.08, min(punch + 0.9, dur), 0.25)
        cam_z.append(f"0.30*{p}")
        if not risky:
            game_z.append(f"0.14*{p}")
            if punch_style == "freeze" or (punch_style == "auto" and mood in FREEZE_MOODS):
                freeze = (punch, min(punch + 0.6, dur - 0.1))

    start = f"[0:v]fps={FPS},setpts=PTS-STARTPTS"
    if freeze:
        # Dropping frames and letting fps repeat the last one is a freeze that needs no buffering.
        start += f",select='not(between(t,{freeze[0]:.2f},{freeze[1]:.2f}))',fps={FPS}"
    parts = [start + f",split={len(used) + 1}[g]" + "".join(f"[c{i}]" for i in range(len(used)))]
    for i, name in enumerate(used):
        c = base.cam_crop(boxes[name], W / cam_h)
        parts.append(f"[c{i}]crop={c['w']}:{c['h']}:{c['x']}:{c['y']},scale={W}:{cam_h}:flags=lanczos,"
                     f"setsar=1[k{i}]")
    last = "k0"
    for i, name in enumerate(used[1:], 1):
        spans = []
        for j, (at, n) in enumerate(timeline):
            if n == name:
                end = timeline[j + 1][0] if j + 1 < len(timeline) else dur + 1
                spans.append(f"gte(t,{at})*lt(t,{end})")
        parts.append(f"[{last}][k{i}]overlay=0:0:enable='{'+'.join(spans)}'[m{i}]")
        last = f"m{i}"
    cam_fx = zoom("1+max(0," + "+".join(cam_z) + ")", W, cam_h, 0.3) if cam_z else "null"
    game_fx = zoom("1+" + "+".join(game_z), W, game_h, 0.5) if game_z else "null"
    parts.append(f"[{last}]{cam_fx},setsar=1[cam]")
    parts.append(f"[g]crop={gc['w']}:{gc['h']}:{gc['x']}:{gc['y']},scale={W}:{game_h}:flags=lanczos,"
                 f"{game_fx},setsar=1[game]")
    if bar:
        parts.append(f"color=c=0x101014:s={W}x{bar}:r={FPS}:d={dur},setsar=1[bar]")
        parts.append("[bar][cam][game]vstack=inputs=3:shortest=1[stack]")
    else:
        parts.append("[game][cam]vstack=inputs=2[stack]")
    parts.append(f"[stack]trim=duration={dur},setpts=PTS-STARTPTS,format=yuv420p[body]")
    geo = {"game_centre": bar + cam_h + game_h // 2 if bar else game_h // 2, "game_crop": gc, "freeze": freeze}
    return parts, pos, geo


def outro_graph(kind, box, mood, length):
    """Reaction card from input 1: the facecam alone, full width, on a dark background.
    Always in colour: a grey "defeated" freeze framed him negatively (campaign rule, 2026-11-23)."""
    c = base.cam_crop(box, W / 810)
    f = (f"[1:v]fps={FPS},setpts=PTS-STARTPTS,crop={c['w']}:{c['h']}:{c['x']}:{c['y']},"
         f"scale={W}:810:flags=lanczos,")
    if kind == "reaction_replay":
        f += f"setpts=2*PTS,fps={FPS},tpad=stop_mode=clone:stop_duration=3,"
    else:
        f += "trim=end_frame=1,tpad=stop_mode=clone:stop_duration=3,"
    f += f"trim=duration={length},setpts=PTS-STARTPTS,"
    if kind == "reaction_zoom":
        f += zoom("1+0.45*min(1,it/0.25)+0.04*it", W, 810, 0.28) + ","
    elif kind == "reaction_freeze":
        f += zoom("1.1+0.12*it", W, 810, 0.28) + ","
    return f + f"pad={W}:{H}:0:730:color=0x101014,setsar=1,format=yuv420p[end]"


# ---------- text ----------

def ass_safe(text):
    return text.replace("\\", "/").replace("{", "(").replace("}", ")")


def write_text(path, ws, style, idx, pos, centre, dur, total, hook, outro_text, animated, scale=1.0):
    """One ASS file for the whole edit: captions, big hook and outro words."""
    base.write_ass(path, [] if animated else ws, style, pos["cap_y"], dur)
    if scale != 1.0:
        text = Path(path).read_text()
        text = re.sub(r"^(Style: Cap,[^,]+,)(\d+)", lambda m: m.group(1) + str(round(int(m.group(2)) * scale)),
                      text, flags=re.M)
        Path(path).write_text(text)
    font, _, hl, upper = base.CAPTION_STYLES[style]
    key = KEY_COLOUR[style]
    case = str.upper if upper else str
    lines = []

    def event(st, en, text, layer=0):
        lines.append(f"Dialogue: {layer},{base.ass_time(st)},{base.ass_time(en)},Cap,,0,0,0,,{text}")

    if animated:
        chunks = base.chunk_words(ws)
        for ci, ch in enumerate(chunks):
            chunk_end = ch[-1]["e"] + 0.35
            if ci + 1 < len(chunks):
                chunk_end = min(chunk_end, chunks[ci + 1][0]["s"])
            for wi, w in enumerate(ch):
                st = max(0, w["s"])
                en = min(ch[wi + 1]["s"] if wi + 1 < len(ch) else chunk_end, dur)
                if en <= st:
                    continue
                parts = []
                for wj, x in enumerate(ch):
                    word = ass_safe(case(base.mask(x["w"])))
                    is_key = bool(KEY_WORDS.match(x["w"].strip(" ?!.,'\"")))
                    if wj == wi:  # spoken word pops, then settles
                        size = 112 if is_key else 106
                        tags = (f"\\c{key if is_key else hl}\\fscx125\\fscy125"
                                f"\\t(0,110,\\fscx{size}\\fscy{size})")
                    elif wj > wi:  # not spoken yet
                        tags = "\\c&H00D0D0D0&\\alpha&H70&"
                    else:
                        tags = f"\\c{key}" if is_key else ""
                    parts.append(f"{{{tags}}}{word}{{\\r}}" if tags else word)
                event(st, en, f"{{\\pos({W // 2},{pos['cap_y']})}}" + " ".join(parts))

    if hook:
        shown, size, width = base.mask(hook).upper(), 124, 14
        if len(base.wrap(shown, width)) > 3:  # long hook: smaller, so it never becomes a wall of text
            size, width = 100, 18
        # Narrowest width with the same number of lines, so no word is left alone on the last one.
        n = len(base.wrap(shown, width))
        while width > 6 and len(base.wrap(shown, width - 1)) == n:
            width -= 1
        body = "\\N".join(ass_safe(x) for x in base.wrap(shown, width))
        x, y = W // 2, centre
        move, colour = [
            (f"\\pos({x},{y})\\fscx116\\fscy116\\t(0,160,\\fscx100\\fscy100)", "&H00FFFFFF&"),  # pop
            (f"\\move({x},{y + 90},{x},{y},0,180)", hl),  # slide up
            (f"\\pos({x},{y})\\frz3\\fscx112\\fscy112\\t(0,120,\\fscx100\\fscy100)", key),  # tilted stamp
        ][idx % 3]
        event(0, min(HOOK_S, dur), f"{{{move}\\fn{font}\\fs{size}\\bord10\\shad4\\c{colour}}}{body}", 2)

    if outro_text and total > dur:
        body = "\\N".join(ass_safe(x) for x in base.wrap(base.mask(outro_text).upper(), 14))
        event(dur, total, f"{{\\pos({W // 2},500)\\fn{font}\\fs124\\bord8\\c{hl}"
                          f"\\fscx130\\fscy130\\t(0,150,\\fscx100\\fscy100)}}{body}", 2)
    with open(path, "a") as f:
        f.writelines(line + "\n" for line in lines)


def label_filters(td, hook, hook_from, layout, pos, dur):
    """Small hook that stays up after the big one, then the Ad label. The Ad label is always last."""
    filters = []
    bold = base.esc_path(font_file("Arial Black"))
    if hook:
        shown = base.mask(hook) if layout == "game_top_tall" else base.mask(hook).upper()
        (td / "hook.txt").write_text("\n".join(base.wrap_balanced(shown, 18)))
        font = base.esc_path(font_file("Impact" if layout == "game_top" else "Arial Black"))
        box = "box=1:boxcolor=black@0.72:boxborderw=22" if pos["hook_box"] else "borderw=0"
        filters.append(f"drawtext=fontfile='{font}':textfile='{base.esc_path(td / 'hook.txt')}':expansion=none:"
                       f"fontsize=60:line_spacing=10:text_align=C:fontcolor=white:x=(w-tw)/2:y={pos['hook_y']}:"
                       f"{box}:enable='gte(t,{hook_from})*lt(t,{dur})'")
    x, y = pos["ad"]
    ad = f"drawtext=fontfile='{bold}':fontcolor=black:x={x}:y={y}:box=1"
    filters.append(f"{ad}:text='AD':fontsize=58:boxcolor=0xFFD400:boxborderw=14:enable='lt(t,3)'")
    filters.append(f"{ad}:text='Ad':fontsize=38:boxcolor=0xFFD400@0.9:boxborderw=10:enable='gte(t,3)'")
    return filters


# ---------- render ----------

def render_edited(d, cfg, r, c, idx, trans_by_rank, out_dir, without=(), overrides=None, out_path=None):
    """Render one edited clip. overrides is a version spec from the review page (see apply_overrides);
    out_path replaces the default output/edited/<name>.mp4."""
    rank = c["candidate_rank"]
    clip_id = f"{r['campaign']}_{r['vod_id']}_c{rank:02d}"
    c, st, warns = apply_overrides(c, overrides or {}, trans_by_rank[rank], cfg)
    without = sorted(set(without) | set(st["without"]))
    name = clip_id + "".join(f"_no-{x}" for x in without)
    on, flags = allowed_layers(cfg, without)
    fields, field_warns = rankings.edit_fields(c)
    warns += field_warns
    for w in warns:
        print(f"  WARN {w}")
    layout, style = base.rotation(idx)
    layout, style = st["layout"] or layout, st["caption_style"] or style
    mood = fields["mood"]
    hook = (base.hook_for(clip_id, c) if st["hook"] is None else st["hook"]) if flags["allow_overlays"] else ""

    seg, t0 = base.fetch_segment(d, r["vod_id"], c)
    sw, sh = base.probe_size(seg)
    lay = cfg["layout"]
    scale = sw / lay["source_resolution"][0]
    boxes = {k: base.scale_box(v, scale)
             for k, v in {**lay["facecam"], **lay.get("facecam_switch", {})}.items()}
    overlay = lay["exclude"]["chat_overlay"]
    chat_bottom = int(round((overlay["y"] + overlay["h"]) * scale))

    ss, dur = c["start"] - t0, round(c["end"] - c["start"], 2)
    timeline = cam_timeline(seg, ss, dur, boxes)
    punch = None if fields["punchline_ts"] is None else round(fields["punchline_ts"] - c["start"], 2)

    outro, outro_len = "none", 0.0
    room = cfg.get("rules", {}).get("max_clip_s", 60) - dur
    if "outro" in on and fields["outro_type"] != "none" and room >= 1.5:
        outro = fields["outro_type"]
        outro_len = round(min(OUTRO_S[outro], room), 2)
    total = round(dur + outro_len, 2)

    parts, pos, geo = body_graph(layout, boxes, timeline, chat_bottom, sw, sh, dur, punch, mood, on,
                                 bool(c.get("violence_early_risk")), st["punch_style"])
    inputs = ["-ss", f"{ss:.3f}", "-t", f"{dur}", "-i", str(seg)]
    outro_text = ""
    if outro != "none":
        react = fields["reaction_ts"] - c["start"]
        length = 0.3
        if outro == "reaction_replay":
            react, length = min(max(react - 0.4, 0), dur - 1.1), 1.1
        inputs += ["-ss", f"{ss + react:.3f}", "-t", f"{length}", "-i", str(seg)]
        parts.append(outro_graph(outro, boxes[cam_at(timeline, react)], mood, outro_len))
        parts.append("[body][end]concat=n=2:v=1:a=0[joined]")
        outro_text = fields["outro_text"] or OUTRO_WORDS.get(mood, "")
    else:
        parts.append("[body]null[joined]")

    sounds = []
    if "sfx" in on:
        cues = [("hook", 0.0, "hook" in on),
                ("punchline", punch, punch is not None and "punchline" in on),
                ("outro", dur, outro != "none")]
        for role, at, wanted in cues:
            s = pick_sound(role, mood, idx) if wanted else None
            if s:
                sounds.append({"role": role, "at": at, "id": s["id"], "file": s["file"],
                               "gain": round(s["gain"] * st["sfx_gain"], 3)})

    # Loudness pass 1 on the speech and game audio, so the sounds sit on top of a -14 LUFS bed.
    err = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-ss", f"{ss:.3f}", "-i", str(seg), "-t", f"{dur}",
                          "-vn", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    ln = json.loads(err[err.rindex("{"):err.rindex("}") + 1])
    parts.append(f"[0:a]loudnorm=I=-14:TP=-1.5:LRA=11:measured_I={ln['input_i']}:measured_TP={ln['input_tp']}:"
                 f"measured_LRA={ln['input_lra']}:measured_thresh={ln['input_thresh']}:"
                 f"offset={ln['target_offset']}:linear=true,aresample=48000,aformat=channel_layouts=stereo,"
                 f"afade=t=out:st={dur - 0.08:.2f}:d=0.08,apad=whole_dur={total}[bed]")
    first = 2 if outro != "none" else 1
    for i, s in enumerate(sounds):
        inputs += ["-i", str(ROOT / "assets" / s["file"])]
        parts.append(f"[{first + i}:a]aresample=48000,aformat=channel_layouts=stereo,volume={s['gain']},"
                     f"adelay={int(s['at'] * 1000)}:all=1[s{i}]")
    parts.append("[bed]" + "".join(f"[s{i}]" for i in range(len(sounds)))
                 + f"amix=inputs={len(sounds) + 1}:normalize=0:duration=first,"
                   f"aresample=192000,alimiter=limit=0.8:level=false:latency=true,aresample=48000,"
                   f"atrim=duration={total}[a]")  # limit at 4x rate so true peaks are caught

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        big_hook = hook if "hook" in on else ""
        write_text(td / "text.ass", base.words_in(trans_by_rank[rank], c["start"], c["end"]), style, idx, pos,
                   geo["game_centre"], dur, total, big_hook, outro_text, "captions" in on, st["caption_scale"])
        labels = label_filters(td, hook, HOOK_S if big_hook else 0, layout, pos, dur)
        parts.append(f"[joined]subtitles='{base.esc_path(td / 'text.ass')}',"
                     + ",".join(labels) + ",format=yuv420p[v]")
        out = Path(out_path) if out_path else out_dir / f"{name}.mp4"
        tmp = out.with_name(f".{out.stem}.part.mp4")  # swap in only when the render succeeded
        base.run([FFMPEG, "-v", "error", "-y", *inputs, "-filter_complex", ";".join(parts),
                  "-map", "[v]", "-map", "[a]", "-t", f"{total}", "-r", str(FPS),
                  "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-profile:v", "high",
                  "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-movflags", "+faststart", str(tmp)])
        tmp.replace(out)

    meta = {
        "clip_id": clip_id, "render_id": name, "variant": "edited",
        "campaign": r["campaign"], "vod_id": r["vod_id"], "candidate_rank": rank,
        "vod_start": c["start"], "vod_end": c["end"], "body_s": dur, "duration_s": total,
        "score": c["score"], "summary": c["summary"], "hook_used": hook, "hooks": c["hooks"],
        "layout": layout, "caption_style": style, "facecam_timeline": timeline,
        "game_crop": geo["game_crop"], "layers": sorted(on), "campaign_flags": flags,
        "campaign_rules_verified": cfg.get("edit", {}).get("rules_verified") is True,
        "mood": mood, "punchline_s": punch, "freeze_s": geo["freeze"],
        "outro_type": outro, "outro_text": outro_text,
        "sounds": [{k: s[k] for k in ("role", "at", "id")} for s in sounds],
        "violence_early_risk": c.get("violence_early_risk", False),
        "platforms_ok": base.platforms_for(cfg, c), "status": "rendered, not reviewed",
    }
    meta.update(caption_scale=st["caption_scale"], sfx_gain=st["sfx_gain"], punch_style=st["punch_style"],
                spec=overrides or {}, warnings=warns)
    sidecar = out.with_name(out.stem + "_render.json") if out_path else out.with_suffix(".json")
    sidecar.write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    cams = ", ".join(f"{n} from {t}s" for t, n in timeline)
    print(f"#{rank}: {total}s, {layout}, cam {cams}, punch {punch}, outro {outro}, "
          f"sounds {', '.join(s['id'] for s in sounds) or 'none'}\n  -> {out}", flush=True)
    return out
