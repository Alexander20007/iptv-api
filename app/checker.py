import asyncio
import json
import subprocess
import time
import httpx
from typing import Dict

USER_AGENT = "Mozilla/5.0 (IPTVChecker/1.0)"


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
                    if resp.status_code in (200, 206):
                        return {
                            "ok": True,
                            "http_status": resp.status_code,
                            "response_time_ms": round(elapsed, 2),
                        }
                    last_error = f"HTTP {resp.status_code}"
        except Exception as e:
            last_error = str(e)
        await asyncio.sleep(0.5 * (attempt + 1))

    return {
        "ok": False,
        "http_status": None,
        "response_time_ms": None,
        "error": last_error,
    }


def ffprobe_check(url: str, timeout: int) -> Dict:
    cmd = [
        "ffprobe",
        "-v", "error",
        "-user_agent", USER_AGENT,
        "-timeout", str(timeout * 1_000_000),
        "-i", url,
        "-show_entries", "stream=codec_type,codec_name,width,height",
        "-of", "json",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 5,
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
    use_ffprobe: bool,
    sem: asyncio.Semaphore,
) -> Dict:
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

        if not use_ffprobe:
            result["status"] = "online"
            return result

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
