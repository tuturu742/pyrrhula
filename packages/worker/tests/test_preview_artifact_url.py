"""Where a preview container fetches its artifact from.

Regression test for a live failure: the preview pod runs in ``pyrrhula-envs`` while the
api Service lives in ``pyrrhula``, so the short name in the global ``git_http_base``
setting does not resolve there and the container died with
``URLError: Name or service not known``. The engine declaration carries an FQDN override
for exactly this reason, and the delegation work script already honours it -- previews
must too.
"""

from __future__ import annotations

import json

import pytest

from worker.preview import _artifact_url

_FQDN = "http://pyrrhula-api.pyrrhula.svc.cluster.local:8000"


def _reset_settings() -> None:
    """Settings are cached in a module global, not lru_cache -- drop it so the patched
    environment is actually read."""
    import core.config

    core.config._settings = None  # noqa: SLF001 -- the only reset seam there is


@pytest.fixture
def k8s_engine(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "PYRRHULA_EXEC_ENGINES",
        json.dumps(
            [
                {
                    "key": "k8s",
                    "kind": "kubernetes",
                    "namespace": "pyrrhula-envs",
                    "git_http_base": _FQDN,
                }
            ]
        ),
    )
    _reset_settings()
    yield
    _reset_settings()


def test_engine_git_http_base_wins_over_the_global_setting(k8s_engine) -> None:
    url = _artifact_url("t1234567-vgame", "game-web.tar.gz", "k8s")
    assert url.startswith(_FQDN), (
        "a preview in another namespace cannot resolve the short Service name"
    )
    assert url.endswith("/git/t1234567-vgame/artifact?name=game-web.tar.gz")


def test_unknown_engine_key_falls_back_to_the_deployment_default(k8s_engine) -> None:
    """The API records engine_key=None when it has no engine declaration of its own;
    the worker must still resolve the cluster's FQDN rather than the short name."""
    assert _artifact_url("t1234567-vgame", "game-web.tar.gz", None).startswith(_FQDN)


def test_falls_back_to_settings_when_the_engine_declares_no_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "PYRRHULA_EXEC_ENGINES",
        json.dumps([{"key": "local", "kind": "socket", "socket": "/x.sock"}]),
    )
    monkeypatch.setenv("PYRRHULA_GIT_HTTP_BASE", "http://pyrrhula_api_1:8000")
    _reset_settings()
    try:
        url = _artifact_url("t1-r", "a.tar.gz", "local")
        assert url.startswith("http://pyrrhula_api_1:8000")
    finally:
        _reset_settings()
