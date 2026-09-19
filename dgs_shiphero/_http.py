"""One kept-alive HTTP session per thread, for ShipHero and the credit ledger.

Every call used to be a bare ``requests.post``: a new TCP connection and TLS
handshake per ShipHero query, and with the credit ledger on (Phase 3) two more
per query to Supabase -- the gate before and the charge after. A
``requests.Session`` keeps the connection open between calls, so after the
first call each is one round trip rather than three or four.

Per THREAD, because a Session is not documented as thread-safe (its adapters
and cookie jar are shared mutable state) and both apps call ShipHero from
thread pools. Per PROCESS as well: gunicorn forks workers after import
(``--preload``), and a child that inherited its parent's open socket would
interleave its bytes with the parent's on the same connection. The owner's pid
is kept beside each session and a mismatch starts a fresh one.
"""
import os
import threading

import requests

_local = threading.local()


def session():
    """This thread's Session, made on first use (and again after a fork)."""
    s = getattr(_local, "session", None)
    if s is None or getattr(_local, "pid", None) != os.getpid():
        s = requests.Session()
        _local.session, _local.pid = s, os.getpid()
    return s


def post(url, **kwargs):
    """``requests.post`` on this thread's kept-alive session.

    The one seam every HTTP call in this package goes through, so a test
    replaces ``dgs_shiphero._http.post`` and nothing can reach the network."""
    return session().post(url, **kwargs)
