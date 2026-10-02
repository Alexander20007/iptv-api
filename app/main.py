import os
import re
import asyncio
import traceback
import httpx
from datetime import datetime, timezone, timedelta
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


def get_epg_urls_from_m3u(m3u_content: str) -> list:
    for line in m3u_content.splitlines():
        if line.startswith("#EXTM3U") and "url-tvg=" in line:
            match = re.search(r'url-tvg="([^"]+)"', line)
            if match:
                return [u.strip() for u in match.group(1).split(",") if u.strip()]
    return []


async def fetch_m3u() -> str:
    resp = supabase.table("settings").select("m3u_url").eq("id", 1).execute()
    if not resp.data:
        raise HTTPException(404, "URL do M3U não encontrada")
    m3u_url = clean_url(resp.data[0]["m3u_url"])
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(m3u_url)
        r.raise_for_status()
        return r.text


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


# ─────────────────────────────────────────────────────
# CRON CANAIS
# ─────────────────────────────────────────────────────

@app.api_route("/cron/next", methods=["GET", "HEAD"])
async def cron_next(
    batch_size: int = 20,
    mode: str = "deep",
    concurrency: int = 8,
):
    log = supabase.table("sync_log").insert({"status": "running"}).execute()
    log_id = log.data[0]["id"] if log.data else None

    try:
        prog = supabase.table("sync_progress").select("*").eq("id", 1).execute()
        if not prog.data:
            supabase.table("sync_progress").insert({"id": 1, "next_offset": 0}).execute()
            current_offset = 0
        else:
            current_offset = prog.data[0].get("next_offset") or 0

        m3u_content = await fetch_m3u()
        all_channels = parse_m3u(m3u_content)
        total = len(all_channels)

        if current_offset >= total:
            current_offset = 0

        chunk = all_channels[current_offset:current_offset + batch_size]

        sem = asyncio.Semaphore(concurrency)
        tasks = [check_channel(ch, 10, 1, mode, sem) for ch in chunk]
        results = await asyncio.gather(*tasks)

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

        saved = 0
        if online:
            for i in range(0, len(online), 50):
                b = online[i:i + 50]
                supabase.table("channels_online").upsert(
                    b, on_conflict="stream"
                ).execute()
                saved += len(b)

        next_offset = current_offset + batch_size
        finished_round = next_offset >= total
        if finished_round:
            next_offset = 0

        supabase.table("sync_progress").update({
            "next_offset": next_offset,
            "total_channels": total,
            "last_run_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", 1).execute()

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
            "online_neste_lote": saved,
            "rodada_completa": finished_round,
        }

    except Exception as e:
        tb = traceback.format_exc()
        print(f"ERRO:\n{tb}")
        if log_id:
            supabase.table("sync_log").update({
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status": "error",
                "error": str(e)[:500],
            }).eq("id", log_id).execute()
        raise HTTPException(500, f"{type(e).__name__}: {str(e)[:200]}")


@app.api_route("/cron/reset", methods=["GET", "HEAD"])
async def cron_reset():
    supabase.table("sync_progress").update({
        "next_offset": 0,
    }).eq("id", 1).execute()
    return {"ok": True, "message": "Progresso resetado"}


# ─────────────────────────────────────────────────────
# EPG SOB DEMANDA ⭐
# ─────────────────────────────────────────────────────

@app.api_route("/cron/epg-index", methods=["GET", "HEAD"])
async def cron_epg_index(idx: int = 0):
    """
    Popula a tabela epg_index: mapeia tvg_id → qual EPG contém ele.
    Processa 1 EPG por chamada (chamado varias vezes com idx=0,1,2...).
    """
    try:
        m3u_content = await fetch_m3u()
        epg_urls = get_epg_urls_from_m3u(m3u_content)

        if not epg_urls:
            return {"ok": False, "error": "Nenhum EPG no M3U"}

        if idx >= len(epg_urls):
            return {"ok": True, "message": "Índice completo", "total": len(epg_urls)}

        epg_url = epg_urls[idx]

        # Baixa o EPG
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            r = await client.get(epg_url)
            if r.status_code != 200:
                return {"ok": False, "idx": idx, "error": f"HTTP {r.status_code}"}

            content = r.content
            # Descomprime se necessário (para extrair cabeçalhos)
            import gzip
            if content[:2] == b"\x1f\x8b":
                content = gzip.decompress(content)

            text = content.decode("utf-8", errors="ignore")

        # Extrai os tvg_id (só cabeçalhos, rápido)
        tvg_ids = re.findall(r'<channel\s+id="([^"]+)"', text)

        # Salva no índice
        if tvg_ids:
            rows = [{"tvg_id": t.strip(), "epg_url": epg_url} for t in tvg_ids if t.strip()]
            for i in range(0, len(rows), 500):
                supabase.table("epg_index").upsert(
                    rows[i:i + 500], on_conflict="tvg_id"
                ).execute()

        return {
            "ok": True,
            "idx": idx,
            "total_epgs": len(epg_urls),
            "tvg_ids_encontrados": len(tvg_ids),
            "next_idx": idx + 1 if idx + 1 < len(epg_urls) else None,
        }

    except Exception as e:
        tb = traceback.format_exc()
        print(f"ERRO EPG INDEX:\n{tb}")
        raise HTTPException(500, f"{type(e).__name__}: {str(e)[:200]}")


