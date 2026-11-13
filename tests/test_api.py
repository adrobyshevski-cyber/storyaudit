from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_live_is_public():
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_api_requires_access_token():
    response = client.get("/scans")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
