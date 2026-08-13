"""The single ShipHero GraphQL client shared by ops-portal and dgs-returns.

Both apps used to keep parallel copies of this auth / transport / retry code
against the *same* 4004-credit ShipHero account. This is the one canonical
implementation they now both import.

A :class:`ShipHeroClient` is parameterised by *which* refresh-token env var it
authenticates with, so several tokens can coexist — the app-wide token and
ops-portal's separate billing-reads token each get their own instance and token
cache — without a shared module global getting in the way.

Two call styles:

* :meth:`ShipHeroClient.request` — single-shot, raises on the first error. This
  is exactly what ops-portal's ``_graphql_request`` did; callers that want
  resilience wrap it themselves (e.g. the SLA sync's patient credit-retry loop).
* :meth:`ShipHeroClient.graphql` — waits out ShipHero's shared-credit
  exhaustion (code 30) using the deficit ShipHero reports, and retries transient
  timeouts. Bounded and request-path safe by default; the returns grading /
  restock flow uses this.

Every call is metered (:meth:`get_credit_usage`) — the seed for the
cross-process shared credit ledger planned for the backend merge.
"""
import os
import time

import requests

from .errors import ShipHeroError

SHIPHERO_AUTH_URL = "https://public-api.shiphero.com/auth/refresh"
SHIPHERO_GRAPHQL_URL = "https://public-api.shiphero.com/graphql"

_USAGE_LOG_MAX = 50  # recent calls kept for get_credit_usage()


def _op_snippet(query):
    """A short one-line label for a query, for the usage log."""
    if not query:
        return ""
    return " ".join(str(query).split())[:80]


