"""
Tests for WebmotorsSession I/O: cookie_header, age, is_fresh, to_dict/from_dict,
save/load cache, and mint_session bypass branch.

Fixtures consumed from conftest.py:
- webmotors_cookie_env  : sets WEBMOTORS_COOKIE so mint_session skips Chrome
- cache_in_tmp          : patches wm.CACHE_PATH to a tmp_path file, returns Path
- clear_lru_caches      : autouse, clears LRU singletons between tests
"""

from __future__ import annotations

import json
import time

import pytest

import webmotors_scraper as wm
from webmotors_scraper import (
    SESSION_TTL_SECONDS,
    WebmotorsSession,
    load_cached_session,
    mint_session,
    save_cached_session,
)


# ---------------------------------------------------------------------------
# Helpers (keyword-only params per project rule)
# ---------------------------------------------------------------------------


def make_session(
    *,
    px3: str = "abc",
    pxvid: str = "def",
    user_agent: str = "TestUA/1.0",
    minted_at: float | None = None,
) -> WebmotorsSession:
    """Build a WebmotorsSession with sane defaults. All params keyword-only."""
    cookies: dict[str, str] = {}
    if px3:
        cookies["_px3"] = px3
    if pxvid:
        cookies["_pxvid"] = pxvid
    kwargs: dict = {"cookies": cookies, "user_agent": user_agent}
    if minted_at is not None:
        kwargs["minted_at"] = minted_at
    return WebmotorsSession(**kwargs)


# ---------------------------------------------------------------------------
# WebmotorsSession.cookie_header
# ---------------------------------------------------------------------------


class TestCookieHeader:
    def test_basic_two_cookies(self) -> None:
        """Exact format: 'k1=v1; k2=v2' (semicolon-space separator, insertion order)."""
        # Use a dict literal so insertion order is guaranteed (Python 3.7+).
        sess = WebmotorsSession(
            cookies={"_px3": "abc", "_pxvid": "def"},
            user_agent="UA/1",
        )
        assert sess.cookie_header == "_px3=abc; _pxvid=def"

    def test_single_cookie(self) -> None:
        sess = WebmotorsSession(cookies={"_px3": "xyz"}, user_agent="UA/1")
        assert sess.cookie_header == "_px3=xyz"

    def test_four_cookies_order(self) -> None:
        """All four PX cookies in insertion order."""
        cookies = {"_px3": "x", "_pxvid": "y", "pxcts": "z", "_pxde": "w"}
        sess = WebmotorsSession(cookies=cookies, user_agent="UA/1")
        assert sess.cookie_header == "_px3=x; _pxvid=y; pxcts=z; _pxde=w"

    def test_empty_cookies(self) -> None:
        sess = WebmotorsSession(cookies={}, user_agent="UA/1")
        assert sess.cookie_header == ""


# ---------------------------------------------------------------------------
# WebmotorsSession.age
# ---------------------------------------------------------------------------


