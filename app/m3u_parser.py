import re
from typing import List, Dict

ATTR_RE = re.compile(r'([a-zA-Z0-9\-_]+)="([^"]*)"')


def parse_m3u(content: str) -> List[Dict]:
    channels = []
    current = None

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("#EXTINF"):
            current = {
                "name": "Unknown",
                "url": "",
                "logo": None,
                "group": None,
                "tvg_id": None,
            }
            attrs = dict(ATTR_RE.findall(line))
            current["logo"] = attrs.get("tvg-logo")
            current["group"] = attrs.get("group-title")
            current["tvg_id"] = attrs.get("tvg-id")

            if "," in line:
                current["name"] = line.split(",", 1)[1].strip()

        elif line.startswith("#"):
            continue

        else:
            if current is not None:
                current["url"] = line
                channels.append(current)
                current = None

    return channels
