"""Load/save configuration for Desktop Grid.

Config lives in %APPDATA%\\DesktopGrid\\config.json:
{
  "zones": [{"name": "Work", "x": 100, "y": 80, "w": 400, "h": 300, "color": "#4FC3F7"}],
  "layouts": {"2560x1440@1": {"Icon Name": [x, y], ...}},
  "auto_restore": true
}
Zone coords are virtual-screen pixels.
"""

import json
import os

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "DesktopGrid")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULT = {
    "zones": [],
    "layouts": {},
    "auto_restore": True,
    "custom_colors": [],
    # Optional custom label font: path to a .ttf/.otf and the family name it
    # registers as. Leave empty to use Segoe UI (covers Georgian natively).
    "font_file": "",
    "font_family": "",
}

ZONE_COLORS = ["#4FC3F7", "#81C784", "#FFB74D", "#BA68C8", "#F06292", "#4DB6AC", "#FFF176", "#90A4AE"]


def load():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    merged = dict(DEFAULT)
    merged.update(data)
    return merged


def save(data):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)


def next_zone_color(zones):
    return ZONE_COLORS[len(zones) % len(ZONE_COLORS)]
