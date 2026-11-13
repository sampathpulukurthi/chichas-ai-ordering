import sys
from pathlib import Path
from unittest.mock import MagicMock
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from app.main import app, db

client = TestClient(app)

def test_db_unconfigured_returns_503():
    import app.main as m
    old_session = m.SessionLocal
    try:
        m.SessionLocal = None
        app.dependency_overrides.clear()
        resp = client.get("/api/v1/bolna/inbound-context")
        assert resp.status_code == 503
    finally:
        m.SessionLocal = old_session

def test_bolna_check_item_requires_parameter():
    mock_session = MagicMock()
    app.dependency_overrides[db] = lambda: mock_session
    try:
        resp = client.get("/api/v1/bolna/check-item")
        assert resp.status_code == 400
        assert "Item name query parameter or body required" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()

def test_bolna_check_item_post_validation():
    mock_session = MagicMock()
    app.dependency_overrides[db] = lambda: mock_session
    try:
        resp = client.post("/api/v1/bolna/check-item", json={"item_name": ""})
        assert resp.status_code == 400
    finally:
        app.dependency_overrides.clear()

def test_bolna_order_webhook_validation():
    mock_session = MagicMock()
    app.dependency_overrides[db] = lambda: mock_session
    try:
        # Empty items list should fail validation
        resp = client.post(
            "/api/v1/bolna/order-webhook",
            headers={"Authorization": "Bearer chichas-bolna-secret-2026"},
            json={
                "contact_number": "+17622405663",
                "items": []
            }
        )
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()

def test_bolna_order_webhook_flat_schema_creates_order():
    # Test flat schema with item_name and quantity
    resp = client.post(
        "/api/v1/bolna/order-webhook",
        headers={"Authorization": "Bearer chichas-bolna-secret-2026"},
        json={
            "customer_name": "Flat Webcall",
            "contact_number": "Webcall",
            "item_name": "Chicken 65",
            "quantity": 2
        }
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "PENDING_APPROVAL"
    assert "CHI-" in data["reference"]
    assert "total_formatted" in data

