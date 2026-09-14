import os
import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from supabase import create_client, Client
from .m3u_parser import parse_m3u
from .checker import check_channel

app = FastAPI(title="IPTV Checker API")

# Configuração do Supabase (vamos usar variáveis de ambiente por segurança)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("AVISO: Variáveis de ambiente do Supabase não configuradas!")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

@app.get("/check")
async def check_playlist_from_db():
    try:
        # 1. Busca a URL do M3U na tabela 'settings' do Supabase
        response = supabase.table("settings").select("m3u_url").eq("id", 1).execute()
        
        if not response.data:
            raise HTTPException(404, "URL do M3U não encontrada no Supabase.")
            
        m3u_url = response.data[0]['m3u_url']
        
        # 2. Baixa o arquivo M3U
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(m3u_url)
            m3u_content = r.text
            
        channels = parse_m3u(m3u_content)
        if not channels:
            raise HTTPException(400, "Nenhum canal encontrado no M3U.")

        # 3. Verifica os canais em paralelo
        sem = asyncio.Semaphore(20) # Limita a 20 verificações ao mesmo tempo
        tasks = [check_channel(ch, 10, sem) for ch in channels]
        results = await asyncio.gather(*tasks)

        # 4. Monta o resumo
        online = sum(1 for r in results if r["status"] == "online")
        offline = sum(1 for r in results if r["status"] == "offline")
        
        return {
            "total": len(results),
            "online": online,
            "offline": offline,
            "results": results
        }
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/health")
async def health():
    return {"status": "ok"}
