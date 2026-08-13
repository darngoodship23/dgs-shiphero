"""Unit tests for the shared ShipHero client — no network, requests is faked."""
import pytest

from dgs_shiphero import ShipHeroClient, ShipHeroError
from dgs_shiphero import client as client_mod


class FakeResp:
    def __init__(self, *, ok=True, status_code=200, json_data=None, text=""):
        self.ok = ok
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json


@pytest.fixture
def sh(monkeypatch):
    """A client whose access token is already valid, so no auth round-trip."""
    c = ShipHeroClient(refresh_token="fake-refresh")
    c._token_cache = {"access_token": "tok", "expires_at": 1e18}
    return c


# ---- ShipHeroError.credit_wait (contract shared with ops-portal SLA tests) ----

def test_credit_wait_from_deficit():
    err = ShipHeroError([{"code": 30, "required_credits": 26, "remaining_credits": 7}])
    assert err.credit_wait == pytest.approx((26 - 7) / 60.0)


def test_credit_wait_from_time_remaining_string():
    err = ShipHeroError([{"code": 30, "time_remaining": "in 12 seconds",
                          "required_credits": 100, "remaining_credits": 0}])
    assert err.credit_wait == 12.0


def test_credit_wait_none_for_non_credit_error():
    assert ShipHeroError([{"code": 20, "message": "bad query"}]).credit_wait is None


# ---- request() ----

def test_request_returns_data_and_meters_complexity(sh, monkeypatch):
    monkeypatch.setattr(client_mod.requests, "post",
                        lambda *a, **k: FakeResp(json_data={"data": {
                            "orders": {"complexity": 101, "data": {"edges": []}}}}))
    data = sh.request("query { orders { complexity data { edges { node { id } } } } }")
    assert data["orders"]["data"]["edges"] == []
    usage = sh.get_credit_usage()
    assert usage["calls"] == 1
    assert usage["credits"] == 101
    assert usage["recent"][-1]["complexity"] == 101


def test_request_raises_shiphero_error_on_graphql_errors(sh, monkeypatch):
    monkeypatch.setattr(client_mod.requests, "post",
                        lambda *a, **k: FakeResp(json_data={"errors": [{"code": 30}]}))
    with pytest.raises(ShipHeroError):
        sh.request("{ x }")


def test_request_raises_runtimeerror_with_status_on_http_failure(sh, monkeypatch):
    monkeypatch.setattr(client_mod.requests, "post",
                        lambda *a, **k: FakeResp(ok=False, status_code=429, text="slow down"))
    with pytest.raises(RuntimeError) as ei:
        sh.request("{ x }")
    assert "429" in str(ei.value)  # string-matching callers rely on this


# ---- graphql() retry ----

def test_graphql_waits_out_credit_error_then_succeeds(sh, monkeypatch):
    calls = {"n": 0}

    def fake_request(query, variables=None, timeout=30):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ShipHeroError([{"code": 30, "required_credits": 5, "remaining_credits": 4}])
        return {"ok": True}

    monkeypatch.setattr(sh, "request", fake_request)
    monkeypatch.setattr(client_mod.time, "sleep", lambda *_: None)  # no real waiting
    assert sh.graphql("{ x }") == {"ok": True}
    assert calls["n"] == 2


def test_graphql_gives_up_after_credit_attempts(sh, monkeypatch):
    def always_credit(query, variables=None, timeout=30):
        raise ShipHeroError([{"code": 30, "required_credits": 5, "remaining_credits": 0}])

    monkeypatch.setattr(sh, "request", always_credit)
    monkeypatch.setattr(client_mod.time, "sleep", lambda *_: None)
    with pytest.raises(ShipHeroError):
        sh.graphql("{ x }", credit_attempts=2)


def test_graphql_does_not_retry_non_credit_error(sh, monkeypatch):
    calls = {"n": 0}

    def bad_query(query, variables=None, timeout=30):
        calls["n"] += 1
        raise ShipHeroError([{"code": 20, "message": "bad query"}])

    monkeypatch.setattr(sh, "request", bad_query)
    with pytest.raises(ShipHeroError):
        sh.graphql("{ x }")
    assert calls["n"] == 1  # not spun on


# ---- auth ----

def test_missing_token_raises_named_env(monkeypatch):
    monkeypatch.delenv("SHIPHERO_REFRESH_TOKEN", raising=False)
    with pytest.raises(RuntimeError) as ei:
        ShipHeroClient().get_access_token()
    assert "SHIPHERO_REFRESH_TOKEN" in str(ei.value)


def test_token_is_cached(monkeypatch):
    posts = {"n": 0}

    def fake_post(url, **k):
        posts["n"] += 1
        return FakeResp(json_data={"access_token": "abc", "expires_in": 3600})

    monkeypatch.setattr(client_mod.requests, "post", fake_post)
    c = ShipHeroClient(refresh_token="r")
    assert c.get_access_token() == "abc"
    assert c.get_access_token() == "abc"
    assert posts["n"] == 1  # second call served from cache
