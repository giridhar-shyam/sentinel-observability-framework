"""Load config.properties, then let env vars override."""
import os
from pathlib import Path

_ROOT = Path(__file__).parent.parent  # project root (one level up from utils/)
_PROPS_FILE = _ROOT / "config.properties"


def _load():
    cfg = {}
    if _PROPS_FILE.exists():
        for line in _PROPS_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
    # env vars win
    for k in list(cfg):
        cfg[k] = os.environ.get(k, cfg[k])
    return cfg


config = _load()
