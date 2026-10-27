def test_health_reports_degraded_when_neo4j_down(client, monkeypatch):
    monkeypatch.setattr("app.main.check_neo4j", lambda: False)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["components"]["neo4j"] is False


def test_metrics_endpoint_exposes_request_counter(client):
    client.get("/health")
    body = client.get("/metrics").text
    assert "http_requests_total" in body
