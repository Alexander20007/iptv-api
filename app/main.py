import os
import asyncio
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from supabase import create_client, Client
from .m3u_parser import parse_m3u
from .checker import check_channel

app = FastAPI(title="IPTV Checker API")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

print("=" * 50)
print(f"SUPABASE_URL definida: {bool(SUPABASE_URL)}")
print(f"SUPABASE_KEY definida: {bool(SUPABASE_KEY)}")
if SUPABASE_KEY:
    print(f"SUPABASE_KEY tamanho: {len(SUPABASE_KEY)}")
    print(f"SUPABASE_KEY prefixo: {SUPABASE_KEY[:20]}...")
print("=" * 50)

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL e SUPABASE_KEY precisam estar definidas!")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
print("Supabase client criado com sucesso!")


def clean_url(url: str) -> str:
    if not url:
        return url
    return "".join(url.split())


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/debug/m3u-url")
async def debug_m3u_url():
    response = supabase.table("settings").select("m3u_url").eq("id", 1).execute()
    if not response.data:
        raise HTTPException(404, "Nenhuma URL encontrada em settings id=1")
    raw = response.data[0]["m3u_url"]
    return {
        "raw_length": len(raw),
        "cleaned_length": len(clean_url(raw)),
        "has_whitespace": raw != clean_url(raw),
        "cleaned_url": clean_url(raw),
    }


@app.get("/check")
async def check_playlist_from_db(
    limit: int = 50,
    offset: int = 0,
    ffprobe: bool = False,
    timeout: int = 10,
    retries: int = 1,
):
    try:
        response = supabase.table("settings").select("m3u_url").eq("id", 1).execute()
        if not response.data:
            raise HTTPException(404, "URL do M3U não encontrada no Supabase.")

        m3u_url = clean_url(response.data[0]["m3u_url"])
        print(f"URL limpa: {m3u_url}")

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            r = await client.get(m3u_url)
            r.raise_for_status()
            m3u_content = r.text

        channels = parse_m3u(m3u_content)
        if not channels:
            raise HTTPException(400, "Nenhum canal encontrado no M3U.")

        total_original = len(channels)
        channels = channels[offset:offset + limit]

        sem = asyncio.Semaphore(20)
        tasks = [check_channel(ch, timeout, retries, ffprobe, sem) for ch in channels]
        results = await asyncio.gather(*tasks)

        online = sum(1 for r in results if r["status"] == "online")
        offline = sum(1 for r in results if r["status"] == "offline")
        errors = sum(1 for r in results if r["status"] == "error")

        return {
            "total_original": total_original,
            "total_verificado": len(results),
            "offset": offset,
            "limit": limit,
            "online": online,
            "offline": offline,
            "errors": errors,
            "results": results,
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"ERRO: {e}")
        raise HTTPException(500, str(e))


@app.get("/check/clean-m3u", response_class=PlainTextResponse)
async def clean_m3u(
    limit: int = 50,
    offset: int = 0,
    ffprobe: bool = False,
    timeout: int = 10,
    retries: int = 1,
):
    response = supabase.table("settings").select("m3u_url").eq("id", 1).execute()
    if not response.data:
        raise HTTPException(404, "URL do M3U não encontrada no Supabase.")

    m3u_url = clean_url(response.data[0]["m3u_url"])

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(m3u_url)
        r.raise_for_status()
        m3u_content = r.text

    channels = parse_m3u(m3u_content)[offset:offset + limit]

    sem = asyncio.Semaphore(20)
    tasks = [check_channel(ch, timeout, retries, ffprobe, sem) for ch in channels]
    results = await asyncio.gather(*tasks)

    lines = ["#EXTM3U"]
    for r in results:
        if r["status"] == "online":
            attrs = []
            if r.get("logo"):
                attrs.append(f'tvg-logo="{r["logo"]}"')
            if r.get("group"):
                attrs.append(f'group-title="{r["group"]}"')
            attr_str = " ".join(attrs)
            lines.append(f"#EXTINF:-1 {attr_str},{r['name']}")
            lines.append(r["url"])
    return "\n".join(lines)
