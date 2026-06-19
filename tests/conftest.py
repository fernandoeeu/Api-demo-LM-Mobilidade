"""
Shared test fixtures and factories for the Webmotors scraper test suite.

Import resolution: pyproject.toml sets pythonpath=["."] so the repo root is
on sys.path. Both `webmotors_scraper` and `vehicle_search` import cleanly.

Mock library: requests-mock (pytest fixture `requests_mock` is available to
any test function that declares it as an argument — zero decorator needed).

Sequential response example (403 → 200):
    requests_mock.get(
        "https://www.webmotors.com.br/api/...",
        [
            {"status_code": 403},
            {"json": payload, "status_code": 200},
        ],
    )
"""

from __future__ import annotations

import json
import copy
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Raw fixture loaders — return parsed dict, not a copy (callers should copy
# if they mutate). These are session-scoped so the files are read once.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def search_payload() -> dict[str, Any]:
    """Full /api/search/car response (15 SearchResults, mix of 0km + used)."""
    return json.loads((FIXTURES_DIR / "search.json").read_text())


@pytest.fixture(scope="session")
def detail_payload() -> dict[str, Any]:
    """Full /api/detail/car response (UniqueId=69863170, Honda City, Prata)."""
    return json.loads((FIXTURES_DIR / "detail.json").read_text())


@pytest.fixture(scope="session")
def search_item_used_payload() -> dict[str, Any]:
    """Single used search item (UniqueId=71923214, Price float, YearModel float)."""
    return json.loads((FIXTURES_DIR / "search_item_used.json").read_text())


@pytest.fixture(scope="session")
def search_item_zerokm_payload() -> dict[str, Any]:
    """Single 0km search item (UniqueId=0, has AdvertisementLink)."""
    return json.loads((FIXTURES_DIR / "search_item_zerokm.json").read_text())


# ---------------------------------------------------------------------------
# Factories — produce independent copies so mutations in one test don't leak.
# All parameters are keyword-only (project rule: no positional params).
# ---------------------------------------------------------------------------


def make_listing(
    *,
    unique_id: int = 71923214,
    price: float = 115900.0,
    make: str = "HONDA",
    model: str = "CITY",
    version: str = "1.5 i-VTEC FLEX EXL CVT",
    year_model: float = 2024.0,
    year_fabrication: str = "2024",
    odometer: float = 33384.0,
    number_ports: str = "4",
    color: str = "Cinza",
    transmission: str = "Automática",
    title: str = "HONDA CITY 1.5 i-VTEC FLEX EXL CVT",
) -> dict[str, Any]:
    """
    Build a search result item (used car, UniqueId > 0).

    Prices.Price is a FLOAT (as the real API returns in /api/search/car).
    Seller is present in the real search fixture but NOT required by
    normalize_detail — omitted here to keep factories minimal; add via
    extra_seller if needed.

    Usage:
        listing = make_listing(price=99000.0, year_model=2023.0)
    """
    return {
        "UniqueId": unique_id,
        "Prices": {"Price": price, "SearchPrice": price},
        "Specification": {
            "Title": title,
            "Make": {"Value": make, "id": 16},
            "Model": {"Value": model, "id": 3053},
            "Version": {"Value": version, "id": 348898},
            "YearModel": year_model,
            "YearFabrication": year_fabrication,
            "Odometer": odometer,
            "NumberPorts": number_ports,
            "Transmission": transmission,
            "Color": {"Primary": color, "IdPrimary": "30405"},
            "Armored": "N",
            "Auction": False,
        },
        "MediaZeroKm": False,
        "AdvertisementLink": "",
    }


def make_detail(
    *,
    unique_id: int = 69863170,
    price: str = "90000",
    make: str = "HONDA",
    model: str = "CITY",
    version: str = "1.5 i-VTEC FLEX HATCH EXL CVT",
    year_model: str = "2022",
    year_fabrication: str = "2022",
    odometer: str = "70000",
    number_ports: str = "4",
    color: str = "Prata",
    transmission: str = "CVT",
    title: str = "HONDA CITY 1.5 i-VTEC FLEX HATCH EXL CVT",
    seller_city: str | None = "Volta Redonda",
    seller_state: str | None = "Rio de Janeiro (RJ)",
) -> dict[str, Any]:
    """
    Build a detail item (/api/detail/car response).

    Prices.Price is a STRING (as the real API returns in /api/detail/car).
    Seller is present when seller_city/seller_state are provided (not None).

    Usage:
        detail = make_detail(price="115900", seller_city=None, seller_state=None)
    """
    payload: dict[str, Any] = {
        "UniqueId": unique_id,
        "Prices": {"Price": price, "SearchPrice": price},
        "Specification": {
            "Title": title,
            "Make": {"Value": make, "id": 16},
            "Model": {"Value": model, "id": 3053},
            "Version": {"Value": version, "id": 348912},
            "YearModel": year_model,
            "YearFabrication": year_fabrication,
            "Odometer": odometer,
            "NumberPorts": number_ports,
            "Transmission": transmission,
            "Color": {"Primary": color, "IdPrimary": "30409"},
            "Armored": "N",
        },
        "Type": "car",
        "ListingType": "U",
    }
    if seller_city is not None or seller_state is not None:
        payload["Seller"] = {
            "City": seller_city,
            "State": seller_state,
            "SellerType": "PF",
            "Id": 8164410,
        }
    return payload


# ---------------------------------------------------------------------------
# Environment / session fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def webmotors_cookie_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Set WEBMOTORS_COOKIE so mint_session takes the no-browser bypass branch.

    mint_session() checks os.getenv("WEBMOTORS_COOKIE") first; when set it
    parses the cookie string and returns a WebmotorsSession without touching
    Playwright or Chrome.
    """
    monkeypatch.setenv(
        "WEBMOTORS_COOKIE",
        "_px3=x; _pxvid=y; pxcts=z; _pxde=w",
    )


@pytest.fixture()
def cache_in_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """
    Point WEBMOTORS_CACHE at a per-test tmp_path directory.

    Returns the Path to where the cache file will be written so tests can
    inspect or pre-populate it.

    Note: webmotors_scraper reads CACHE_PATH at module import time as a
    module-level constant. We patch the constant directly on the module so
    the already-imported value is updated.
    """
    import webmotors_scraper as wm

    cache_file = tmp_path / "wm-session.json"
    monkeypatch.setattr(wm, "CACHE_PATH", cache_file)
    monkeypatch.setenv("WEBMOTORS_CACHE", str(cache_file))
    return cache_file


# ---------------------------------------------------------------------------
# lru_cache cleanup — autouse so singletons never bleed between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_lru_caches() -> None:
    """
    Clear get_webmotors_client and get_tavily_client lru_cache singletons
    before every test so a stale client from one test never leaks into the
    next.
    """
    import vehicle_search as vs

    vs.get_webmotors_client.cache_clear()
    vs.get_tavily_client.cache_clear()
    yield
    vs.get_webmotors_client.cache_clear()
    vs.get_tavily_client.cache_clear()
