"""Shared ShipHero credit ledger (Phase 3).

Both apps hit one ShipHero account with a shared 4004-credit budget. A *credit
store* coordinates that budget across processes via the token-bucket functions in
the shared dgs-ops database (``shiphero_gate`` / ``shiphero_charge``). Inject one
into ``ShipHeroClient(credit_store=...)`` and the client gates before each query
and charges the actual complexity after.

All store calls are best-effort — ``ShipHeroClient`` wraps them so a ledger
outage can never block a ShipHero request (fail-open).

``PostgrestCreditStore`` reaches the functions over Supabase PostgREST RPC using
the service-role key (the functions are service_role-only). It works for any app
that can reach the shared project's REST endpoint; a psycopg-backed store is a
reasonable alternative for a process that already holds a DB connection.
"""
import requests


class PostgrestCreditStore:
    """Credit store backed by ``shiphero_gate`` / ``shiphero_charge`` exposed via
    Supabase PostgREST RPC. Construct with the shared project's URL and its
    service-role key."""

    def __init__(self, base_url, service_key, *, floor=1, timeout=8):
        self._gate_url = base_url.rstrip("/") + "/rest/v1/rpc/shiphero_gate"
        self._charge_url = base_url.rstrip("/") + "/rest/v1/rpc/shiphero_charge"
        self._floor = floor
        self._timeout = timeout
        self._headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        }

    def gate(self):
        """Returns ``{"ok": bool, "available": float, "wait_seconds"?: float}``."""
        r = requests.post(self._gate_url, json={"p_floor": self._floor},
                          headers=self._headers, timeout=self._timeout)
        r.raise_for_status()
        return r.json()

    def charge(self, cost):
        r = requests.post(self._charge_url, json={"p_cost": cost},
                          headers=self._headers, timeout=self._timeout)
        r.raise_for_status()
        return r.json()