@app.api_route("/epg/on-demand", methods=["GET", "HEAD"])
async def epg_on_demand(tvg_id: str = Query(...), force: bool = False):
    """
    Busca o EPG de UM canal sob demanda.
    1. Verifica cache (epg_cache). Se tem e é fresco (< 6h), devolve.
    2. Se não, consulta epg_index pra saber qual EPG baixar.
    3. Baixa o EPG, filtra só esse tvg_id, salva no cache e devolve.
    """
    try:
        # 1) Verifica cache
        if not force:
            cached = supabase.table("epg_cache").select("*").eq("tvg_id", tvg_id).execute()
            if cached.data:
                updated = cached.data[0].get("updated_at")
                if updated:
                    try:
                        dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                        if datetime.now(timezone.utc) - dt < timedelta(hours=6):
                            programs = cached.data[0]["programs"]
                            return {
                                "ok": True,
                                "source": "cache",
                                "tvg_id": tvg_id,
                                **get_current_and_next(programs),
                            }
                    except Exception:
                        pass

        # 2) Descobre qual EPG contém esse tvg_id
        idx_resp = supabase.table("epg_index").select("epg_url").eq("tvg_id", tvg_id).execute()
        if not idx_resp.data:
            return {
                "ok": False,
                "error": f"tvg_id '{tvg_id}' não está em nenhum EPG indexado",
            }
        epg_url = idx_resp.data[0]["epg_url"]

        # 3) Baixa o EPG
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            r = await client.get(epg_url)
            if r.status_code != 200:
                return {"ok": False, "error": f"Falha ao baixar EPG (HTTP {r.status_code})"}

            parsed = parse_epg_xml(r.content)

        if tvg_id not in parsed:
            return {"ok": False, "error": "Programação não encontrada no EPG"}

        programs = parsed[tvg_id]

        # 4) Remove duplicatas
        seen = set()
        unique = []
        for p in sorted(programs, key=lambda x: x["inicio"]):
            if p["inicio"] not in seen:
                seen.add(p["inicio"])
                unique.append(p)

        # 5) Salva no cache
        supabase.table("epg_cache").upsert({
            "tvg_id": tvg_id,
            "programs": unique,
            "source_epg_url": epg_url,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="tvg_id").execute()

        return {
            "ok": True,
            "source": "fresh",
            "tvg_id": tvg_id,
            **get_current_and_next(unique),
        }

    except Exception as e:
        tb = traceback.format_exc()
        print(f"ERRO EPG ON-DEMAND:\n{tb}")
        raise HTTPException(500, f"{type(e).__name__}: {str(e)[:200]}")


# ─────────────────────────────────────────────────────
# DEBUG
# ─────────────────────────────────────────────────────

@app.api_route("/debug/online-count", methods=["GET", "HEAD"])
async def debug_online_count():
    resp = supabase.table("channels_online").select("id", count="exact").execute()
    return {"online_canais": resp.count or 0}


@app.api_route("/debug/progress", methods=["GET", "HEAD"])
async def debug_progress():
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
    }


@app.api_route("/debug/epg-index-count", methods=["GET", "HEAD"])
async def debug_epg_index_count():
    resp = supabase.table("epg_index").select("id", count="exact").execute()
    return {"total_indexados": resp.count or 0}


@app.api_route("/debug/epg-cache-count", methods=["GET", "HEAD"])
async def debug_epg_cache_count():
    resp = supabase.table("epg_cache").select("id", count="exact").execute()
    return {"total_em_cache": resp.count or 0}
