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
    monkeypatch.setattr(client_mod._http, "post",
                        lambda *a, **k: FakeResp(json_data={"data": {
                            "orders": {"complexity": 101, "data": {"edges": []}}}}))
    data = sh.request("query { orders { complexity data { edges { node { id } } } } }")
    assert data["orders"]["data"]["edges"] == []
    usage = sh.get_credit_usage()
    assert usage["calls"] == 1
    assert usage["credits"] == 101
    assert usage["recent"][-1]["complexity"] == 101


def test_request_raises_shiphero_error_on_graphql_errors(sh, monkeypatch):
    monkeypatch.setattr(client_mod._http, "post",
                        lambda *a, **k: FakeResp(json_data={"errors": [{"code": 30}]}))
    with pytest.raises(ShipHeroError):
        sh.request("{ x }")


def test_request_raises_runtimeerror_with_status_on_http_failure(sh, monkeypatch):
    monkeypatch.setattr(client_mod._http, "post",
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


@pytest.mark.parametrize("status", [502, 503, 504])
def test_graphql_retries_a_read_on_a_gateway_error(sh, monkeypatch, status):
    """ShipHero's gateway failing once must not fail the caller (lojistic-api,
    2026-10-14: one 502 lost the whole morning run)."""
    calls = {"n": 0}

    def flaky(query, variables=None, timeout=30):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError(f"ShipHero {status}: <html><title>{status} Bad Gateway</title></html>")
        return {"ok": True}

    monkeypatch.setattr(sh, "request", flaky)
    monkeypatch.setattr(client_mod.time, "sleep", lambda *_: None)
    assert sh.graphql("query { shipments { data { edges { node { id } } } } }") == {"ok": True}
    assert calls["n"] == 2


def test_graphql_never_retries_a_mutation_on_a_gateway_error(sh, monkeypatch):
    """The gateway can answer 502 after ShipHero applied the write."""
    calls = {"n": 0}

    def gateway(query, variables=None, timeout=30):
        calls["n"] += 1
        raise RuntimeError("ShipHero 502: <html>Bad Gateway</html>")

    monkeypatch.setattr(sh, "request", gateway)
    monkeypatch.setattr(client_mod.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError):
        sh.graphql("# set a size\n  mutation ($d: UpdateProductInput!) { product_update(data: $d) { request_id } }")
    assert calls["n"] == 1


def test_graphql_does_not_retry_a_500(sh, monkeypatch):
    calls = {"n": 0}

    def server_error(query, variables=None, timeout=30):
        calls["n"] += 1
        raise RuntimeError("ShipHero 500: Internal Server Error")

    monkeypatch.setattr(sh, "request", server_error)
    monkeypatch.setattr(client_mod.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError):
        sh.graphql("{ x }")
    assert calls["n"] == 1


def test_graphql_gives_up_on_a_gateway_error_after_its_attempts(sh, monkeypatch):
    calls = {"n": 0}

    def gateway(query, variables=None, timeout=30):
        calls["n"] += 1
        raise RuntimeError("ShipHero 503: Service Unavailable")

    monkeypatch.setattr(sh, "request", gateway)
    monkeypatch.setattr(client_mod.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError):
        sh.graphql("{ x }", attempts=3)
    assert calls["n"] == 3


@pytest.mark.parametrize("query, want", [
    ("mutation { x }", True),
    ("  # comment\n mutation M { x }", True),
    ("query Q { x }", False),
    ("{ x }", False),
    ("", False),
])
def test_is_mutation(query, want):
    assert client_mod._is_mutation(query) is want


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

    monkeypatch.setattr(client_mod._http, "post", fake_post)
    c = ShipHeroClient(refresh_token="r")
    assert c.get_access_token() == "abc"
    assert c.get_access_token() == "abc"
    assert posts["n"] == 1  # second call served from cache


# ---- a refused access token (401) ----

REFUSED = FakeResp(ok=False, status_code=401, text='{"message": "Unauthorized request"}')


class FakeShipHero:
    """Both endpoints: /auth/refresh hands out tok-1, tok-2, ...; /graphql
    answers each call with the next of ``answers`` and records its token."""

    def __init__(self, answers, on_query=None):
        self.answers = list(answers)
        self.on_query = on_query
        self.refreshes = 0
        self.sent_with = []

    def post(self, url, **kwargs):
        if url == client_mod.SHIPHERO_AUTH_URL:
            self.refreshes += 1
            return FakeResp(json_data={"access_token": f"tok-{self.refreshes}",
                                       "expires_in": 3600})
        self.sent_with.append(kwargs["headers"]["Authorization"].split()[-1])
        if self.on_query:
            self.on_query()
        return self.answers.pop(0)


def _client_holding(token):
    """A client still holding a token that has weeks left, as the long-running
    worker was on 2026-11-02."""
    c = ShipHeroClient(refresh_token="r")
    c._token_cache = {"access_token": token, "expires_at": 1e18}
    return c


def test_a_refused_token_is_replaced_and_the_request_sent_again(monkeypatch):
    fake = FakeShipHero([REFUSED, FakeResp(json_data={"data": {"ok": True}})])
    monkeypatch.setattr(client_mod._http, "post", fake.post)
    c = _client_holding("old")
    assert c.request("{ x }") == {"ok": True}
    assert fake.sent_with == ["old", "tok-1"]
    assert fake.refreshes == 1

    fake.answers.append(FakeResp(json_data={"data": {"ok": True}}))
    c.request("{ x }")
    assert fake.sent_with[-1] == "tok-1" and fake.refreshes == 1  # the new one is kept


def test_a_second_refusal_raises_the_401(monkeypatch):
    fake = FakeShipHero([REFUSED, REFUSED])
    monkeypatch.setattr(client_mod._http, "post", fake.post)
    with pytest.raises(RuntimeError) as ei:
        _client_holding("old").request("{ x }")
    assert str(ei.value).startswith("ShipHero 401: ")
    assert fake.sent_with == ["old", "tok-1"] and fake.refreshes == 1


def test_a_refused_mutation_is_sent_again_only_once(monkeypatch):
    """Through graphql() too: its retries never add a third send."""
    fake = FakeShipHero([REFUSED, REFUSED, FakeResp(json_data={"data": {"ok": True}})])
    monkeypatch.setattr(client_mod._http, "post", fake.post)
    monkeypatch.setattr(client_mod.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError, match="^ShipHero 401: "):
        _client_holding("old").graphql(
            "mutation ($d: UpdateOrderInput!) { order_update(data: $d) { request_id } }")
    assert fake.sent_with == ["old", "tok-1"]


def test_a_token_another_thread_already_replaced_is_kept(monkeypatch):
    c = _client_holding("old")

    def another_thread_refreshes():
        if c._token_cache["access_token"] == "old":
            c._token_cache = {"access_token": "theirs", "expires_at": 1e18}

    fake = FakeShipHero([REFUSED, FakeResp(json_data={"data": {"ok": True}})],
                        on_query=another_thread_refreshes)
    monkeypatch.setattr(client_mod._http, "post", fake.post)
    assert c.request("{ x }") == {"ok": True}
    assert fake.sent_with == ["old", "theirs"] and fake.refreshes == 0


@pytest.mark.parametrize("status", [403, 500])
def test_only_a_401_gets_a_new_token(monkeypatch, status):
    fake = FakeShipHero([FakeResp(ok=False, status_code=status, text="no")])
    monkeypatch.setattr(client_mod._http, "post", fake.post)
    with pytest.raises(RuntimeError, match=f"^ShipHero {status}: "):
        _client_holding("old").request("{ x }")
    assert fake.sent_with == ["old"] and fake.refreshes == 0


# ---- shared credit ledger (Phase 3) ----

class FakeStore:
    def __init__(self, gate_results=None):
        self.gate_calls = 0
        self.charges = []
        self._results = list(gate_results or [{"ok": True}])

    def gate(self):
        self.gate_calls += 1
        return self._results[min(self.gate_calls - 1, len(self._results) - 1)]

    def charge(self, cost):
        self.charges.append(cost)
        return {"ok": True}


def _client_with_store(store):
    c = ShipHeroClient(refresh_token="r", credit_store=store)
    c._token_cache = {"access_token": "tok", "expires_at": 1e18}
    return c


def test_credit_gate_before_and_charge_after(monkeypatch):
    store = FakeStore([{"ok": True}])
    c = _client_with_store(store)
    monkeypatch.setattr(client_mod._http, "post", lambda *a, **k: FakeResp(
        json_data={"data": {"orders": {"complexity": 42, "data": {}}}}))
    c.request("query { orders { complexity data { x } } }")
    assert store.gate_calls == 1
    assert client_mod._charger.flush()  # the charge is paid off the caller's thread
    assert store.charges == [42]  # charged the actual complexity


def test_credit_no_charge_without_complexity(monkeypatch):
    store = FakeStore([{"ok": True}])
    c = _client_with_store(store)
    monkeypatch.setattr(client_mod._http, "post", lambda *a, **k: FakeResp(
        json_data={"data": {"account": {"data": {}}}}))
    c.request("{ account { data { id } } }")
    assert client_mod._charger.flush()
    assert store.gate_calls == 1 and store.charges == []


def test_credit_waits_then_proceeds(monkeypatch):
    store = FakeStore([{"ok": False, "wait_seconds": 0.5}, {"ok": True}])
    c = _client_with_store(store)
    slept = []
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(client_mod._http, "post",
                        lambda *a, **k: FakeResp(json_data={"data": {"ok": True}}))
    c.request("{ x }")
    assert store.gate_calls == 2 and slept == [0.5]


def test_credit_fail_open_on_store_error(monkeypatch):
    class BadStore:
        def gate(self): raise RuntimeError("ledger down")
        def charge(self, cost): raise RuntimeError("ledger down")

    c = _client_with_store(BadStore())
    monkeypatch.setattr(client_mod._http, "post",
                        lambda *a, **k: FakeResp(json_data={"data": {"ok": True}}))
    assert c.request("{ x }") == {"ok": True}  # ledger outage never blocks ShipHero
