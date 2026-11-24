"""Run state shared between GitHub Actions runs.

Lives in ./state/, which the workflows check out from the `state` branch and push back after
each run. It holds only small JSON files: which VODs are done, the posting queue and the post log.
Videos never go here (they travel as workflow artifacts). Tokens that must be refreshed between
runs are stored encrypted (see pipeline/vault.py); nothing secret is ever written in plain text.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from pipeline.tools import ROOT

STATE = Path(os.environ.get("CLIPPER_STATE_DIR", ROOT / "state"))


def _path(name):
    STATE.mkdir(parents=True, exist_ok=True)
    return STATE / name


def load(name, default):
    p = _path(name)
    return json.loads(p.read_text()) if p.exists() else default


def save(name, data):
    p = _path(name)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    tmp.replace(p)


def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt=None):
    return (dt or now()).isoformat()


def parse(s):
    return datetime.fromisoformat(s)


def log(event, **fields):
    """Append one line to state/events.jsonl (the run history a status check can read)."""
    with open(_path("events.jsonl"), "a") as f:
        f.write(json.dumps({"at": iso(), "event": event, **fields}, ensure_ascii=False) + "\n")
