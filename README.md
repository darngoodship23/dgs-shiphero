# dgs-shiphero

The single ShipHero GraphQL client shared by **ops-portal** (`tools.darngoodship.com`)
and **dgs-returns** (`returns.darngoodship.com`). Both apps authenticate against
the same 4004-credit ShipHero account; keeping one client means one place for
auth, transport, credit-retry, error shape, and usage metering — and no drift.

## Install

Pinned from git in each app's `requirements.txt`:

```
dgs-shiphero @ git+https://github.com/darngoodship23/dgs-shiphero@v0.3.0
```

Local dev (editable), from a checkout next to the app:

```
pip install -e ../dgs-shiphero
```

## Use

```python
from dgs_shiphero import ShipHeroClient, ShipHeroError

sh = ShipHeroClient()                       # uses $SHIPHERO_REFRESH_TOKEN
billing = ShipHeroClient("SHIPHERO_BILLING_REFRESH_TOKEN")  # a second token

data = sh.request(query, variables)         # single-shot; raises on first error
data = sh.graphql(query, variables)         # waits out credit exhaustion, bounded

sh.get_credit_usage()   # {"calls", "credits", "recent": [...]} for this process
```

- `request()` is the exact single-shot contract ops-portal's `_graphql_request`
  had. Callers that want a patient, bespoke retry (the SLA overnight sync) keep
  wrapping it themselves.
- `graphql()` is request-path safe by default (bounded credit waits); pass
  `credit_attempts=` / `max_credit_wait=` to widen the budget for bulk jobs.
- `ShipHeroError.credit_wait` returns the seconds to wait on a code-30
  credit-exhaustion error (else `None`).

## Transport and the credit ledger (0.3.0)

- Every HTTP call goes through `dgs_shiphero._http`: one kept-alive
  `requests.Session` per thread (and per process, so a gunicorn fork never
  shares its parent's socket). ShipHero calls and the ledger's gate/charge no
  longer open a new TLS connection each time.
- With a credit store, the **gate** still happens before the query, but the
  **charge** is queued on a background thread (`credit.BackgroundCharger`,
  bounded at 256) instead of making the caller wait for a second Supabase
  round trip. Failures are counted and logged (at most one line a minute
  during an outage); a charge that finds the backlog full is dropped, counted
  and logged. `get_credit_usage()["ledger"]` reports charged / failed /
  dropped / pending / last_error.
- Tests: patch `dgs_shiphero._http.post` (a patch of `requests.post` no
  longer reaches the client). `tests/conftest.py` fails any test whose
  request gets as far as a real adapter.

## Scope

Deliberately just the shared *core*: auth, transport, retry, error, usage.
Domain helpers (orders, boxes, returns, restock, webhooks, …) stay in each app
where they belong.

## Versioning

Bump `version` in `pyproject.toml` and `__init__.py`, tag `vX.Y.Z`, and update
the pin in both apps. Both are pinned to a tag so neither moves unexpectedly.
