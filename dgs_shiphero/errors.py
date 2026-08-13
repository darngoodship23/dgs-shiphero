"""ShipHero GraphQL error type.

Kept in its own module so both the client and callers can import it without a
cycle. Behaviour (message format + ``credit_wait``) is carried over verbatim
from ops-portal's ``rts/shiphero.py`` so existing ``except RuntimeError`` /
``except ShipHeroError`` handlers and the SLA credit-retry tests keep passing.
"""
import re


class ShipHeroError(RuntimeError):
    """A GraphQL-level error returned by ShipHero.

    Subclasses ``RuntimeError`` so existing ``except RuntimeError`` /
    ``except Exception`` handlers keep catching it and ``str(exc)`` is unchanged
    — while giving callers structured access to the error list so they can react
    to specific codes (notably credit exhaustion, code 30).
    """

    def __init__(self, errors):
        self.errors = errors or []
        super().__init__(f"ShipHero API error: {self.errors}")

    @property
    def credit_wait(self):
        """Seconds to wait before retrying if this is a credit-exhaustion error
        (code 30), else ``None``. ShipHero's GraphQL credit budget refills at
        ~60/sec and the error reports required vs remaining credits (and a
        ``time_remaining`` estimate), so callers can wait out the deficit rather
        than fail."""
        for err in self.errors:
            if not isinstance(err, dict):
                continue
            is_credit = err.get("code") == 30 or \
                "not enough credits" in str(err.get("message", "")).lower()
            if not is_credit:
                continue
            tr = err.get("time_remaining")
            if isinstance(tr, str):
                m = re.search(r"\d+", tr)
                if m:
                    return float(m.group())
            deficit = max(0, (err.get("required_credits") or 0)
                          - (err.get("remaining_credits") or 0))
            return deficit / 60.0
        return None