class TestAge:
    def test_age_is_nonnegative(self) -> None:
        """Age should always be >= 0 for a just-created session."""
        sess = WebmotorsSession(cookies={"_px3": "v"}, user_agent="UA")
        assert sess.age >= 0.0

    def test_age_with_known_past_minted_at(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Freeze time.time() to confirm age computation is exactly now - minted_at."""
        frozen_now = 1_000_000.0
        monkeypatch.setattr(wm.time, "time", lambda: frozen_now)

        past = frozen_now - 120.0
        sess = WebmotorsSession(cookies={"_px3": "v"}, user_agent="UA", minted_at=past)
        assert sess.age == pytest.approx(120.0, abs=0.001)

    def test_age_grows_relative_to_minted_at(self) -> None:
        """Without monkeypatching, capture 'now' and verify age is within tolerance."""
        before = time.time()
        # minted_at exactly 60 s in the past
        past = before - 60.0
        sess = WebmotorsSession(cookies={"_px3": "v"}, user_agent="UA", minted_at=past)
        after = time.time()
        # age must be between 60 and (60 + elapsed since before)
        assert 60.0 <= sess.age <= 60.0 + (after - before) + 0.01


# ---------------------------------------------------------------------------
# WebmotorsSession.is_fresh()
# ---------------------------------------------------------------------------


class TestIsFresh:
    def test_fresh_with_px3_and_recent_minted_at(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_px3 present and age < TTL → True."""
        frozen_now = 1_000_000.0
        monkeypatch.setattr(wm.time, "time", lambda: frozen_now)

        sess = WebmotorsSession(
            cookies={"_px3": "some_token"},
            user_agent="UA",
            minted_at=frozen_now,  # age == 0
        )
        assert sess.is_fresh() is True

    def test_not_fresh_without_px3(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No _px3 key → False even if just minted."""
        frozen_now = 1_000_000.0
        monkeypatch.setattr(wm.time, "time", lambda: frozen_now)

        sess = WebmotorsSession(
            cookies={"_pxvid": "y"},  # _px3 absent
            user_agent="UA",
            minted_at=frozen_now,
        )
        assert sess.is_fresh() is False

    def test_not_fresh_with_empty_px3(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_px3 present but empty string → bool("") is False → not fresh."""
        frozen_now = 1_000_000.0
        monkeypatch.setattr(wm.time, "time", lambda: frozen_now)

        sess = WebmotorsSession(
            cookies={"_px3": ""},  # falsy value
            user_agent="UA",
            minted_at=frozen_now,
        )
        assert sess.is_fresh() is False

    def test_not_fresh_when_age_equals_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """age == SESSION_TTL_SECONDS: is_fresh uses strict <, so this is False."""
        frozen_now = 1_000_000.0
        monkeypatch.setattr(wm.time, "time", lambda: frozen_now)

        sess = WebmotorsSession(
            cookies={"_px3": "tok"},
            user_agent="UA",
            minted_at=frozen_now - SESSION_TTL_SECONDS,  # age == TTL exactly
        )
        # age < TTL is False when equal → is_fresh() is False
        assert sess.is_fresh() is False

    def test_not_fresh_when_age_exceeds_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """age > TTL (e.g. now - 500 s with TTL=480) → False."""
        frozen_now = 1_000_000.0
        monkeypatch.setattr(wm.time, "time", lambda: frozen_now)

        sess = WebmotorsSession(
            cookies={"_px3": "tok"},
            user_agent="UA",
            minted_at=frozen_now - 500,  # 500 > 480 (default TTL)
        )
        assert sess.is_fresh() is False

    def test_fresh_just_below_ttl_boundary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """age one second below TTL → still fresh."""
        frozen_now = 1_000_000.0
        monkeypatch.setattr(wm.time, "time", lambda: frozen_now)

        sess = WebmotorsSession(
            cookies={"_px3": "tok"},
            user_agent="UA",
            minted_at=frozen_now - (SESSION_TTL_SECONDS - 1),
        )
        assert sess.is_fresh() is True


# ---------------------------------------------------------------------------
# to_dict / from_dict round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_round_trip_preserves_all_fields(self) -> None:
        original = WebmotorsSession(
            cookies={"_px3": "abc", "_pxvid": "def", "pxcts": "ghi", "_pxde": "jkl"},
            user_agent="Mozilla/5.0 (Test)",
            minted_at=1_700_000_000.0,
        )
        restored = WebmotorsSession.from_dict(original.to_dict())

        assert restored.cookies == original.cookies
        assert restored.user_agent == original.user_agent
        assert restored.minted_at == original.minted_at

    def test_to_dict_keys(self) -> None:
        sess = make_session()
        d = sess.to_dict()
        assert set(d.keys()) == {"cookies", "user_agent", "minted_at"}

    def test_from_dict_without_minted_at_uses_current_time(self) -> None:
        """from_dict falls back to time.time() if minted_at is absent (via .get())."""
        before = time.time()
        sess = WebmotorsSession.from_dict({"cookies": {"_px3": "x"}, "user_agent": "UA"})
        after = time.time()
        assert before <= sess.minted_at <= after

    def test_round_trip_cookie_header_unchanged(self) -> None:
        original = WebmotorsSession(
            cookies={"_px3": "tok1", "_pxvid": "tok2"},
            user_agent="UA",
            minted_at=999_000.0,
        )
        restored = WebmotorsSession.from_dict(original.to_dict())
        assert restored.cookie_header == original.cookie_header


# ---------------------------------------------------------------------------
# save_cached_session + load_cached_session
# ---------------------------------------------------------------------------


class TestCacheIO:
    def test_save_then_load_returns_equal_session(
        self, cache_in_tmp: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Save a fresh session and load it back; all fields preserved."""
        frozen_now = 1_000_000.0
        monkeypatch.setattr(wm.time, "time", lambda: frozen_now)

        sess = WebmotorsSession(
            cookies={"_px3": "abc", "_pxvid": "def"},
            user_agent="TestUA",
            minted_at=frozen_now,  # fresh: age == 0
        )
        save_cached_session(session=sess)
        loaded = load_cached_session()

        assert loaded is not None
        assert loaded.cookies == sess.cookies
        assert loaded.user_agent == sess.user_agent
        assert loaded.minted_at == sess.minted_at

    def test_load_returns_none_when_file_absent(self, cache_in_tmp: object) -> None:
        """Cache file does not exist → None, no exception."""
        result = load_cached_session()
        assert result is None

    def test_load_returns_none_for_corrupt_json(self, cache_in_tmp) -> None:
        """Corrupt file content → None (code catches json.JSONDecodeError)."""
        cache_path = cache_in_tmp  # conftest returns the Path
        cache_path.write_text("{not valid json!!!}")
        result = load_cached_session()
        assert result is None

    def test_load_returns_none_for_invalid_structure(self, cache_in_tmp) -> None:
        """Valid JSON but missing required keys → None (code catches KeyError)."""
        cache_path = cache_in_tmp
        cache_path.write_text(json.dumps({"wrong_key": "no cookies here"}))
        result = load_cached_session()
        assert result is None

    def test_load_returns_none_for_stale_session(
        self, cache_in_tmp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Saved session with old minted_at → is_fresh() False → load returns None."""
        frozen_now = 2_000_000.0
        monkeypatch.setattr(wm.time, "time", lambda: frozen_now)

        old_sess = WebmotorsSession(
            cookies={"_px3": "token"},
            user_agent="UA",
            minted_at=frozen_now - 600,  # 600 > 480 → stale
        )
        save_cached_session(session=old_sess)
        loaded = load_cached_session()
        assert loaded is None

    def test_load_returns_none_for_session_without_px3(
        self, cache_in_tmp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Session without _px3 is never fresh → load returns None."""
        frozen_now = 2_000_000.0
        monkeypatch.setattr(wm.time, "time", lambda: frozen_now)

        sess = WebmotorsSession(
            cookies={"_pxvid": "y"},  # no _px3
            user_agent="UA",
            minted_at=frozen_now,
        )
        # Write raw JSON directly to bypass is_fresh check in save
        cache_in_tmp.write_text(json.dumps(sess.to_dict()))
        loaded = load_cached_session()
        assert loaded is None


# ---------------------------------------------------------------------------
# mint_session bypass (WEBMOTORS_COOKIE env set)
# ---------------------------------------------------------------------------


class TestMintSessionBypass:
    def test_bypass_returns_session_with_parsed_cookies(
        self, webmotors_cookie_env: None
    ) -> None:
        """Bypass branch parses 'k=v; k2=v2' into cookies dict correctly."""
        sess = mint_session()
        assert isinstance(sess, WebmotorsSession)
        assert sess.cookies.get("_px3") == "x"
        assert sess.cookies.get("_pxvid") == "y"
        assert sess.cookies.get("pxcts") == "z"
        assert sess.cookies.get("_pxde") == "w"

    def test_bypass_uses_default_ua(self, webmotors_cookie_env: None) -> None:
        """User-agent is set to DEFAULT_UA (not empty, not None)."""
        sess = mint_session()
        # DEFAULT_UA is the module constant; bypass uses it directly
        assert sess.user_agent == wm.DEFAULT_UA
        assert sess.user_agent  # truthy

    def test_bypass_session_is_fresh(
        self, webmotors_cookie_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bypass session has _px3 and was just minted → is_fresh() True."""
        frozen_now = 1_000_000.0
        monkeypatch.setattr(wm.time, "time", lambda: frozen_now)

        sess = mint_session()
        assert sess.is_fresh() is True

    def test_bypass_does_not_invoke_browser(
        self, webmotors_cookie_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PROVE the Chrome branch is never taken: sync_playwright raises if called."""

        def explode(*args, **kwargs):  # noqa: ANN001, ANN202
            raise AssertionError(
                "sync_playwright was called; Chrome branch taken unexpectedly"
            )

        monkeypatch.setattr(wm, "sync_playwright", explode)

        # Must succeed without opening Chrome
        sess = mint_session()
        assert sess is not None
        assert sess.cookies.get("_px3") == "x"
