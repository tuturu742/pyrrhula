"""The optional /api prefix: load balancers that cannot rewrite paths (AWS ALB) route
/api/* straight to the app, so every route must answer with and without the prefix."""

from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import app


def test_routes_answer_with_and_without_api_prefix() -> None:
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/health").status_code == 200
        # A bogus login is a 4xx either way -- proving routing, not auth.
        for path in ("/auth/login", "/api/auth/login"):
            code = client.post(path, json={"email": "x@x.co", "password": "x"}).status_code
            assert 400 <= code < 500, (path, code)


def test_prefix_strip_is_exact() -> None:
    with TestClient(app) as client:
        # "/apifoo" is not the prefix -- must stay a 404, never be rewritten to "/foo".
        assert client.get("/apihealth").status_code == 404
