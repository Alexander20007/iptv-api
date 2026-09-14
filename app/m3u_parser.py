import re
from typing import List, Dict

ATTR_RE = re.compile(r'([a-zA-Z0-9\-_]+)="([^"]*)"')

def parse_m3u(content: str) -> List[Dict]:
    channels = []
    current = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line: continue
        if line.startswith("#EXTINF"):
            current = {"name": "Unknown", "url": "", "logo": None, "group": None}
            if "," in line: current["name"] = line.split(",", 1)[1].strip()
            attrs = dict(ATTR_RE.findall(line))
            current["logo"] = attrs.get("tvg-logo")
            current["group"] = attrs.get("group-title")
        elif not line.startswith("#"):
            if current:
                current["url"] = line
                channels.append(current)
                current = {}
    return channels
