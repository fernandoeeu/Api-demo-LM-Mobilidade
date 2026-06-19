# API demo — LM Mobilidade

API FastAPI para consultar veículos na Webmotors.

Duas estratégias:

| Endpoint | Fonte | Qualidade do dado |
|---|---|---|
| `GET /search_vehicle` | Tavily (busca indexada) + regex nos snippets | aproximado |
| `GET /webmotors/*` | **API interna da Webmotors** (`/api/detail`, `/api/search`) | **dado real e estruturado** |

Os endpoints `/webmotors/*` retornam preço, km, cor, câmbio, cidade/UF, ano de
fabricação/modelo etc. direto da fonte.

## Como o bloqueio anti-bot é resolvido

A Webmotors fica atrás do **PerimeterX**. `curl`/`fetch` puro com cookie
copiado cai em 401/403: o token `_px3` é emitido por uma sessão de navegador
real e amarrado ao fingerprint + IP. O fluxo (em `webmotors_scraper.py`):

1. **Mint** — abre o **Chrome real** (headed) via Playwright
   com um init-script de stealth (patch de `navigator.webdriver` etc.). Deixa o
   sensor PerimeterX rodar e, **se aparecer o captcha "Pressione e segure",
   resolve sozinho** segurando o mouse no botão via Playwright. Colhe os
   cookies `_px3`, `_pxvid`, `pxcts`, `_pxde` + User-Agent.
2. **Scrape** — com esses cookies + UA + mesmo IP, `requests` puro no
   `/api/detail/...` e `/api/search/car` retorna **200 de forma consistente**.
   O `_px3` dura ~10 min e não rotaciona; quando uma chamada toma 403, o cliente
   **re-minta sozinho**.

Validado: 40/40 hits no detail + 12/12 carros distintos a partir da busca, e
cold-start completo (mint → busca → detalhe) reproduzível do zero.

## Como rodar

Pré-requisitos:

- Python 3.10+
- Google Chrome instalado

### Windows (PowerShell)

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn vehicle_search:app --reload
```

> Se o `Activate.ps1` for bloqueado pela política de execução, rode antes:
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned`
> (ou, no `cmd`, use `.venv\Scripts\activate.bat`).

### macOS / Linux

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn vehicle_search:app --reload
```

No primeiro request a `/webmotors/*`, o Playwright abre o Chrome para gerar a
sessão. Depois disso, a API reutiliza a sessão em cache.

Se o Chrome estiver em um local não padrão, defina `WEBMOTORS_CHROME_PATH` no
`.env`.

## Testar

Com a API no ar, abra `http://localhost:8000/docs` ou rode:

```bash
curl "http://localhost:8000/webmotors/search?marca=honda&modelo=city&per_page=12"
```

No PowerShell, use `curl.exe` em vez de `curl`.

Também dá para testar sem subir a API:

```bash
python webmotors_scraper.py search --marca honda --modelo city --per-page 1
python webmotors_scraper.py detail --marca honda --modelo city --pages 1 --per-page 1
```

## Endpoints

```bash
# Busca real (lista + médias)
curl 'http://localhost:8000/webmotors/search?marca=honda&modelo=city&per_page=12'

# Busca + detalhe completo de cada usado
curl 'http://localhost:8000/webmotors/detail?marca=honda&modelo=city&pages=1&per_page=12'

# Detalhe por URL de detail
curl 'http://localhost:8000/webmotors/detail_by_url?url=https://www.webmotors.com.br/api/detail/car/honda/city/15-i-vtec-flex-hatch-exl-cvt/4-portas/2022/69863170'

# Força um novo mint (reabre o Chrome)
curl -X POST 'http://localhost:8000/webmotors/refresh_session'
```

## CLI (sem subir a API)

```bash
python webmotors_scraper.py mint                              # abre o browser e salva a sessão
python webmotors_scraper.py search --marca honda --modelo city --per-page 12
python webmotors_scraper.py detail --marca honda --modelo city --per-page 6
python webmotors_scraper.py url "https://www.webmotors.com.br/api/detail/car/..."
```

## Configuração

Tudo via env (ver `.env.example`): `WEBMOTORS_CHROME_PATH`, `WEBMOTORS_UA`,
`WEBMOTORS_CACHE`, `WEBMOTORS_PROFILE_DIR`, `WEBMOTORS_SESSION_TTL`.
