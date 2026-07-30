import pytest
import sys
import os
import time

# Add trilateration-app/server to path
server_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../trilateration-app/server"))
sys.path.append(server_dir)

import server

@pytest.fixture
def client():
    server.app.config['TESTING'] = True
    with server.app.test_client() as client:
        yield client

def test_location_update_updates_state(client):
    """Test that POST /api/location/update updates position and gps in GET /api/state."""
    payload = {
        "device_id": "TEST_DEV01",
        "x": 10.5,
        "y": 15.2,
        "rssi": -65,
        "sos_flag": False,
        "timestamp": time.time()
    }
    response = client.post("/api/location/update", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert data.get("status") == "success"

    # Verify state payload contains position and gps
    state_res = client.get("/api/state")
    assert state_res.status_code == 200
    state_data = state_res.get_json()

    assert state_data["position"] is not None
    assert round(state_data["position"]["x"], 1) == 10.5
    assert round(state_data["position"]["y"], 1) == 15.2
    assert state_data["gps"] is not None
    assert "lat" in state_data["gps"]
    assert "lng" in state_data["gps"]

def test_batch_update_location(client):
    """Test that POST /api/gateway/batch-update processes locations correctly."""
    payload = {
        "locations": [
            {
                "device_id": "BATCH_DEV01",
                "x": 5.0,
                "y": 8.0,
                "rssi": -70,
                "sos_flag": False,
                "timestamp": time.time()
            }
        ]
    }
    response = client.post("/api/gateway/batch-update", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert data.get("data", {}).get("processed") == 1
