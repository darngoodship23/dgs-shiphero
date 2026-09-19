"""Kept-alive sessions and the background credit charge. No network: the
conftest refuses any request that reaches an adapter, and every test here fakes
the transport or the store."""
import logging
import threading
import time

import pytest
import requests

from dgs_shiphero import PostgrestCreditStore, ShipHeroClient
from dgs_shiphero import _http
from dgs_shiphero import client as client_mod
from dgs_shiphero.credit import BackgroundCharger


class FakeResp:
    def __init__(self, json_data=None, ok=True, status_code=200):
        self._json, self.ok, self.status_code, self.text = json_data or {}, ok, status_code, ""

    def json(self):
        return self._json

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code}")


# ---------------------------------------------------------------- sessions

def test_one_session_per_thread_reused_between_calls():
    first, again = _http.session(), _http.session()
    assert first is again and isinstance(first, requests.Session)

    other = []
    t = threading.Thread(target=lambda: other.append(_http.session()))
    t.start(); t.join()
    assert other[0] is not first


def test_a_forked_child_does_not_reuse_its_parents_session(monkeypatch):
    """gunicorn --preload forks after import; a shared socket would interleave
    two processes' bytes on one connection."""
    parent = _http.session()
    monkeypatch.setattr(_http.os, "getpid", lambda: -12345)
    assert _http.session() is not parent


def test_every_client_call_goes_through_the_session(monkeypatch):
    seen = []

    class FakeSession:
        def post(self, url, **kwargs):
            seen.append((url, kwargs.get("timeout")))
            if url.endswith("/auth/refresh"):
                return FakeResp({"access_token": "t", "expires_in": 3600})
            return FakeResp({"data": {"ok": True}})

    monkeypatch.setattr(_http, "session", lambda: FakeSession())
    c = ShipHeroClient(refresh_token="r")
    assert c.request("{ x }") == {"ok": True}
    assert [url for url, _ in seen] == [client_mod.SHIPHERO_AUTH_URL, client_mod.SHIPHERO_GRAPHQL_URL]
    assert all(timeout for _, timeout in seen)


def test_the_ledger_store_uses_the_session_too(monkeypatch):
    seen = []
    monkeypatch.setattr(_http, "post", lambda url, **kw: seen.append((url, kw["timeout"])) or
                        FakeResp({"ok": True}))
    store = PostgrestCreditStore("https://x.supabase.co/", "key", timeout=5)
    store.gate()
    store.charge(12)
    assert [u.rsplit("/", 1)[-1] for u, _ in seen] == ["shiphero_gate", "shiphero_charge"]
    assert {t for _, t in seen} == {5}


# -------------------------------------------------------- background charge

class SlowStore:
    def __init__(self, delay=0.0, fail=False):
        self.delay, self.fail, self.charges = delay, fail, []

    def gate(self):
        return {"ok": True}

    def charge(self, cost):
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("ledger down")
        self.charges.append(cost)


def _client(store, monkeypatch):
    monkeypatch.setattr(client_mod._http, "post", lambda *a, **k: FakeResp(
        {"data": {"orders": {"complexity": 7, "data": {}}}}))
    c = ShipHeroClient(refresh_token="r", credit_store=store)
    c._token_cache = {"access_token": "tok", "expires_at": 1e18}
    return c


def test_the_caller_does_not_wait_for_the_charge(monkeypatch):
    store = SlowStore(delay=0.5)
    c = _client(store, monkeypatch)
    started = time.monotonic()
    c.request("query { orders { complexity data { x } } }")
    assert time.monotonic() - started < 0.3
    assert client_mod._charger.flush(timeout=3)
    assert store.charges == [7]


def test_a_failed_charge_is_counted_and_logged_never_raised(monkeypatch, caplog):
    charger = BackgroundCharger()
    monkeypatch.setattr(client_mod, "_charger", charger)
    c = _client(SlowStore(fail=True), monkeypatch)
    with caplog.at_level(logging.WARNING, logger="dgs_shiphero"):
        c.request("query { orders { complexity data { x } } }")
        assert charger.flush()
    stats = c.get_credit_usage()["ledger"]
    assert stats["failed"] == 1 and stats["last_error"] == "RuntimeError: ledger down"
    assert "credit ledger charge of 7 failed" in caplog.text


def test_a_full_backlog_drops_and_says_so(monkeypatch, caplog):
    gate = threading.Event()

    class Stuck:
        def charge(self, cost):
            gate.wait(5)

    charger = BackgroundCharger(maxsize=1)
    with caplog.at_level(logging.WARNING, logger="dgs_shiphero"):
        charger.submit(Stuck(), 1)       # taken by the worker, which then blocks
        time.sleep(0.05)
        charger.submit(Stuck(), 2)       # fills the queue
        charger.submit(Stuck(), 3)       # no room: dropped, counted, logged
    gate.set()
    assert charger.flush()
    assert charger.stats()["dropped"] == 1
    assert "backlog full" in caplog.text


def test_an_outage_logs_about_once_a_minute_not_once_a_call(monkeypatch, caplog):
    charger = BackgroundCharger()
    with caplog.at_level(logging.WARNING, logger="dgs_shiphero"):
        for cost in range(20):
            charger.submit(SlowStore(fail=True), cost)
        assert charger.flush()
    assert charger.stats()["failed"] == 20
    assert len([r for r in caplog.records if "failed" in r.getMessage()]) == 1
