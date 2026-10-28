from fastapi.testclient import TestClient

from app.main import create_app


def client(settings, container) -> TestClient:
    return TestClient(create_app(settings, container))


def test_dashboard_meta_and_backtest(settings, container):
    with client(settings, container) as http:
        dash = http.get("/api/dashboard").json()
        assert dash["ready"] and dash["broker"]["label"] == "로컬 시뮬레이션"
        assert dash["loss"]["limit"] == 300_000

        meta = http.get("/api/strategies/meta").json()
        assert {t["id"] for t in meta["templates"]} == {"momentum", "meanrev", "news", "value"}
        news = next(t for t in meta["templates"] if t["id"] == "news")
        assert news["available"] is False  # OpenAI 키 없음

        bt = http.post("/api/backtest", json={"template": "meanrev", "universe": "k200", "amount": 20_000_000, "stop_pct": 5, "take_pct": 10, "max_weight_pct": 15}).json()
        assert bt["ok"] and len(bt["curve"]) > 50 and bt["trades"] > 0


def test_onboarding_creates_default_strategy(settings, container):
    with client(settings, container) as http:
        assert http.put("/api/settings", json={"risk_profile": "neutral", "invest_amount": 20_000_000, "daily_loss_limit": 300_000, "limit_action": "buy", "agreed": True}).status_code == 200
        res = http.post("/api/onboarding/complete").json()
        assert res["onboarded"] and res["has_strategy"]
        strategies = http.get("/api/strategies").json()
        assert strategies[0]["template"] == "meanrev" and strategies[0]["exec_mode"] == "approve"


def test_mode_and_kill_switch(settings, container):
    with client(settings, container) as http:
        assert http.post("/api/control/mode", json={"mode": "auto"}).status_code == 200
        assert http.get("/api/dashboard").json()["status"]["mode"] == "auto"
        assert http.post("/api/control/stop").json() == {"cancelled": 0}
        assert http.get("/api/dashboard").json()["status"]["running"] is False
        assert http.post("/api/decisions/D-NONE/approve", json={}).status_code == 409


def test_sp500_rejected_for_kiwoom_only_strategy(settings, container):
    container.broker.kind = "kiwoom"
    with client(settings, container) as http:
        r = http.post("/api/strategies", json={"template": "meanrev", "universe": "sp500", "amount": 1_000_000, "stop_pct": 5, "take_pct": 10, "max_weight_pct": 15})
        assert r.status_code == 400
