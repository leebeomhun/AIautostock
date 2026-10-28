import json

import httpx
import pytest

from app.kiwoom.broker import KiwoomBroker
from app.kiwoom.client import KiwoomClient, KiwoomError, to_int


class FakeKiwoom:
    """키움 REST 서버 흉내. 받은 요청을 기록하고 api-id 별로 준비한 응답을 돌려줍니다."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict]] = []
        self.token_count = 0
        self.responses: dict[str, list] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        api_id = request.headers.get("api-id", "")
        self.calls.append((api_id, dict(request.headers), body))
        if request.url.path == "/oauth2/token":
            self.token_count += 1
            return httpx.Response(200, json={"expires_dt": "20991231235959", "token_type": "bearer", "token": f"T{self.token_count}", "return_code": 0, "return_msg": "ok"})
        queue = self.responses.get(api_id)
        if not queue:
            return httpx.Response(200, json={"return_code": 1505, "return_msg": "없는 API"})
        item = queue.pop(0)
        headers, data = item if isinstance(item, tuple) else ({}, item)
        return httpx.Response(200, json=data, headers=headers)


@pytest.fixture
def fake():
    return FakeKiwoom()


def client_for(fake: FakeKiwoom) -> KiwoomClient:
    return KiwoomClient("APPKEY", "SECRET", mode="mock", max_rps=0, transport=httpx.MockTransport(fake))


def test_uses_mock_domain_and_required_headers(fake):
    fake.responses["ka10001"] = [{"stk_cd": "005930", "cur_prc": "+70100", "return_code": 0}]
    c = client_for(fake)
    assert c.base_url == "https://mockapi.kiwoom.com"
    data = c.request("ka10001", "/api/dostk/stkinfo", {"stk_cd": "005930"}).data
    assert to_int(data["cur_prc"]) == 70100
    token_call = fake.calls[0]
    assert token_call[2] == {"grant_type": "client_credentials", "appkey": "APPKEY", "secretkey": "SECRET"}
    api_id, headers, body = fake.calls[1]
    assert api_id == "ka10001"
    assert headers["authorization"] == "Bearer T1"
    assert headers["content-type"].startswith("application/json")
    assert body == {"stk_cd": "005930"}


def test_business_error_raises_even_on_http_200(fake):
    fake.responses["ka10001"] = [{"return_code": 1902, "return_msg": "종목 정보가 없습니다"}]
    with pytest.raises(KiwoomError) as exc:
        client_for(fake).request("ka10001", "/api/dostk/stkinfo", {"stk_cd": "999999"})
    assert exc.value.code == 1902


def test_invalid_token_is_refreshed_once(fake):
    fake.responses["ka10001"] = [{"return_code": 8005, "return_msg": "Token이 유효하지 않습니다"}, {"cur_prc": "100", "return_code": 0}]
    c = client_for(fake)
    assert c.request("ka10001", "/api/dostk/stkinfo", {"stk_cd": "005930"}).data["cur_prc"] == "100"
    assert fake.token_count == 2
    assert fake.calls[-1][1]["authorization"] == "Bearer T2"


def test_pagination_follows_next_key(fake):
    fake.responses["ka10081"] = [
        ({"cont-yn": "Y", "next-key": "K2"}, {"stk_dt_pole_chart_qry": [{"dt": "20261028"}], "return_code": 0}),
        ({"cont-yn": "N"}, {"stk_dt_pole_chart_qry": [{"dt": "20261027"}], "return_code": 0}),
    ]
    rows = client_for(fake).paginate("ka10081", "/api/dostk/chart", {"stk_cd": "005930"}, "stk_dt_pole_chart_qry")
    assert [r["dt"] for r in rows] == ["20261028", "20261027"]
    second = fake.calls[-1][1]
    assert second["cont-yn"] == "Y" and second["next-key"] == "K2"


def test_broker_parses_balance_and_places_market_order(fake):
    fake.responses["kt00018"] = [
        {
            "prsm_dpst_aset_amt": "000000052480300",
            "acnt_evlt_remn_indv_tot": [
                {"stk_cd": "A005930", "stk_nm": "삼성전자", "rmnd_qty": "000000000000120", "pur_pric": "000000000071200", "cur_prc": "000000074800"},
                {"stk_cd": "A000660", "stk_nm": "SK하이닉스", "rmnd_qty": "000000000000000", "pur_pric": "0", "cur_prc": "0"},
            ],
            "return_code": 0,
        }
    ]
    fake.responses["kt00001"] = [{"entr": "000000012500000", "ord_alow_amt": "000000012300000", "return_code": 0}]
    fake.responses["ka00001"] = [{"acctNo": "8012345611", "return_code": 0}]
    fake.responses["kt10001"] = [{"ord_no": "0000138", "dmst_stex_tp": "KRX", "return_code": 0, "return_msg": "매도주문이 완료되었습니다."}]
    broker = KiwoomBroker(client_for(fake))
    acct = broker.account()
    assert broker.label == "모의투자" and not broker.is_live
    assert acct.equity == 52_480_300 and acct.cash == 12_300_000
    assert [(h.symbol, h.qty, h.avg_price) for h in acct.holdings] == [("005930", 120, 71_200)]
    assert acct.account_label == "키움 ···3456"

    result = broker.place_market_order("005930", "sell", 30)
    assert result.accepted and result.order_no == "0000138"
    api_id, _headers, body = fake.calls[-1]
    assert api_id == "kt10001"
    assert body == {"dmst_stex_tp": "KRX", "stk_cd": "005930", "ord_qty": "30", "ord_uv": "", "trde_tp": "3", "cond_uv": ""}


def test_broker_order_status_from_fills_and_unfilled(fake):
    fake.responses["ka10076"] = [
        {"cntr": [{"ord_no": "0000138", "cntr_qty": "30", "cntr_pric": "176400", "oso_qty": "0", "tdy_trde_cmsn": "790", "tdy_trde_tax": "9525", "ord_stt": "체결"}], "return_code": 0}
    ]
    fake.responses["ka10075"] = [{"oso": [], "return_code": 0}]
    status = KiwoomBroker(client_for(fake)).order_status([("0000138", "035420", 30)])["0000138"]
    assert status.state == "filled" and status.filled_qty == 30 and status.avg_price == 176_400
    assert status.fee == 790 and status.tax == 9525


def test_rejected_order_returns_message(fake):
    fake.responses["kt10000"] = [{"return_code": 20, "return_msg": "주문가능금액을 초과했습니다"}]
    result = KiwoomBroker(client_for(fake)).place_market_order("005930", "buy", 1)
    assert not result.accepted and "초과" in result.message
