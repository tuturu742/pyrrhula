from fastapi.testclient import TestClient

from api.main import app


def test_health() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_reports_the_running_version() -> None:
    """An operator running a pulled image has no source tree to read a version out of.
    If this ever answers "unknown" in a container, the image did not install the
    distribution and every other version claim about that deployment is a guess."""
    with TestClient(app) as client:
        version = client.get("/health").json()["version"]
    assert version and version != "unknown"
