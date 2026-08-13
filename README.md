# dgs-shiphero

The single ShipHero GraphQL client shared by **ops-portal** (`tools.darngoodship.com`)
and **dgs-returns** (`returns.darngoodship.com`). Both apps authenticate against
the same 4004-credit ShipHero account; keeping one client means one place for
auth, transport, credit-retry, error shape, and usage metering — and no drift.

## Install

Pinned from git in each app's `requirements.txt`:

```
dgs-shiphero @ git+https://github.com/darngoodship23/dgs-shiphero@v0.1.0
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

## Scope

Deliberately just the shared *core*: auth, transport, retry, error, usage.
Domain helpers (orders, boxes, returns, restock, webhooks, …) stay in each app
where they belong.

## Versioning

Bump `version` in `pyproject.toml` and `__init__.py`, tag `vX.Y.Z`, and update
the pin in both apps. Both are pinned to a tag so neither moves unexpectedly.
