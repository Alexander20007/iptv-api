import re
from typing import List, Dict, Optional

ATTR_RE = re.compile(r'([a-zA-Z0-9\-_]+)="([^"]*)"')

# Marcas conhecidas (pra inferir grupo quando o M3U não tem group-title)
KNOWN_BRANDS = [
    "globo", "record", "sbt", "band", "redetv", "rede tv",
    "espn", "sportv", "premiere", "combate", "ufc",
    "fox", "discovery", "history", "hbo", "telecine", "megapix",
    "warner", "universal", "sony", "axn", "amc", "tnt", "space",
    "cartoon", "nick", "disney", "gloob",
    "cnn", "band news", "globonews", "jovem pan",
    "multishow", "bis", "gnt", "off", "tlc",
]


def _infer_group_from_name(name: str) -> Optional[str]:
    if not name:
        return None
    name_lower = name.lower().strip()
    for brand in KNOWN_BRANDS:
        if name_lower.startswith(brand + " ") or name_lower == brand:
            return brand.title()
    first_word = name.split()[0] if name.split() else None
    if first_word and len(first_word) >= 3 and first_word.isalpha():
        return first_word.capitalize()
    return None


def _is_vod(group: str, name: str) -> bool:
    """
    Detecta se o canal é VOD (série, filme, episódio).
    Retorna True se for VOD (não deve ser salvo).
    """
    g = (group or "").lower().strip()
    n = (name or "").lower().strip()

    # Grupos VOD
    if re.match(r'^(serie|série|filme|movie|novela|show|anime|animes|documentario|documentário)\s*\|', g):
        return True

    # Nomes tipo episódio
    if re.search(r'\bs\d+\s*e\d+\b', n):
        return True
    if re.search(r'\bt\d+\s*ep?\d+\b', n):
        return True
    if re.search(r'\b\d{1,2}x\d{1,2}\b', n):
        return True
    if re.search(r'temporada\s*\d+', n):
        return True

    return False


def parse_m3u(content: str, skip_vod: bool = True) -> List[Dict]:
    """
    Parseia o M3U.
    Se skip_vod=True, remove canais de VOD (séries, filmes, episódios).
    """
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
            current["tvg_id"] = attrs.get("tvg-id") or attrs.get("tvg-name")

            if "," in line:
                current["name"] = line.split(",", 1)[1].strip()

            if not current["group"]:
                inferred = _infer_group_from_name(current["name"])
                current["group"] = inferred or "Outros"

        elif line.startswith("#"):
            continue

        else:
            if current is not None:
                current["url"] = line

                # Filtra VOD, se pedido
                if not (skip_vod and _is_vod(current.get("group"), current.get("name"))):
                    channels.append(current)

                current = None

    return channels
