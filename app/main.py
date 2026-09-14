import os
import re
import asyncio
import traceback
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


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


# ─────────────────────────────────────────────────────
# CRON MESTRE: /cron/next
# Processa 1 lote por vez e guarda o offset na sync_progress.
# ─────────────────────────────────────────────────────

@app.api_route("/cron/next", methods=["GET", "HEAD"])
async def cron_next(batch_size: int = 50, mode: str = "deep"):
    """
    Processa o próximo lote de canais.
    Cada execução:
      1. Lê o offset atual da tabela sync_progress
      2. Processa `batch_size` canais a partir desse offset
      3. Salva os online em channels_online
      4. Atualiza o offset na sync_progress
      5. Se passou do total, reinicia do 0
    """
    log = supabase.table("sync_log").insert({"status": "running"}).execute()
    log_id = log.data[0]["id"] if log.data else None

    try:
        # 1) Lê offset atual
        prog = supabase.table("sync_progress").select("*").eq("id", 1).execute()
        if not prog.data:
            # Se não existe, cria
            supabase.table("sync_progress").insert({"id": 1, "next_offset": 0}).execute()
            current_offset = 0
        else:
            current_offset = prog.data[0].get("next_offset") or 0

        # 2) Baixa M3U
        resp = supabase.table("settings").select("m3u_url").eq("id", 1).execute()
        if not resp.data:
            raise HTTPException(404, "URL do M3U não encontrada")
        m3u_url = clean_url(resp.data[0]["m3u_url"])

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            r = await client.get(m3u_url)
            r.raise_for_status()
            m3u_content = r.text

        all_channels = parse_m3u(m3u_content)
        total = len(all_channels)

        # 3) Se já passou do fim, reinicia do 0
        if current_offset >= total:
            current_offset = 0

        # 4) Pega o lote
        chunk = all_channels[current_offset:current_offset + batch_size]

        # 5) Verifica em paralelo
        sem = asyncio.Semaphore(30)
        tasks = [check_channel(ch, 10, 1, mode, sem) for ch in chunk]
        results = await asyncio.gather(*tasks)

        # 6) Dedup + payload
        seen = set()
        online = []
        for ch, res in zip(chunk, results):
            if res["status"] != "online":
                continue
            stream = ch["url"]
            if stream in seen:
                continue
            seen.add(stream)
            online.append({
                "stream": stream,
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

        # 7) Upsert em lotes
        saved = 0
        if online:
            for i in range(0, len(online), 50):
                b = online[i:i + 50]
                supabase.table("channels_online").upsert(
                    b, on_conflict="stream"
                ).execute()
                saved += len(b)

        # 8) Calcula próximo offset
        next_offset = current_offset + batch_size
        finished_round = next_offset >= total
        if finished_round:
            next_offset = 0  # reinicia na próxima rodada

        # 9) Atualiza sync_progress
        supabase.table("sync_progress").update({
            "next_offset": next_offset,
            "total_channels": total,
            "last_run_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", 1).execute()

        # 10) Fecha log
        if log_id:
            supabase.table("sync_log").update({
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "total_channels": len(chunk),
                "online_count": saved,
                "status": "done",
            }).eq("id", log_id).execute()

        return {
            "ok": True,
            "processed_offset": current_offset,
            "next_offset": next_offset,
            "total": total,
            "batch_size": len(chunk),
            "mode": mode,
            "online_neste_lote": saved,
            "rodada_completa": finished_round,
        }

    except Exception as e:
        tb = traceback.format_exc()
        print(f"ERRO COMPLETO:\n{tb}")
        if log_id:
            supabase.table("sync_log").update({
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status": "error",
                "error": str(e)[:500],
            }).eq("id", log_id).execute()
        raise HTTPException(500, f"{type(e).__name__}: {str(e)[:200]}")


# ─────────────────────────────────────────────────────
# Endpoint para resetar o progresso (útil se travar)
# ─────────────────────────────────────────────────────

@app.api_route("/cron/reset", methods=["GET", "HEAD"])
async def cron_reset():
    """Reseta o offset da sincronização para 0."""
    supabase.table("sync_progress").update({
        "next_offset": 0,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", 1).execute()
    return {"ok": True, "message": "Progresso resetado"}


# ─────────────────────────────────────────────────────
# CRON: BAIXAR EPG E SALVAR CACHE (mantido, 1x por dia)
# ─────────────────────────────────────────────────────

@app.api_route("/cron/epg", methods=["GET", "HEAD"])
async def cron_epg():
    resp = supabase.table("settings").select("m3u_url").eq("id", 1).execute()
    if not resp.data:
        raise HTTPException(404, "URL do M3U não encontrada")
    m3u_url = clean_url(resp.data[0]["m3u_url"])

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.get(m3u_url)
        m3u_content = r.text

    epg_urls = []
    for line in m3u_content.splitlines():
        if line.startswith("#EXTM3U") and "url-tvg=" in line:
            match = re.search(r'url-tvg="([^"]+)"', line)
            if match:
                epg_urls = [u.strip() for u in match.group(1).split(",") if u.strip()]
            break

    if not epg_urls:
        return {"ok": False, "error": "Nenhum EPG encontrado no M3U"}

    resp = supabase.table("channels_online").select("tvg_id").execute()
    online_tvg_ids = {row["tvg_id"] for row in (resp.data or []) if row.get("tvg_id")}

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

    return {"ok": True, "epgs_baixados": len(epg_urls), "canais_com_epg": saved}


# ─────────────────────────────────────────────────────
# DEBUG
# ─────────────────────────────────────────────────────

@app.api_route("/debug/m3u-url", methods=["GET", "HEAD"])
async def debug_m3u_url():
    resp = supabase.table("settings").select("m3u_url").eq("id", 1).execute()
    if not resp.data:
        raise HTTPException(404, "Nada em settings id=1")
    raw = resp.data[0]["m3u_url"]
    return {
        "raw_length": len(raw),
        "cleaned_url": clean_url(raw),
        "has_whitespace": raw != clean_url(raw),
    }


@app.api_route("/debug/online-count", methods=["GET", "HEAD"])
async def debug_online_count():
    resp = supabase.table("channels_online").select("id", count="exact").execute()
    return {"online_canais": resp.count or 0}


@app.api_route("/debug/progress", methods=["GET", "HEAD"])
async def debug_progress():
    """Mostra o estado atual da sincronização."""
    resp = supabase.table("sync_progress").select("*").eq("id", 1).execute()
    if not resp.data:
        return {"ok": False, "error": "sync_progress não inicializado"}
    p = resp.data[0]
    total = p.get("total_channels") or 0
    offset = p.get("next_offset") or 0
    return {
        "next_offset": offset,
        "total_channels": total,
        "progresso_percent": round((offset / total * 100), 1) if total else 0,
        "last_run_at": p.get("last_run_at"),
    }


@app.api_route("/debug/epg-sample", methods=["GET", "HEAD"])
async def debug_epg_sample(tvg_id: str = Query(...)):
    resp = supabase.table("epg_cache").select("*").eq("tvg_id", tvg_id).execute()
    if not resp.data:
        raise HTTPException(404, "EPG não encontrado")
    return get_current_and_next(resp.data[0]["programs"])


@app.api_route("/debug/parse", methods=["GET", "HEAD"])
async def debug_parse(limit: int = 20, offset: int = 0):
    resp = supabase.table("settings").select("m3u_url").eq("id", 1).execute()
    if not resp.data:
        raise HTTPException(404, "URL do M3U não encontrada")
    m3u_url = clean_url(resp.data[0]["m3u_url"])

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(m3u_url)
        m3u_content = r.text

    all_channels = parse_m3u(m3u_content)
    slice_ = all_channels[offset:offset + limit]
    return {
        "total": len(all_channels),
        "offset": offset,
        "limit": limit,
        "canais": [
            {"name": c["name"], "group": c.get("group"), "url": c["url"]}
            for c in slice_
        ],
    }
