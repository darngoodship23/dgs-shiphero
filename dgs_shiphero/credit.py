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

**The charge is paid off the caller's thread** (``BackgroundCharger``). The gate
has to happen before a query, so a page waits on it; the charge only records
what the query already cost, and the page used to wait on that too -- a second
Supabase round trip after every ShipHero call, for bookkeeping nobody was
waiting to read. It goes on a bounded queue drained by one worker thread per
process. Nothing about it is silent: a failed charge is counted and logged, and
so is one dropped because the queue was full (a ledger that far behind is
already wrong, and blocking the page to catch it up would bring back the wait).
"""
import logging
import os
import queue
import threading
import time

from . import _http

log = logging.getLogger("dgs_shiphero")


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
        r = _http.post(self._gate_url, json={"p_floor": self._floor},
                       headers=self._headers, timeout=self._timeout)
        r.raise_for_status()
        return r.json()

    def charge(self, cost):
        r = _http.post(self._charge_url, json={"p_cost": cost},
                       headers=self._headers, timeout=self._timeout)
        r.raise_for_status()
        return r.json()


class BackgroundCharger:
    """Runs ``store.charge(cost)`` on a worker thread instead of the caller's.

    One worker per process, started on first use and again after a fork (a
    thread does not survive ``fork()``, and gunicorn forks after import).
    ``maxsize`` bounds the backlog. ``stats()`` reports what was charged,
    what failed and what was dropped; ``flush()`` waits for the backlog, for
    tests and for a script about to exit.
    """

    LOG_EVERY = 60.0      # seconds between failure log lines, so an outage is one line a minute

    def __init__(self, maxsize=256):
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._queue = None
        self._pid = None
        self.charged = 0
        self.failed = 0
        self.dropped = 0
        self.last_error = None
        self._last_log = 0.0
        self._unlogged = 0

    def _worker_queue(self):
        if self._pid != os.getpid():
            with self._lock:
                if self._pid != os.getpid():
                    q = queue.Queue(self._maxsize)
                    threading.Thread(target=self._drain, args=(q,), daemon=True,
                                     name="dgs-shiphero-credit-charge").start()
                    self._queue, self._pid = q, os.getpid()
        return self._queue

    def submit(self, store, cost):
        """Queue a charge; returns at once. Never raises."""
        try:
            self._worker_queue().put_nowait((store, cost))
        except queue.Full:
            self.dropped += 1
            self._complain(f"credit ledger backlog full ({self._maxsize}); "
                           f"a {cost}-credit charge was not recorded")
        except Exception as exc:  # noqa: BLE001 -- a thread that won't start, say
            self.failed += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._complain(f"could not queue a {cost}-credit charge: {self.last_error}")

    def _drain(self, q):
        while True:
            store, cost = q.get()
            try:
                store.charge(cost)
                self.charged += 1
            except Exception as exc:  # noqa: BLE001 -- counted and logged, never raised
                self.failed += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._complain(f"credit ledger charge of {cost} failed: {self.last_error}")
            finally:
                q.task_done()

    def _complain(self, message):
        now = time.monotonic()
        if now - self._last_log >= self.LOG_EVERY:
            extra = f" (and {self._unlogged} more since the last report)" if self._unlogged else ""
            log.warning("%s%s", message, extra)
            self._last_log, self._unlogged = now, 0
        else:
            self._unlogged += 1

    def flush(self, timeout=5.0):
        """Wait up to `timeout` seconds for queued charges. True if all done."""
        q = self._queue
        if q is None or self._pid != os.getpid():
            return True
        deadline = time.monotonic() + timeout
        while q.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return not q.unfinished_tasks

    def stats(self):
        pending = self._queue.unfinished_tasks if self._queue is not None else 0
        return {"charged": self.charged, "failed": self.failed, "dropped": self.dropped,
                "pending": pending, "last_error": self.last_error}
