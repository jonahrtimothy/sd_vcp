"""
Loads config.yaml (project root) once and caches it. Every tunable that
would otherwise be hardcoded in a module (watchlist, detection thresholds,
confluence bonus magnitudes) should be read from here instead, per
SYSTEM_BUILD_PROMPT.md Section 15.
"""

from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

_cached_config: dict[str, Any] | None = None


def load_config(force_reload: bool = False) -> dict[str, Any]:
    global _cached_config
    if _cached_config is None or force_reload:
        if not CONFIG_PATH.exists():
            raise FileNotFoundError(
                f"config.yaml not found at {CONFIG_PATH}. This file holds "
                f"the watchlist and all tunable thresholds -- see "
                f"SYSTEM_BUILD_PROMPT.md Section 5 for the expected shape."
            )
        with open(CONFIG_PATH, encoding="utf-8") as f:
            _cached_config = yaml.safe_load(f)
    return _cached_config


def update_watchlist(new_watchlist: list[str]) -> None:
    """
    Rewrites just the `watchlist:` block in config.yaml, leaving every
    other line (including comments) untouched. A full yaml.safe_dump of
    the whole config would silently drop all the comments documenting
    each section -- this does a targeted text replacement instead.
    """
    import re

    text = CONFIG_PATH.read_text(encoding="utf-8")
    new_block = "watchlist:\n" + "\n".join(f"  - {s}" for s in new_watchlist) + "\n"

    pattern = re.compile(r"^watchlist:\n(?:  - .*\n)*", re.MULTILINE)
    if not pattern.search(text):
        raise ValueError("Could not find a `watchlist:` block in config.yaml to replace.")

    new_text = pattern.sub(new_block, text, count=1)
    CONFIG_PATH.write_text(new_text, encoding="utf-8")

    global _cached_config
    _cached_config = None  # force reload next time load_config() is called


if __name__ == "__main__":
    cfg = load_config()
    print(f"Loaded config.yaml from: {CONFIG_PATH}")
    print(f"Watchlist ({len(cfg.get('watchlist', []))} symbols): {cfg.get('watchlist')}")
    print(f"Detection: {cfg.get('detection')}")
    print(f"Confluence: {cfg.get('confluence')}")
    print(f"Fundamentals: {cfg.get('fundamentals')}")
