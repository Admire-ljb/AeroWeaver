import time
import pytest

pytest.importorskip("mpe2")
from flask import Flask
from sim.mpe_api import create_mpe_blueprint


def test_mpe_api_lifecycle_and_catalog():
    app = Flask(__name__)
    app.register_blueprint(create_mpe_blueprint())
    client = app.test_client()
    assert len(client.get("/api/mpe/catalog").json["scenarios"]) == 9
    response = client.post("/api/mpe/sessions", json={"scenario": "simple_crypto", "max_cycles": 3})
    assert response.status_code == 200
    key = response.json["id"]
    url = "/api/mpe/sessions/" + key
    assert response.json["roles"]["eve_0"]["role"] == "eavesdropper"
    assert client.post(url + "/step").status_code == 200
    assert client.get(url).json["round"] == 1
    assert client.post(url + "/run").status_code == 200
    deadline = time.monotonic() + 3
    while client.get(url).json["running"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert client.get(url).json["done"]
    assert len(client.get(url + "/trace").json["trace"]) == 3
    assert client.delete(url).status_code == 200
    assert client.get(url).status_code == 404
    assert client.post("/api/mpe/sessions", json={"scenario": "not_native"}).status_code == 400


def test_map_landmarks_endpoint_has_valid_path():
    import server
    response = server.app.test_client().get("/api/map/landmarks")
    assert response.status_code == 200
    assert isinstance(response.json["landmarks"], list)
