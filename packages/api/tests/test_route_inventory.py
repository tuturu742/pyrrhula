"""T0.6's route-inventory acceptance criterion: no route reachable without a resolved
principal, except the documented health/auth exceptions. Walks FastAPI's dependant tree
(not just each route's direct dependencies) so a dependency-of-a-dependency — e.g.
``rate_limit_by_principal`` pulling in ``get_request_context`` — still counts.

Route enumeration recurses through ``_IncludedRouter`` wrappers: this FastAPI/Starlette
version doesn't flatten ``include_router()``'d routes into ``app.routes`` as plain
``APIRoute`` objects — each included router shows up as one opaque wrapper whose actual
routes live on ``.original_router.routes``. Walking only ``app.routes`` directly would
make every test here vacuously pass (nothing to check), which is worse than not having
the test at all.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from api.main import app
from api.middleware.auth import get_request_context
from api.middleware.rate_limit import (
    rate_limit_by_ip,
    rate_limit_by_principal,
    rate_limit_by_tenant,
)
from api.routes.admin import require_platform_admin

_EXEMPT_PATHS = {"/health", "/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"}
# /git: smart-HTTP for exec environments -- authenticated inside the handlers by the
# short-lived per-repo job token in the URL (mint_git_job_token), which git clients can
# send but a Depends(get_request_context) chain cannot express.
# /p/: preview share links -- a running build handed to a human tester who has no
# account, so requiring a session would defeat the feature. Authenticated inside the
# handler by a signed token in the path that carries its own tenant id, which the handler
# uses to open a normal tenant_scope (core/previews/tokens.py); RLS applies as usual.
_EXEMPT_PREFIXES = ("/auth", "/git/", "/p/")

# Routes gated by an authenticator other than get_request_context. require_platform_admin
# accepts the ops token OR resolves a normal admin-tenant JWT (calling
# get_request_context itself, outside the Depends graph).
_ALTERNATE_AUTHENTICATORS = {require_platform_admin}


def _iter_api_routes(routes: list[Any]) -> Iterator[Any]:
    for route in routes:
        nested_router = getattr(route, "original_router", None)
        if nested_router is not None:
            yield from _iter_api_routes(nested_router.routes)
        elif getattr(route, "path", None) is not None and hasattr(route, "dependant"):
            yield route  # APIRoute only -- plain Starlette Route (docs/openapi) has no dependant


def _all_routes() -> list[Any]:
    routes = list(_iter_api_routes(app.routes))
    assert len(routes) >= 5, (
        f"only found {len(routes)} routes -- route enumeration is probably broken again, "
        "not that the app genuinely has this few routes"
    )
    return routes


def _dependency_closure(dependant: Any) -> set[Any]:
    seen: set[Any] = set()
    stack = [dependant]
    while stack:
        current = stack.pop()
        if current.call is not None:
            seen.add(current.call)
        stack.extend(current.dependencies)
    return seen


def _is_authenticated(route: Any) -> bool:
    closure = _dependency_closure(route.dependant)
    return get_request_context in closure or bool(_ALTERNATE_AUTHENTICATORS & closure)


def test_all_non_health_non_auth_routes_require_request_context() -> None:
    offenders: list[str] = []

    for route in _all_routes():
        path = route.path
        if path in _EXEMPT_PATHS or any(path.startswith(p) for p in _EXEMPT_PREFIXES):
            continue
        if not _is_authenticated(route):
            offenders.append(path)

    assert not offenders, f"routes reachable without a resolved principal: {offenders}"


def test_exempt_routes_really_are_health_and_auth_only() -> None:
    """Guards the allowlist itself: if a *new* route needs exemption, that has to be a
    deliberate decision made in this test, not a silent expansion of the allowlist."""
    exempt_in_practice = [route.path for route in _all_routes() if not _is_authenticated(route)]
    for path in exempt_in_practice:
        assert path in _EXEMPT_PATHS or any(path.startswith(p) for p in _EXEMPT_PREFIXES), (
            f"unexpected unauthenticated route: {path}"
        )


def test_auth_routes_are_ip_rate_limited() -> None:
    auth_routes = [r for r in _all_routes() if r.path.startswith("/auth")]
    assert len(auth_routes) >= 2, "expected register/login routes to exist"

    for route in auth_routes:
        if route.path == "/auth/logout":
            continue  # not brute-forceable; no credentials to guess
        assert rate_limit_by_ip in _dependency_closure(route.dependant), (
            f"{route.path} is not rate-limited by IP"
        )


def test_me_route_is_principal_and_tenant_rate_limited() -> None:
    for route in _all_routes():
        if route.path == "/me":
            closure = _dependency_closure(route.dependant)
            assert rate_limit_by_principal in closure
            assert rate_limit_by_tenant in closure
            return
    raise AssertionError("/me route not found")
