"""
Scraper real da Webmotors (API interna /api/detail e /api/search).

A Webmotors fica atrás do PerimeterX. curl/fetch puro com cookie copiado cai
em 401/403 porque o token `_px3` é emitido por uma sessão de navegador real e
amarrado ao fingerprint + IP. Este módulo resolve isso assim:

    1. MINT  -> abre o Chrome REAL (headed) via Playwright com stealth,
                deixa o sensor PerimeterX rodar (e resolve o press-and-hold
                "Pressione e segure" se ele aparecer), e colhe os cookies
                `_px3`, `_pxvid`, `pxcts`, `_pxde` + User-Agent.
    2. SCRAPE -> com esses cookies + UA + mesmo IP, requests puro no
                `/api/detail/...` e `/api/search/car` retorna 200 de forma
                consistente. O `_px3` dura ~10 min e NÃO rotaciona, então
                quando uma chamada toma 403 o cliente re-minta sozinho.

Validado: 40/40 hits no detail + 12/12 carros distintos a partir da busca.

Dependências de runtime:
    - Google Chrome instalado
    - playwright
    - requests

Bypass do browser: se você já tem cookies válidos, exporte
WEBMOTORS_COOKIE="_px3=...; _pxvid=...; pxcts=...; _pxde=..." e
WEBMOTORS_UA="...". Aí o mint via browser nem é chamado.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

# ==========================================
# Configuração (tudo sobrescrevível por env)
# ==========================================

def _default_chrome_path() -> str:
    """Caminho do Chrome real por SO, o 1º que existir, senão o default do SO.

    O mint precisa do Chrome de verdade (o fingerprint tem que ser o do binário
    real). Sobrescrevível por WEBMOTORS_CHROME_PATH.
    """
    if sys.platform == "win32":
        candidates = [
            os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    elif sys.platform == "darwin":
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:  # linux e afins
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ]
    return next((p for p in candidates if os.path.exists(p)), candidates[0])


CHROME_PATH = os.getenv("WEBMOTORS_CHROME_PATH") or _default_chrome_path()


def _default_user_agent() -> str:
    """UA coerente com o SO real. O PerimeterX cruza o UA com o fingerprint,
    então um UA de Mac rodando no Windows é um tell. Sobrescrevível por
    WEBMOTORS_UA. No bypass por cookie, use o UA do navegador onde colheu os
    cookies.
    """
    if sys.platform == "win32":
        platform_token = "Windows NT 10.0; Win64; x64"
    elif sys.platform == "darwin":
        platform_token = "Macintosh; Intel Mac OS X 10_15_7"
    else:
        platform_token = "X11; Linux x86_64"
    return (
        f"Mozilla/5.0 ({platform_token}) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    )


USER_AGENT_OVERRIDE = os.getenv("WEBMOTORS_UA")
DEFAULT_UA = USER_AGENT_OVERRIDE or _default_user_agent()

BASE_URL = "https://www.webmotors.com.br"
PROFILE_DIR = os.getenv(
    "WEBMOTORS_PROFILE_DIR",
    str(Path(tempfile.gettempdir()) / "wm-scraper-profile"),
)
CACHE_PATH = Path(
    os.getenv("WEBMOTORS_CACHE", str(Path(tempfile.gettempdir()) / "wm-session.json"))
)

# Viewport fixa -> o card do press-and-hold fica sempre centralizado e o botão
# numa coordenada determinística (centro horizontal, +57px do centro vertical).
VIEWPORT = (1280, 800)
_BUTTON_X = VIEWPORT[0] // 2
_BUTTON_Y = VIEWPORT[1] // 2 + 57

# Cookies que importam pro PerimeterX (o resto é analytics).
PX_COOKIES = ("_px3", "_pxvid", "pxcts", "_pxde")

# `_px3` dura ~10 min; tratamos como velho antes disso pra re-mintar com folga.
SESSION_TTL_SECONDS = int(os.getenv("WEBMOTORS_SESSION_TTL", "480"))

# Script de stealth injetado antes de qualquer JS da página (patch dos tells
# clássicos de automação que o PerimeterX procura).
_STEALTH_JS = """
(() => {
  try { Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => undefined, configurable: true }); } catch (e) {}
  try { if (!window.chrome) window.chrome = {}; if (!window.chrome.runtime) window.chrome.runtime = {}; } catch (e) {}
  try {
    const q = navigator.permissions && navigator.permissions.query;
    if (q) navigator.permissions.query = (p) => p && p.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission }) : q(p);
  } catch (e) {}
  try { Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5], configurable: true }); } catch (e) {}
  try { Object.defineProperty(navigator, 'languages', { get: () => ['pt-BR','pt','en-US','en'], configurable: true }); } catch (e) {}
})();
"""


class WebmotorsBlocked(RuntimeError):
    """Falha em obter uma sessão válida (PerimeterX bloqueou de novo)."""


@dataclass
class WebmotorsSession:
    cookies: dict[str, str]
    user_agent: str
    minted_at: float = field(default_factory=time.time)

    @property
    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    @property
    def age(self) -> float:
        return time.time() - self.minted_at

    def is_fresh(self) -> bool:
        return bool(self.cookies.get("_px3")) and self.age < SESSION_TTL_SECONDS

    def to_dict(self) -> dict[str, Any]:
        return {
            "cookies": self.cookies,
            "user_agent": self.user_agent,
            "minted_at": self.minted_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WebmotorsSession":
        return cls(
            cookies=data["cookies"],
            user_agent=data["user_agent"],
            minted_at=data.get("minted_at", time.time()),
        )


# ==========================================
# Playwright: helpers de browser
# ==========================================

def _is_blocked(page: Page) -> bool:
    marker = f"{page.title()}\n{page.locator('body').inner_text(timeout=5000)[:5000]}".lower()
    return "denied" in marker or "acesso negado" in marker or "pressione e segure" in marker


def _solve_press_and_hold(page: Page, *, attempts: int = 3) -> bool:
    """Resolve o captcha "Pressione e segure" segurando o mouse no botão.

    O botão fica centralizado horizontalmente e ~57px abaixo do centro
    vertical (card de tamanho fixo, centralizado na viewport fixa).
    """
    for i in range(attempts):
        hold_seconds = 7 + i * 1.5
        box = page.locator("#px-captcha").bounding_box(timeout=5000)
        x = (box["x"] + box["width"] / 2) if box else _BUTTON_X
        y = (box["y"] + box["height"] / 2) if box else _BUTTON_Y
        page.mouse.move(x, y)
        time.sleep(0.4)
        page.mouse.down()
        time.sleep(hold_seconds)
        page.mouse.up()
        time.sleep(4)
        if not _is_blocked(page):
            return True
    return not _is_blocked(page)


# ==========================================
# MINT: abre o Chrome real e colhe os cookies
# ==========================================

def mint_session(*, headless: bool = False, verbose: bool = True) -> WebmotorsSession:
    """Abre o Chrome real, passa pelo PerimeterX e devolve uma sessão válida.

    Bypass: se WEBMOTORS_COOKIE estiver no ambiente, usa direto (sem browser).
    """
    env_cookie = os.getenv("WEBMOTORS_COOKIE")
    if env_cookie:
        cookies = dict(
            part.split("=", 1)
            for part in (p.strip() for p in env_cookie.split(";"))
            if "=" in part
        )
        return WebmotorsSession(cookies=cookies, user_agent=DEFAULT_UA)

    def log(msg: str) -> None:
        if verbose:
            print(f"[mint] {msg}", file=sys.stderr)

    try:
        log("lançando Chrome real via Playwright...")
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR,
                executable_path=CHROME_PATH if os.path.exists(CHROME_PATH) else None,
                headless=headless,
                viewport={"width": VIEWPORT[0], "height": VIEWPORT[1]},
                locale="pt-BR",
                user_agent=USER_AGENT_OVERRIDE,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.add_init_script(_STEALTH_JS)
                page.goto(BASE_URL, wait_until="domcontentloaded")
                time.sleep(3)

                if _is_blocked(page):
                    log("press-and-hold detectado, resolvendo...")
                    if not _solve_press_and_hold(page):
                        raise WebmotorsBlocked("não consegui resolver o press-and-hold do PerimeterX")
                    log("captcha resolvido.")
                else:
                    log("passou direto (sem captcha).")

                cookies: dict[str, str] = {}
                for _ in range(8):
                    raw = context.cookies()
                    cookies = {
                        c["name"]: c["value"]
                        for c in raw
                        if "webmotors.com.br" in c.get("domain", "")
                    }
                    if cookies.get("_px3"):
                        break
                    time.sleep(1.5)

                if not cookies.get("_px3"):
                    raise WebmotorsBlocked("sessão sem _px3 após o mint")

                user_agent = USER_AGENT_OVERRIDE or page.evaluate("navigator.userAgent")
                log(f"colhi {len(cookies)} cookies (_px3 ok).")
                return WebmotorsSession(cookies=cookies, user_agent=user_agent)
            finally:
                context.close()
    except PlaywrightError as exc:
        raise WebmotorsBlocked(f"falha ao abrir/controlar o Chrome via Playwright: {exc}") from exc


def close_browser() -> None:
    return None


# ==========================================
# Cache de sessão em disco
# ==========================================

def load_cached_session() -> WebmotorsSession | None:
    if not CACHE_PATH.exists():
        return None
    try:
        sess = WebmotorsSession.from_dict(json.loads(CACHE_PATH.read_text()))
        return sess if sess.is_fresh() else None
    except (json.JSONDecodeError, KeyError):
        return None


def save_cached_session(session: WebmotorsSession) -> None:
    try:
        CACHE_PATH.write_text(json.dumps(session.to_dict()))
    except OSError:
        pass


# ==========================================
# Slug + montagem de URL de detail
# ==========================================

def slugify(value: str) -> str:
    """'1.5 i-VTEC FLEX HATCH EXL CVT' -> '15-i-vtec-flex-hatch-exl-cvt'."""
    value = value.lower().replace(".", "")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return re.sub(r"-+", "-", value).strip("-")


def build_detail_url(
    *, make: str, model: str, version: str, doors: int | str, year: int | str, unique_id: int | str
) -> str:
    return (
        f"{BASE_URL}/api/detail/car/"
        f"{slugify(make)}/{slugify(model)}/{slugify(version)}/"
        f"{int(float(doors))}-portas/{int(float(year))}/{unique_id}"
    )


def detail_url_from_listing(listing: dict[str, Any]) -> str:
    """Monta a URL de detail a partir de um item do /api/search/car."""
    spec = listing["Specification"]
    return build_detail_url(
        make=spec["Make"]["Value"],
        model=spec["Model"]["Value"],
        version=spec["Version"]["Value"],
        doors=spec.get("NumberPorts", 4) or 4,
        year=spec.get("YearModel") or spec.get("YearFabrication"),
        unique_id=listing["UniqueId"],
    )


# ==========================================
# Cliente HTTP (com re-mint automático no 403)
# ==========================================

class WebmotorsClient:
    def __init__(self, *, session: WebmotorsSession | None = None, headless: bool = False):
        self._session = session or load_cached_session()
        self._headless = headless

    # -- sessão ----------------------------------------------------------

    def ensure_session(self, *, force: bool = False) -> WebmotorsSession:
        if force or self._session is None or not self._session.is_fresh():
            self._session = mint_session(headless=self._headless)
            save_cached_session(self._session)
        return self._session

    def _headers(self) -> dict[str, str]:
        sess = self.ensure_session()
        return {
            "accept": "application/json, text/plain, */*",
            "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "user-agent": sess.user_agent,
            "referer": f"{BASE_URL}/",
            "cookie": sess.cookie_header,
        }

    def _get(self, url: str, *, _retried: bool = False) -> requests.Response:
        resp = requests.get(url, headers=self._headers(), timeout=30)
        if resp.status_code in (401, 403) and not _retried:
            # token velho/queimado -> re-minta uma vez e tenta de novo.
            self.ensure_session(force=True)
            return self._get(url, _retried=True)
        return resp

    # -- API -------------------------------------------------------------

    def search(
        self,
        *,
        make: str,
        model: str = "",
        page: int = 1,
        per_page: int = 24,
        order: int = 1,
        extra: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Lista anúncios. Retorna o JSON cru do /api/search/car."""
        inner = {"tipoveiculo": "carros", "marca1": make.lower(), "page": str(page)}
        if model:
            inner["modelo1"] = model.lower()
        if extra:
            inner.update(extra)
        inner_qs = "&".join(f"{k}={v}" for k, v in inner.items())
        inner_url = requests.utils.quote(f"{BASE_URL}/carros/estoque?{inner_qs}", safe="")
        url = (
            f"{BASE_URL}/api/search/car?url={inner_url}"
            f"&displayPerPage={per_page}&actualPage={page}"
            f"&showMenu=true&showCount=true&showBreadCrumb=true"
            f"&order={order}&mediaZeroKm=true"
        )
        resp = self._get(url)
        resp.raise_for_status()
        return resp.json()

    def get_detail(self, *, listing: dict[str, Any] | None = None, url: str | None = None) -> dict[str, Any]:
        """Detalhe completo de um anúncio (a partir de um item da busca ou URL)."""
        if url is None:
            if listing is None:
                raise ValueError("informe listing= ou url=")
            url = detail_url_from_listing(listing)
        resp = self._get(url)
        resp.raise_for_status()
        return resp.json()

    def iter_details(
        self, *, make: str, model: str = "", pages: int = 1, per_page: int = 24
    ) -> list[dict[str, Any]]:
        """Busca + detalha todos os usados (UniqueId > 0) das páginas pedidas."""
        out: list[dict[str, Any]] = []
        for p in range(1, pages + 1):
            results = self.search(make=make, model=model, page=p, per_page=per_page)
            for item in results.get("SearchResults", []):
                if item.get("UniqueId", 0) > 0:
                    out.append(self.get_detail(listing=item))
        return out


