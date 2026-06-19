from fastapi import FastAPI, HTTPException
from tavily import TavilyClient
from dotenv import load_dotenv
from pathlib import Path
from functools import lru_cache
import os
import re

import webmotors_scraper as wm

# ==========================================
# Carrega .env
# ==========================================

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

# ==========================================
# FastAPI
# ==========================================

app = FastAPI(title="LM Mobilidade - Webmotors")


# Tavily é lazy: o app sobe mesmo sem a key (os endpoints /webmotors não
# dependem dela). A key só é exigida quando /search_vehicle é chamado.
@lru_cache
def get_tavily_client():
    if not TAVILY_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="TAVILY_API_KEY não configurada no .env",
        )
    return TavilyClient(api_key=TAVILY_API_KEY)


# Cliente Webmotors compartilhado entre requests (reusa a sessão PerimeterX
# em cache; só re-minta quando expira ou toma 403).
@lru_cache
def get_webmotors_client() -> wm.WebmotorsClient:
    return wm.WebmotorsClient()


# ==========================================
# Mantém apenas Webmotors
# ==========================================

def keep_only_webmotors(results):

    return [
        r
        for r in results
        if "webmotors.com.br" in r.get("url", "")
    ]

# ==========================================
# Extrai preço e km
# ==========================================

def extract_vehicle_data(text):

    preco_pattern = r"R\$\s?([\d\.]+)"
    km_pattern = r"([\d\.]+)\s?Km"

    precos = re.findall(
        preco_pattern,
        text,
        flags=re.IGNORECASE
    )

    kms = re.findall(
        km_pattern,
        text,
        flags=re.IGNORECASE
    )

    anuncios = []

    total = min(
        len(precos),
        len(kms)
    )

    for i in range(total):

        try:

            preco = int(
                precos[i].replace(".", "")
            )

            km = int(
                kms[i].replace(".", "")
            )

            anuncios.append({
                "preco": preco,
                "km": km
            })

        except:
            pass

    return anuncios


# ==========================================
# Médias (preço/km) sobre anúncios normalizados
# ==========================================

def _to_number(value):
    # A Webmotors devolve número nativo na busca (115900.0) e string no detail
    # ("115900"). Tavily já entrega int. Tratamos os três sem inflar o valor.
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = re.sub(r"[^\d.,]", "", str(value).strip())
    if not s:
        return None
    if "," in s:  # pt-BR: ponto é milhar, vírgula é decimal
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def calcular_medias(anuncios, *, campo_preco="preco", campo_km="km"):
    precos = [n for n in (_to_number(a.get(campo_preco)) for a in anuncios) if n]
    kms = [n for n in (_to_number(a.get(campo_km)) for a in anuncios) if n]
    return {
        "media_preco": round(sum(precos) / len(precos), 2) if precos else None,
        "media_km": round(sum(kms) / len(kms), 2) if kms else None,
    }


# ==========================================
# Endpoint Tavily (busca aproximada via snippets) - legado
# ==========================================

@app.get("/search_vehicle")
def search_vehicle(
    marca: str,
    modelo: str,
    ano: int,
    cor: str = "",
    localidade: str = ""
):

    query = f"""
    site:webmotors.com.br
    {marca}
    {modelo}
    {ano}
    {cor}
    {localidade}
    veículo usado
    preço
    quilometragem
    """

    result = get_tavily_client().search(
        query=query,
        search_depth="advanced",
        max_results=50
    )

    webmotors_results = keep_only_webmotors(
        result.get("results", [])
    )

    anuncios_extraidos = []

    for item in webmotors_results:

        content = item.get(
            "content",
            ""
        )

        anuncios = extract_vehicle_data(
            content
        )

        for anuncio in anuncios:

            anuncios_extraidos.append({

                "titulo": item.get(
                    "title",
                    ""
                ),

                "url": item.get(
                    "url",
                    ""
                ),

                "preco": anuncio["preco"],

                "km": anuncio["km"],

                "localidade_pesquisada": localidade,

                "cor_pesquisada": cor
            })

    medias = calcular_medias(anuncios_extraidos)

    return {
        "marca": marca,
        "modelo": modelo,
        "ano": ano,
        "cor": cor,
        "localidade": localidade,
        "total_anuncios": len(anuncios_extraidos),
        "media_preco": medias["media_preco"],
        "media_km": medias["media_km"],
        "anuncios": anuncios_extraidos,
    }


# ==========================================
# Endpoints Webmotors (dados REAIS via API interna)
#
# Passa pelo PerimeterX abrindo o Chrome real uma vez (mint), depois usa
# HTTP puro com os cookies colhidos. Ver webmotors_scraper.py.
# ==========================================

@app.get("/webmotors/search")
def webmotors_search(
    marca: str,
    modelo: str = "",
    page: int = 1,
    per_page: int = 24,
):
    """Busca anúncios reais na Webmotors (preço/km/cor/cidade corretos)."""
    try:
        data = get_webmotors_client().search(
            make=marca, model=modelo, page=page, per_page=per_page
        )
    except wm.WebmotorsBlocked as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    anuncios = data.get("SearchResults", [])
    anuncios_normalizados = [wm.normalize_detail(it) for it in anuncios]
    medias = calcular_medias(anuncios_normalizados)

    return {
        "marca": marca,
        "modelo": modelo,
        "page": page,
        "total_disponivel": data.get("Count"),
        "total_anuncios": len(anuncios),
        "media_preco": medias["media_preco"],
        "media_km": medias["media_km"],
        "anuncios": anuncios,
        "payload_completo": data,
    }


@app.get("/webmotors/detail")
def webmotors_detail(
    marca: str,
    modelo: str = "",
    pages: int = 1,
    per_page: int = 12,
):
    """Busca + detalha cada usado (payload completo do /api/detail)."""
    try:
        details = get_webmotors_client().iter_details(
            make=marca, model=modelo, pages=pages, per_page=per_page
        )
    except wm.WebmotorsBlocked as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    anuncios_normalizados = [wm.normalize_detail(d) for d in details]
    medias = calcular_medias(anuncios_normalizados)

    return {
        "marca": marca,
        "modelo": modelo,
        "total_anuncios": len(details),
        "media_preco": medias["media_preco"],
        "media_km": medias["media_km"],
        "anuncios": details,
    }


@app.get("/webmotors/detail_by_url")
def webmotors_detail_by_url(url: str):
    """Detalhe de um anúncio a partir da URL de detail (/api/detail/...)."""
    try:
        detail = get_webmotors_client().get_detail(url=url)
    except wm.WebmotorsBlocked as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return detail


@app.post("/webmotors/refresh_session")
def webmotors_refresh_session():
    """Força um novo mint (abre o Chrome e resolve o PerimeterX de novo)."""
    try:
        sess = get_webmotors_client().ensure_session(force=True)
    except wm.WebmotorsBlocked as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True, "cookies": list(sess.cookies), "user_agent": sess.user_agent}
