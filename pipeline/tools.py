"""Paths to external tools. Change them here, not in other modules.

Works on Arjun's Mac (Homebrew ffmpeg-full, mlx-whisper) and on Linux / GitHub Actions
(system ffmpeg, faster-whisper). Nothing else in the pipeline needs to know which.
"""
import os
import platform
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IS_MAC = platform.system() == "Darwin"
IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"

# ffmpeg-full (keg-only in Homebrew) has libass + drawtext for captions and hook text.
# On Linux the distro ffmpeg already has them.
_FULL = Path("/opt/homebrew/opt/ffmpeg-full/bin")
FFMPEG = str(_FULL / "ffmpeg") if (_FULL / "ffmpeg").exists() else shutil.which("ffmpeg")
FFPROBE = str(_FULL / "ffprobe") if (_FULL / "ffprobe").exists() else shutil.which("ffprobe")
YTDLP = shutil.which("yt-dlp")
TWITCH_DL = str(ROOT / "bin" / "TwitchDownloaderCLI")

# Fonts. Mac uses its system fonts. On Linux the workflow installs Microsoft core fonts
# (Arial Black, Impact) and Montserrat, which stands in for Futura.
_FONT_FILES = {
    "Arial Black": ["/System/Library/Fonts/Supplemental/Arial Black.ttf",
                    "/usr/share/fonts/truetype/msttcorefonts/Arial_Black.ttf",
                    "/usr/share/fonts/truetype/msttcorefonts/ariblk.ttf"],
    "Impact": ["/System/Library/Fonts/Supplemental/Impact.ttf",
               "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
               "/usr/share/fonts/truetype/msttcorefonts/impact.ttf"],
}
_FALLBACK_FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                   "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]


def font_file(name):
    """Path of a font file for ffmpeg drawtext, falling back to any bold sans that exists."""
    for p in _FONT_FILES.get(name, []) + _FALLBACK_FONTS:
        if Path(p).exists():
            return Path(p)
    raise FileNotFoundError(f"no font file for {name!r}; install ttf-mscorefonts-installer or fonts-dejavu")


def font_family(name):
    """Family name for libass captions. libass falls back by itself, but Futura has no free
    lookalike on Linux, so name a close one explicitly."""
    if not IS_MAC and name == "Futura":
        return "Montserrat ExtraBold"
    return name


# Whisper backend: mlx on Apple Silicon, faster-whisper (CPU, int8) everywhere else.
WHISPER_BACKEND = os.environ.get("WHISPER_BACKEND") or ("mlx" if IS_MAC and platform.machine() == "arm64"
                                                        else "faster")
WHISPER_MODEL = {
    "mlx": "mlx-community/whisper-large-v3-turbo",
    # turbo on CPU is about 1x real time on a GitHub runner; small.en is ~4x faster and fine for ranking.
    "faster": os.environ.get("WHISPER_MODEL", "small.en"),
}[WHISPER_BACKEND]