# ==========================================
# CLI
# ==========================================

def normalize_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Extrai os campos principais de um payload de detail."""
    spec = detail.get("Specification", {})
    prices = detail.get("Prices", {})
    seller = detail.get("Seller", {})
    return {
        "unique_id": detail.get("UniqueId"),
        "titulo": spec.get("Title"),
        "marca": (spec.get("Make") or {}).get("Value"),
        "modelo": (spec.get("Model") or {}).get("Value"),
        "versao": (spec.get("Version") or {}).get("Value"),
        "ano_modelo": spec.get("YearModel"),
        "ano_fabricacao": spec.get("YearFabrication"),
        "km": spec.get("Odometer"),
        "cor": (spec.get("Color") or {}).get("Primary"),
        "cambio": spec.get("Transmission"),
        "preco": prices.get("Price"),
        "cidade": seller.get("City"),
        "estado": seller.get("State"),
    }


def _main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Scraper real da Webmotors (PerimeterX bypass).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_mint = sub.add_parser("mint", help="abre o browser, resolve o PX e salva a sessão")
    p_mint.add_argument("--headless", action="store_true")

    p_search = sub.add_parser("search", help="lista anúncios")
    p_search.add_argument("--marca", required=True)
    p_search.add_argument("--modelo", default="")
    p_search.add_argument("--page", type=int, default=1)
    p_search.add_argument("--per-page", type=int, default=24)

    p_detail = sub.add_parser("detail", help="detalha N usados de uma busca")
    p_detail.add_argument("--marca", required=True)
    p_detail.add_argument("--modelo", default="")
    p_detail.add_argument("--pages", type=int, default=1)
    p_detail.add_argument("--per-page", type=int, default=12)

    p_url = sub.add_parser("url", help="detalha uma URL de detail direto")
    p_url.add_argument("url")

    args = ap.parse_args(argv)
    client = WebmotorsClient(headless=getattr(args, "headless", False))

    if args.cmd == "mint":
        sess = client.ensure_session(force=True)
        print(json.dumps({"cookies": list(sess.cookies), "user_agent": sess.user_agent}, indent=2))
    elif args.cmd == "search":
        data = client.search(make=args.marca, model=args.modelo, page=args.page, per_page=args.per_page)
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif args.cmd == "detail":
        details = client.iter_details(make=args.marca, model=args.modelo, pages=args.pages, per_page=args.per_page)
        print(json.dumps(details, ensure_ascii=False, indent=2))
    elif args.cmd == "url":
        print(json.dumps(client.get_detail(url=args.url), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
