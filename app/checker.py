import asyncio
import json
import re
import subprocess
import time
import httpx
from typing import Dict, Optional
from urllib.parse import urljoin

USER_AGENT = "Mozilla/5.0 (IPTVChecker/1.0)"


def _looks_like_m3u8(text: str) -> bool:
    if not text:
        return False
    return "#EXTM3U" in text[:2000]


def _m3u8_has_segments(text: str) -> bool:
    if not text:
        return False
    if "#EXT-X-STREAM-INF" in text:
        return True
    lines = text.splitlines()
    has_extinf = False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            has_extinf = True
        elif has_extinf and not line.startswith("#"):
            return True
        elif line.startswith("#EXT-X-ENDLIST"):
            return has_extinf
    if re.search(r"https?://[^\s]+\.(ts|m4s|mp4|aac)", text):
        return True
    return False


def _get_first_variant_url(master_text: str, base_url: str) -> Optional[str]:
    lines = master_text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            for j in range(i + 1, len(lines)):
                nxt = lines[j].strip()
                if nxt and not nxt.startswith("#"):
                    return urljoin(base_url, nxt)
    return None


async def http_check(url: str, timeout: int, retries: int) -> Dict:
    last_error = None
    for attempt in range(retries + 1):
        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
                verify=False,
            ) as client:
                async with client.stream("GET", url) as resp:
                    elapsed = (time.perf_counter() - start) * 1000
                    if resp.status_code not in (200, 206):
                        last_error = f"HTTP {resp.status_code}"
                        continue
                    content_type = (resp.headers.get("content-type") or "").lower()
                    if "text/html" in content_type:
                        return {
                            "ok": False,
                            "http_status": resp.status_code,
                            "response_time_ms": round(elapsed, 2),
                            "error": "HTML em vez de stream",
                        }
                    return {
                        "ok": True,
                        "http_status": resp.status_code,
                        "response_time_ms": round(elapsed, 2),
                    }
        except Exception as e:
            last_error = str(e)
        await asyncio.sleep(0.5 * (attempt + 1))

    return {
        "ok": False,
        "http_status": None,
        "response_time_ms": None,
        "error": last_error,
    }


async def m3u8_deep_check(url: str, timeout: int) -> Dict:
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            verify=False,
        ) as client:
            r = await client.get(url)

            if r.status_code not in (200, 206):
                return {"ok": False, "error": f"HTTP {r.status_code}"}

            content_type = (r.headers.get("content-type") or "").lower()

            if any(ct in content_type for ct in ("video/", "audio/", "application/octet-stream")):
                if len(r.content) > 1000:
                    return {"ok": True, "type": "direct_stream"}
                return {"ok": False, "error": "Stream muito curto"}

            text = r.text
            if not _looks_like_m3u8(text):
                return {"ok": False, "error": "Não é M3U8 válido"}

            if not _m3u8_has_segments(text):
                return {"ok": False, "error": "M3U8 sem segmentos"}

            if "#EXT-X-STREAM-INF" in text:
                variant_url = _get_first_variant_url(text, str(r.url))
                if variant_url:
                    r2 = await client.get(variant_url)
                    if r2.status_code in (200, 206):
                        if _m3u8_has_segments(r2.text):
                            return {"ok": True, "type": "master_with_segments"}
                    return {"ok": False, "error": "Variante do master falhou"}

            return {"ok": True, "type": "media_playlist"}

    except httpx.TimeoutException:
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:150]}


def ffprobe_check(url: str, timeout: int) -> Dict:
    cmd = [
        "ffprobe", "-v", "error",
        "-user_agent", USER_AGENT,
        "-timeout", str(timeout * 1_000_000),
        "-i", url,
        "-show_entries", "stream=codec_type,codec_name,width,height",
        "-of", "json",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout + 5,
        )
        if proc.returncode != 0:
            return {"ok": False, "error": proc.stderr.strip()[:200]}
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams", [])
        if not streams:
            return {"ok": False, "error": "nenhum stream detectado"}
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        resolution = None
        if video and video.get("width") and video.get("height"):
            resolution = f"{video['width']}x{video['height']}"
        return {
            "ok": True,
            "video_codec": video.get("codec_name") if video else None,
            "audio_codec": audio.get("codec_name") if audio else None,
            "resolution": resolution,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ffprobe timeout"}
    except FileNotFoundError:
        return {"ok": False, "error": "ffprobe nao instalado"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def check_channel(
    channel: Dict,
    timeout: int,
    retries: int,
    mode: str,
    sem: asyncio.Semaphore,
) -> Dict:
    """
    mode:
      - "fast":   só HTTP check
      - "deep":   HTTP + M3U8 deep check (recomendado)
      - "ffprobe": HTTP + ffprobe (mais lento, mais preciso)
    """
    async with sem:
        result = {
            "name": channel["name"],
            "url": channel["url"],
            "logo": channel.get("logo"),
            "group": channel.get("group"),
            "status": "offline",
            "http_status": None,
            "response_time_ms": None,
            "video_codec": None,
            "audio_codec": None,
            "resolution": None,
            "error": None,
        }

        if not channel["url"]:
            result["error"] = "sem URL"
            return result

        http_res = await http_check(channel["url"], timeout, retries)
        result["http_status"] = http_res.get("http_status")
        result["response_time_ms"] = http_res.get("response_time_ms")

        if not http_res["ok"]:
            result["status"] = "error" if http_res.get("error") else "offline"
            result["error"] = http_res.get("error")
            return result

        if mode == "fast":
            result["status"] = "online"
            return result

        if mode == "deep":
            deep = await m3u8_deep_check(channel["url"], timeout)
            if deep["ok"]:
                result["status"] = "online"
            else:
                result["status"] = "offline"
                result["error"] = deep.get("error")
            return result

        if mode == "ffprobe":
            ff = await asyncio.to_thread(ffprobe_check, channel["url"], timeout)
            if ff["ok"]:
                result["status"] = "online"
                result["video_codec"] = ff.get("video_codec")
                result["audio_codec"] = ff.get("audio_codec")
                result["resolution"] = ff.get("resolution")
            else:
                result["status"] = "offline"
                result["error"] = ff.get("error")
            return result

        result["status"] = "online"
        return result
