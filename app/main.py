import os
import asyncio
import httpx
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Query
from supabase import create_client, Client
from .m3u_parser import parse_m3u
from .checker import check_channel
from .epg import parse_epg_xml, get_current_and_next

app = FastAPI(title="IPTV Checker + EPG API")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

print("=" * 50)
print(f"SUPABASE_URL definida: {bool(SUPABASE_URL)}")
print(f"SUPABASE_KEY definida: {bool(SUPABASE_KEY)}")
print("=" * 50)

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL e SUPABASE_KEY precisam estar definidas!")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
print("Supabase client criado com sucesso!")


def clean_url(url: str) -> str:
    if not url:
        return url
    return "".join(url.split())


# Aceita GET e HEAD (UptimeRobot free só manda HEAD)
@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


# ─────────────────────────────────────────────────────
# VERIFICAÇÃO E SALVAMENTO DOS CANAIS ONLINE
# ─────────────────────────────────────────────────────

@app.api_route("/cron/update", methods=["GET", "HEAD"])
async def cron_update(
    limit: int = 100,
    offset: int = 0,
    concurrency: int = 30,
    ffprobe: bool = False,
):
    """
    Verifica canais e salva APENAS os online no Supabase.
    Ideal para ser chamado por cron job (cada faixa de offset).
    """
    log = supabase.table("sync_log").insert({
        "status": "running",
    }).execute()
    log_id = log.data[0]["id"] if log.data else None

    try:
        # 1) Lê URL do M3U
        resp = supabase.table("settings").select("m3u_url").eq("id", 1).execute()
        if not resp.data:
            raise HTTPException(404, "URL do M3U não encontrada em settings id=1")
        m3u_url = clean_url(resp.data[0]["m3u_url"])

        # 2) Baixa M3U
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            r = await client.get(m3u_url)
            r.raise_for_status()
            m3u_content = r.text

        # 3) Parseia
        all_channels = parse_m3u(m3u_content)
        total = len(all_channels)
        slice_ = all_channels[offset:offset + limit]

        # 4) Verifica em paralelo
        sem = asyncio.Semaphore(concurrency)
        tasks = [check_channel(ch, 10, 1, ffprobe, sem) for ch in slice_]
        results = await asyncio.gather(*tasks)

        # 5) Salva só os ONLINE (upsert por stream)
        online = []
        for ch, res in zip(slice_, results):
            if res["status"] == "online":
                online.append({
                    "stream": ch["url"],
                    "name": ch["name"],
                    "logo": ch.get("logo"),
                    "group_title": ch.get("group"),
                    "tvg_id": ch.get("tvg_id"),
                    "resolution": res.get("resolution"),
                    "video_codec": res.get("video_codec"),
                    "audio_codec": res.get("audio_codec"),
                    "response_time_ms": res.get("response_time_ms"),
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                })

        if online:
            batch_size = 100
            for i in range(0, len(online), batch_size):
                supabase.table("channels_online").upsert(
                    online[i:i + batch_size],
                    on_conflict="stream",
                ).execute()

        # 6) Atualiza log
        if log_id:
            supabase.table("sync_log").update({
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "total_channels": len(slice_),
                "online_count": len(online),
                "status": "done",
            }).eq("id", log_id).execute()

        return {
            "ok": True,
            "total_original": total,
            "verificados": len(slice_),
            "offset": offset,
            "limit": limit,
            "online_salvos": len(online),
        }

    except Exception as e:
        if log_id:
            supabase.table("sync_log").update({
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status": "error",
                "error": str(e)[:500],
            }).eq("id", log_id).execute()
        raise HTTPException(500, str(e))


# ─────────────────────────────────────────────────────
# BAIXAR E SALVAR EPG NO CACHE
# ─────────────────────────────────────────────────────

@app.api_route("/cron/epg", methods=["GET", "HEAD"])
async def cron_epg():
    """
    Baixa os EPGs do M3U (url-tvg), parseia e salva no cache.
    Só salva programação dos canais que estão em channels_online.
    """
    resp = supabase.table("settings").select("m3u_url").eq("id", 1).execute()
    if not resp.data:
        raise HTTPException(404, "URL do M3U não encontrada")
    m3u_url = clean_url(resp.data[0]["m3u_url"])

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.get(m3u_url)
        m3u_content = r.text

    # Extrai a lista de EPG do cabeçalho
    epg_urls = []
    for line in m3u_content.splitlines():
        if line.startswith("#EXTM3U") and "url-tvg=" in line:
            import re
            match = re.search(r'url-tvg="([^"]+)"', line)
            if match:
                epg_urls = [u.strip() for u in match.group(1).split(",") if u.strip()]
            break

    if not epg_urls:
        return {"ok": False, "error": "Nenhum EPG encontrado no M3U"}

    # Canais online (só os que importam)
    resp = supabase.table("channels_online").select("tvg_id").execute()
    online_tvg_ids = {row["tvg_id"] for row in (resp.data or []) if row.get("tvg_id")}

    # Baixa e mescla todos os EPGs
    merged: dict = {}
    for epg_url in epg_urls:
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                r = await client.get(epg_url)
                if r.status_code != 200:
                    continue
                parsed = parse_epg_xml(r.content)
                for tvg_id, programs in parsed.items():
                    if tvg_id in online_tvg_ids:
                        merged.setdefault(tvg_id, []).extend(programs)
        except Exception as e:
            print(f"Erro EPG {epg_url}: {e}")
            continue

    # Salva no cache
    saved = 0
    for tvg_id, programs in merged.items():
        seen = set()
        unique = []
        for p in sorted(programs, key=lambda x: x["inicio"]):
            if p["inicio"] not in seen:
                seen.add(p["inicio"])
                unique.append(p)

        supabase.table("epg_cache").upsert({
            "tvg_id": tvg_id,
            "programs": unique,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="tvg_id").execute()
        saved += 1

    return {
        "ok": True,
        "epgs_baixados": len(epg_urls),
        "canais_com_epg": saved,
    }


# ─────────────────────────────────────────────────────
# CONSULTAS DE DEBUG
# ─────────────────────────────────────────────────────

@app.api_route("/debug/m3u-url", methods=["GET", "HEAD"])
async def debug_m3u_url():
    resp = supabase.table("settings").select("m3u_url").eq("id", 1).execute()
    if not resp.data:
        raise HTTPException(404, "Nada em settings id=1")
    raw = resp.data[0]["m3u_url"]
    return {
        "raw_length": len(raw),
        "cleaned_length": len(clean_url(raw)),
        "has_whitespace": raw != clean_url(raw),
        "cleaned_url": clean_url(raw),
    }


@app.api_route("/debug/online-count", methods=["GET", "HEAD"])
async def debug_online_count():
    resp = supabase.table("channels_online").select("id", count="exact").execute()
    return {"online_canais": resp.count or 0}


@app.api_route("/debug/epg-sample", methods=["GET", "HEAD"])
async def debug_epg_sample(tvg_id: str = Query(...)):
    resp = supabase.table("epg_cache").select("*").eq("tvg_id", tvg_id).execute()
    if not resp.data:
        raise HTTPException(404, "EPG não encontrado")
    programs = resp.data[0]["programs"]
    return get_current_and_next(programs)
