"""Build the starter sound effects in assets/sfx/ and their entries in assets/manifest.json.

Run:
  python pipeline/make_assets.py

Every sound is plain maths (sine waves, filtered noise). No samples, downloads or AI, so there is
nothing to license. Entries you add to the manifest by hand are kept when this is re-run.
The cloud workflow runs this before rendering, so the .wav files never need committing.
"""
import json
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.tools import ROOT  # noqa: E402

SR = 48000
rng = np.random.default_rng(6)


def secs(d):
    return np.arange(int(SR * d)) / SR


def env(d, attack=0.005, decay=5.0):
    t = secs(d)
    return np.minimum(t / attack, 1) * np.exp(-decay * t) * np.minimum((d - t) / 0.02, 1)


def osc(freq, d):
    return np.sin(2 * np.pi * np.cumsum(np.broadcast_to(freq, secs(d).shape)) / SR)


def noise(d, lo, hi):
    n = len(secs(d))
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / SR)
    spec[(f < lo) | (f > hi)] = 0
    x = np.fft.irfft(spec, n)
    return x / np.abs(x).max()


def horn(freq, d, partials=8):
    return sum(osc(freq * k, d) / k for k in range(1, partials + 1))


def place(total, *parts):
    out = np.zeros(int(SR * total))
    for at, x in parts:
        i = int(SR * at)
        x = x[: len(out) - i]
        out[i:i + len(x)] += x
    return out


def whoosh(lo, hi, d):
    return noise(d, lo, hi) * np.sin(np.pi * secs(d) / d) ** 3


def boom():
    t = secs(1.0)
    body = np.tanh(3 * osc(45 + 110 * np.exp(-t * 14), 1.0) * env(1.0, 0.003, 4.5))
    return place(1.0, (0, body), (0, 0.5 * noise(0.12, 200, 3000) * env(0.12, 0.002, 30)))


def trombone():
    parts = []
    for i, f in enumerate((233, 220, 208, 196)):
        d = 0.75 if i == 3 else 0.28
        t = secs(d)
        wobble = f * (1 + (0.03 if i == 3 else 0.005) * np.sin(2 * np.pi * 6 * t))
        parts.append((i * 0.28, horn(wobble, d) * env(d, 0.03, 1.5)))
    return place(1.65, *parts)


def scratch():
    d = 0.5
    t = secs(d)
    sweep = horn(500 + 380 * np.sin(2 * np.pi * 5 * t), d, 5)
    return (0.4 * sweep + noise(d, 900, 5000)) * env(d, 0.005, 4) * np.abs(np.sin(2 * np.pi * 5 * t))


def rimshot():
    tom = lambda f: osc(f, 0.2) * env(0.2, 0.002, 18)  # noqa: E731
    return place(1.1, (0, tom(190)), (0.15, tom(130)),
                 (0.36, 0.7 * noise(0.7, 3000, 14000) * env(0.7, 0.002, 6)))


def boing():
    t = secs(0.6)
    return osc(170 + 260 * t + 90 * np.sin(2 * np.pi * 13 * t) * np.exp(-3 * t), 0.6) * env(0.6, 0.01, 4)


def sting():
    return place(1.0, *[(0, horn(f, 1.0, 6) * env(1.0, 0.004, 4)) for f in (220, 311, 440, 466)])


def fanfare():
    parts = [(i * 0.11, horn(f, 0.16) * env(0.16, 0.01, 6)) for i, f in enumerate((392, 523, 659))]
    return place(1.1, *parts, (0.33, horn(784, 0.7) * env(0.7, 0.01, 3)))


def airhorn():
    blast = lambda d: sum(horn(f, d, 10) for f in (466, 470, 588)) * env(d, 0.01, 0.8)  # noqa: E731
    return place(1.0, (0, blast(0.17)), (0.23, blast(0.17)), (0.46, blast(0.5)))


def chime():
    return place(1.3, *[(i * 0.12, (osc(f, 0.9) + 0.2 * osc(2 * f, 0.9)) * env(0.9, 0.005, 5))
                        for i, f in enumerate((784, 988, 1175, 1568))])


def ding():
    return (osc(1318, 0.9) + 0.3 * osc(2637, 0.9)) * env(0.9, 0.003, 6)


# id: (builder, role, moods, gain when mixed)
SOUNDS = {
    "whoosh_soft": (lambda: whoosh(300, 2500, 0.5), "hook", [], 0.70),
    "whoosh_sharp": (lambda: whoosh(1200, 8000, 0.35), "hook", [], 0.65),
    "whoosh_low": (lambda: whoosh(100, 900, 0.6), "hook", [], 0.75),
    "boom": (boom, "punchline", ["fail", "shock"], 0.55),
    "record_scratch": (scratch, "punchline", ["fail", "funny"], 0.80),
    "boing": (boing, "punchline", ["funny"], 0.65),
    "sting": (sting, "punchline", ["shock"], 0.60),
    "fanfare": (fanfare, "punchline", ["hype"], 0.65),
    "ding": (ding, "punchline", ["wholesome"], 0.75),
    # Not used: mocks the streamer, against the AsianJeff "never show him negatively" rule (2026-11-23).
    "sad_trombone": (trombone, "outro", [], 0.50),
    "rimshot": (rimshot, "outro", ["funny"], 0.60),
    "outro_boom": (boom, "outro", ["shock"], 0.60),
    "airhorn": (airhorn, "outro", ["hype"], 0.60),
    "chime": (chime, "outro", ["wholesome", "fail"], 0.50),
}


def main():
    path = ROOT / "assets" / "manifest.json"
    (ROOT / "assets" / "sfx").mkdir(parents=True, exist_ok=True)
    old = json.loads(path.read_text())["assets"] if path.exists() else []
    items = []
    for name, (build, role, moods, gain) in SOUNDS.items():
        x = build()
        x = x / np.abs(x).max() * 0.89
        rel = f"sfx/{name}.wav"
        with wave.open(str(ROOT / "assets" / rel), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes((x * 32767).astype("<i2").tobytes())
        items.append({"id": name, "kind": "sfx", "file": rel, "role": role, "moods": moods, "gain": gain,
                      "licence": "own-work", "source": "synthesised by pipeline/make_assets.py"})
    items.append({"id": "streamer_reaction", "kind": "reaction", "file": None,
                  "moods": ["funny", "fail", "shock", "hype", "wholesome"], "licence": "campaign-footage",
                  "source": "the named streamer's facecam, cut from the same clip at render time"})
    ours = {i["id"] for i in items}
    items += [i for i in old if i["id"] not in ours and i.get("kind") != "font"]
    path.write_text(json.dumps({"assets": items}, indent=1) + "\n")
    print(f"{len(SOUNDS)} sounds written, {len(items)} assets in {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
