import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from app.main import app, policy
from datetime import datetime
from zoneinfo import ZoneInfo

client = TestClient(app)

def test_health():
    assert client.get('/health').status_code == 200

def test_owner_page():
    assert client.get('/owner/menu').status_code == 200

def test_staff_menu_requires_auth():
    assert client.get('/api/v1/staff/menu').status_code == 401

def test_paused_restaurant_rejects_orders():
    result = policy({'sat': '10:00-22:00'}, True, 0, datetime(2026, 10, 31, 15, tzinfo=ZoneInfo('America/New_York')))
    assert result['accepting_orders'] is False
    assert result['reason'] == 'OWNER_PAUSED'