class ShipHeroClient:
    """A ShipHero GraphQL client bound to one refresh token."""

    def __init__(self, refresh_token_env="SHIPHERO_REFRESH_TOKEN", *,
                 refresh_token=None, auth_url=SHIPHERO_AUTH_URL,
                 graphql_url=SHIPHERO_GRAPHQL_URL,
                 credit_store=None, gate_max_wait=30.0):
        self._refresh_token_env = refresh_token_env
        self._refresh_token = refresh_token  # explicit value wins over the env var
        self._auth_url = auth_url
        self._graphql_url = graphql_url
        self._token_cache = {"access_token": None, "expires_at": 0}
        self._usage = {"calls": 0, "credits": 0, "log": []}
        # Optional shared-credit coordinator (Phase 3): a duck-typed object with
        # gate() -> {"ok", "wait_seconds"} and charge(cost). Every store call is
        # best-effort (see _await_credit / _charge_credit), so a ledger outage can
        # never block a ShipHero request for either app.
        self._credit_store = credit_store
        self._gate_max_wait = gate_max_wait

    # ---- auth -------------------------------------------------------------
    def get_access_token(self):
        """A valid access token, refreshed from the refresh token when expired.
        Raises ``RuntimeError`` (never a bare ``HTTPError``) so callers can
        surface a clean message — a bad/missing token is the most common
        misconfig."""
        c = self._token_cache
        if c["access_token"] and time.time() < c["expires_at"]:
            return c["access_token"]

        token = self._refresh_token or os.environ.get(self._refresh_token_env)
        if not token:
            raise RuntimeError(f"{self._refresh_token_env} not set")

        try:
            resp = requests.post(self._auth_url,
                                 json={"refresh_token": token}, timeout=10)
        except requests.RequestException as e:
            raise RuntimeError(f"ShipHero auth unreachable: {e}") from e
        if not resp.ok:
            c["access_token"] = None
            c["expires_at"] = 0
            raise RuntimeError(
                f"ShipHero auth failed ({resp.status_code}) "
                f"— check {self._refresh_token_env}")
        data = resp.json()
        c["access_token"] = data["access_token"]
        c["expires_at"] = time.time() + data.get("expires_in", 3600) - 60
        return c["access_token"]

    # ---- transport --------------------------------------------------------
    def request(self, query, variables=None, timeout=30):
        """Single-shot GraphQL call. Returns the ``data`` object; raises
        :class:`ShipHeroError` on GraphQL errors and ``RuntimeError`` on HTTP
        failure. No retry — the exact contract ops-portal's ``_graphql_request``
        had (the HTTP-error message still starts with ``ShipHero <status>`` so
        callers that string-match ``"429"`` keep working)."""
        self._await_credit()  # shared-budget gate (fail-open; no-op without a store)
        headers = {"Authorization": f"Bearer {self.get_access_token()}",
                   "Content-Type": "application/json"}
        resp = requests.post(
            self._graphql_url,
            json={"query": query, "variables": variables or {}},
            headers=headers, timeout=timeout,
        )
        if not resp.ok:
            raise RuntimeError(f"ShipHero {resp.status_code}: {resp.text[:500]}")
        result = resp.json()
        if "errors" in result:
            raise ShipHeroError(result["errors"])
        data = result["data"]
        complexity = self._record_usage(query, data)
        self._charge_credit(complexity)  # deduct the actual cost from the shared budget
        return data

    def graphql(self, query, variables=None, timeout=30, *,
                attempts=3, credit_attempts=3, max_credit_wait=12.0):
        """Resilient GraphQL call.

        Waits out credit exhaustion (code 30) using the deficit ShipHero reports
        — buffered by 1s and capped at ``max_credit_wait`` so a single worker is
        never parked for long — and retries transient timeouts / 429s / auth
        blips a few times. Raises once either budget is spent.

        Defaults are tuned for the request path (a shared, bounded wait). Bulk
        off-request callers (e.g. an overnight sync) can raise ``credit_attempts``
        / ``max_credit_wait`` for a more patient budget."""
        attempt = 0
        credit_tries = 0
        while True:
            attempt += 1
            try:
                return self.request(query, variables, timeout=timeout)
            except ShipHeroError as exc:
                wait = exc.credit_wait
                if wait is None:
                    raise  # a non-credit GraphQL error — don't spin on it
                credit_tries += 1
                if credit_tries > credit_attempts:
                    raise
                time.sleep(min(max(wait, 1.0) + 1.0, max_credit_wait))
            except RuntimeError as exc:
                msg = str(exc).lower()
                transient = ("timed out" in msg or "timeout" in msg
                             or "429" in msg or "unreachable" in msg)
                if attempt >= attempts or not transient:
                    raise
                time.sleep(min(2 ** (attempt - 1), 8))

    # ---- usage accounting -------------------------------------------------
    def _record_usage(self, query, data):
        u = self._usage
        u["calls"] += 1
        complexity = None
        if isinstance(data, dict):
            # ShipHero reports per-query cost as `complexity` on each queried root.
            for v in data.values():
                if isinstance(v, dict) and isinstance(v.get("complexity"), (int, float)):
                    complexity = (complexity or 0) + v["complexity"]
        if complexity:
            u["credits"] += complexity
        u["log"].append({"op": _op_snippet(query), "complexity": complexity})
        if len(u["log"]) > _USAGE_LOG_MAX:
            del u["log"][:-_USAGE_LOG_MAX]
        return complexity

    # ---- shared credit ledger (best-effort; never blocks a ShipHero call) --
    def _await_credit(self):
        """Before a query: consult the shared credit budget and wait out a
        depletion, bounded by ``gate_max_wait``. Fail-open — any store error just
        proceeds, so a ledger outage can't take ShipHero down for either app."""
        store = self._credit_store
        if store is None:
            return
        waited = 0.0
        while True:
            try:
                r = store.gate()
            except Exception:
                return  # fail open
            if not isinstance(r, dict) or r.get("ok", True):
                return
            wait = float(r.get("wait_seconds") or 0)
            if wait <= 0 or waited >= self._gate_max_wait:
                return  # give up; ShipHero's own code-30 retry covers true exhaustion
            wait = min(wait, self._gate_max_wait - waited, 5.0)
            time.sleep(wait)
            waited += wait

    def _charge_credit(self, complexity):
        """After a query: deduct the actual complexity from the shared budget.
        Best-effort."""
        if self._credit_store is None or not complexity:
            return
        try:
            self._credit_store.charge(complexity)
        except Exception:
            pass  # fail open

    def get_credit_usage(self):
        """Accumulated ShipHero usage for this process session::

            {"calls": int, "credits": int, "recent": [{"op", "complexity"}, ...]}

        ``credits`` sums the ``complexity`` ShipHero returns on queries that
        request that field (calls without it still count toward ``calls``). This
        per-call metering is the hook the cross-process shared credit ledger
        (backend-merge phase 2) will persist."""
        u = self._usage
        return {"calls": u["calls"], "credits": u["credits"],
                "recent": list(u["log"])}


def default_client():
    """A client bound to the app-wide ``SHIPHERO_REFRESH_TOKEN``. Each app builds
    its own module-level client; this is a convenience for scripts/tests."""
    return ShipHeroClient("SHIPHERO_REFRESH_TOKEN")
