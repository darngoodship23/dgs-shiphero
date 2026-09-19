"""dgs_shiphero — the single ShipHero GraphQL client shared by ops-portal and
dgs-returns.

Both apps authenticate against the *same* 4004-credit ShipHero account and used
to maintain parallel copies of the auth / transport / retry / error logic. This
package is that logic, once.

Typical use::

    from dgs_shiphero import ShipHeroClient
    sh = ShipHeroClient()                     # SHIPHERO_REFRESH_TOKEN
    data = sh.request("{ account { data { id } } }")     # single-shot
    data = sh.graphql(query, variables)                  # credit-resilient
"""
from .client import (
    SHIPHERO_AUTH_URL,
    SHIPHERO_GRAPHQL_URL,
    ShipHeroClient,
    default_client,
)
from .credit import PostgrestCreditStore
from .errors import ShipHeroError

__all__ = [
    "ShipHeroClient",
    "ShipHeroError",
    "PostgrestCreditStore",
    "SHIPHERO_AUTH_URL",
    "SHIPHERO_GRAPHQL_URL",
    "default_client",
]
__version__ = "0.3.0"
