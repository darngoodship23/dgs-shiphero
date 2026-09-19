"""No test in this package may reach the network.

The client's HTTP goes through dgs_shiphero._http (a kept-alive Session per
thread), which a patch of ``requests.post`` does not intercept -- so a test
that forgot to fake the transport would quietly call ShipHero. Every request
that gets as far as an adapter fails the test instead.
"""
import pytest
import requests


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(self, request, **kwargs):
        raise AssertionError(f"a test tried to reach the network: {request.method} {request.url}")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", refuse)
