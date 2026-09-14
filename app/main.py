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
    print(f"SUPABASE_KEY começa com: {SUPABASE_KEY[:20]}...")
print("=" * 50)

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL e SUPABASE_KEY precisam estar definidas!")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
print("Supabase client criado com sucesso!")


def clean_url(url: str) -> str:
    """Remove espaços, quebras de linha e tabs da URL."""
    if not url:
        return url
    # Remove todos os whitespace (espaço, \n, \t, \r)
    return "".join(url.split())


@app.get("/debug/env")
async def debug_env():
    return {
        "url_defined": bool(SUPABASE_URL),
        "key_defined": bool(SUPABASE_KEY),
        "key_length": len(SUPABASE_KEY) if SUPABASE_KEY else 0,
        "key_prefix": SUPABASE_KEY[:20] if SUPABASE_KEY else None,
    }


@app.get("/debug/m3u-url")
async def debug_m3u_url():
    """Mostra o que está salvo no Supabase, com caracteres visíveis."""
    response = supabase.table("settings").select("m3u_url").eq("id", 1).execute()
    if not response.data:
        raise HTTPException(404, "Não encontrado")
    raw = response.data[0]["m3u_url"]
    return {
        "raw_url": raw,
        "raw_length": len(raw),
        "cleaned_url": clean_url(raw),
        "cleaned_length": len(clean_url(raw)),
        "has_whitespace": raw != clean_url(raw),
    }


@app.get("/check")
async def check_playlist_from_db(limit: int = 50, offset: int = 0):
    try:
        # 1. Busca URL no Supabase
        response = supabase.table("settings").select("m3u_url").eq("id", 1).execute()
        if not response.data:
            raise HTTPException(404, "URL do M3U não encontrada no Supabase.")
        
        m3u_url = clean_url(response.data[0]["m3u_url"])
        print(f"URL limpa: {m3u_url}")

        # 2. Baixa o M3U
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            r = await client.get(m3u_url)
            r.raise_for_status()
            m3u_content = r.text

        channels = parse_m3u(m3u_content)
        if not channels:
            raise HTTPException(400, "Nenhum canal encontrado no M3U.")

        total_original = len(channels)
        # 3. Aplica offset/limit para não estourar timeout
        channels = channels[offset:offset + limit]

        # 4. Verifica em paralelo
        sem = asyncio.Semaphore(20)
        tasks = [check_channel(ch, 10, sem) for ch in channels]
        results = await asyncio.gather(*tasks)

        online = sum(1 for r in results if r["status"] == "online")
        offline = sum(1 for r in results if r["status"] == "offline")

        return {
            "total_original": total_original,
            "total_verificado": len(results),
            "offset": offset,
            "limit": limit,
            "online": online,
            "offline": offline,
            "results": results,
        }
    except Exception as e:
        print(f"ERRO: {e}")
        raise HTTPException(500, str(e))


@app.get("/health")
async def health():
    return {"status": "ok"}
