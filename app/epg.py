import gzip
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional


def _parse_datetime(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        s = s.strip()
        parts = s.split()
        dt_part = parts[0]
        tz_part = parts[1] if len(parts) > 1 else "+0000"

        dt = datetime.strptime(dt_part[:14], "%Y%m%d%H%M%S")

        sign = 1 if tz_part.startswith("+") else -1
        tz_str = tz_part[1:] if tz_part and tz_part[0] in "+-" else tz_part
        hours = int(tz_str[:2])
        minutes = int(tz_str[2:4]) if len(tz_str) >= 4 else 0
        offset = timezone(timedelta(hours=sign * hours, minutes=sign * minutes))

        return dt.replace(tzinfo=offset)
    except Exception:
        return None


def parse_epg_xml(content: bytes) -> Dict[str, List[Dict]]:
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)

    text = content.decode("utf-8", errors="ignore")
    text = re.sub(r'xmlns(:\w+)?="[^"]+"', "", text)
    text = re.sub(r"<(\w+):", r"<", text)
    text = re.sub(r"</(\w+):", r"</", text)

    result: Dict[str, List[Dict]] = {}

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return _parse_epg_manual(text)

    for programme in root.iter("programme"):
        tvg_id = programme.get("channel", "").strip()
        start = _parse_datetime(programme.get("start", ""))
        stop = _parse_datetime(programme.get("stop", ""))
        title_el = programme.find("title")
        title = title_el.text.strip() if title_el is not None and title_el.text else ""

        if not tvg_id or not start or not stop:
            continue

        result.setdefault(tvg_id, []).append({
            "titulo": title,
            "inicio": start.isoformat(),
            "fim": stop.isoformat(),
        })

    return result


def _parse_epg_manual(text: str) -> Dict[str, List[Dict]]:
    result: Dict[str, List[Dict]] = {}
    pattern = re.compile(
        r'<programme\s+start="([^"]+)"\s+stop="([^"]+)"\s+channel="([^"]+)"[^>]*>.*?<title[^>]*>([^<]*)</title>',
        re.DOTALL,
    )
    for m in pattern.finditer(text):
        start = _parse_datetime(m.group(1))
        stop = _parse_datetime(m.group(2))
        tvg_id = m.group(3).strip()
        title = m.group(4).strip()
        if not start or not stop or not tvg_id:
            continue
        result.setdefault(tvg_id, []).append({
            "titulo": title,
            "inicio": start.isoformat(),
            "fim": stop.isoformat(),
        })
    return result


def get_current_and_next(programs: List[Dict], now: Optional[datetime] = None) -> Dict:
    if now is None:
        now = datetime.now(timezone.utc)

    def _to_dt(iso: str) -> datetime:
        return datetime.fromisoformat(iso)

    programs_sorted = sorted(programs, key=lambda p: p["inicio"])

    atual = None
    proximo = None

    for i, prog in enumerate(programs_sorted):
        try:
            inicio = _to_dt(prog["inicio"])
            fim = _to_dt(prog["fim"])
        except Exception:
            continue

        if inicio <= now <= fim:
            atual = prog
            if i + 1 < len(programs_sorted):
                proximo = programs_sorted[i + 1]
            break
        elif inicio > now:
            proximo = prog
            break

    return {"atual": atual, "proximo": proximo}
