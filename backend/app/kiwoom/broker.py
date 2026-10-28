from __future__ import annotations

import logging
import threading
import time

from app.kiwoom.client import KiwoomClient, KiwoomError, to_float, to_int
from app.trading.broker import BrokerAccount, BrokerHolding, FillStatus, OrderResult

log = logging.getLogger(__name__)

ACCOUNT_PATH = "/api/dostk/acnt"
ORDER_PATH = "/api/dostk/ordr"


class KiwoomBroker:
    """키움 REST 주문·계좌.

    - 매수 kt10000 / 매도 kt10001 / 취소 kt10003 (시장가 trde_tp=3)
    - 계좌평가잔고 kt00018, 예수금 kt00001, 계좌번호 ka00001
    - 체결 ka10076, 미체결 ka10075
    """

    kind = "kiwoom"

    def __init__(self, client: KiwoomClient, exchange: str = "KRX", account_ttl: float = 10.0) -> None:
        self.client = client
        self.exchange = exchange
        self.is_live = client.mode == "real"
        self.label = "실전투자" if self.is_live else "모의투자"
        self.account_ttl = account_ttl
        self._lock = threading.Lock()
        self._account: tuple[float, BrokerAccount] | None = None
        self._account_no: str | None = None

    def _account_label(self) -> str:
        if self._account_no is None:
            try:
                data = self.client.request("ka00001", ACCOUNT_PATH, {}).data
                self._account_no = str(data.get("acctNo") or "")
            except KiwoomError as exc:
                log.warning("계좌번호 조회 실패: %s", exc)
                self._account_no = ""
        no = self._account_no
        # 10자리 중 마지막 2자리는 상품 구분이라 그 앞 4자리를 보여 줍니다.
        tail = no[-6:-2] if len(no) >= 6 else no
        return f"키움 ···{tail}" if tail else "키움 계좌"

    def invalidate(self) -> None:
        with self._lock:
            self._account = None

    def account(self) -> BrokerAccount:
        now = time.monotonic()
        with self._lock:
            if self._account and now - self._account[0] < self.account_ttl:
                return self._account[1]
        rows: list[dict] = []
        cont_yn = next_key = None
        totals: dict = {}
        for _ in range(5):
            resp = self.client.request(
                "kt00018", ACCOUNT_PATH, {"qry_tp": "1", "dmst_stex_tp": "KRX"}, cont_yn=cont_yn, next_key=next_key
            )
            totals = totals or resp.data
            rows.extend(resp.data.get("acnt_evlt_remn_indv_tot") or [])
            if resp.cont_yn != "Y" or not resp.next_key:
                break
            cont_yn, next_key = "Y", resp.next_key
        holdings = []
        for r in rows:
            qty = to_int(r.get("rmnd_qty"))
            if qty <= 0:
                continue
            code = str(r.get("stk_cd") or "").strip()
            # 종목번호는 접두어 1자리(A: 주식) + 6자리입니다.
            if len(code) == 7 and code[0].isalpha():
                code = code[1:]
            holdings.append(
                BrokerHolding(
                    symbol=code,
                    name=str(r.get("stk_nm") or code).strip(),
                    qty=qty,
                    avg_price=float(abs(to_int(r.get("pur_pric")))),
                    price=float(abs(to_int(r.get("cur_prc")))),
                )
            )
        deposit = self.client.request("kt00001", ACCOUNT_PATH, {"qry_tp": "3"}).data
        cash = float(to_int(deposit.get("ord_alow_amt")) or to_int(deposit.get("entr")))
        equity = float(to_int(totals.get("prsm_dpst_aset_amt")))
        if equity <= 0:
            equity = float(to_int(deposit.get("entr"))) + sum(h.qty * h.price for h in holdings)
        acct = BrokerAccount(self._account_label(), cash, equity, holdings)
        with self._lock:
            self._account = (now, acct)
        return acct

    def place_market_order(self, symbol: str, side: str, qty: int) -> OrderResult:
        api_id = "kt10000" if side == "buy" else "kt10001"
        body = {
            "dmst_stex_tp": self.exchange,
            "stk_cd": symbol,
            "ord_qty": str(int(qty)),
            "ord_uv": "",
            "trde_tp": "3",  # 시장가
            "cond_uv": "",
        }
        try:
            data = self.client.request(api_id, ORDER_PATH, body).data
        except KiwoomError as exc:
            return OrderResult(False, message=exc.message)
        finally:
            self.invalidate()
        order_no = str(data.get("ord_no") or "").strip()
        if not order_no:
            return OrderResult(False, message=data.get("return_msg") or "주문번호를 받지 못했습니다")
        return OrderResult(True, order_no, data.get("return_msg") or "주문이 접수되었습니다")

    def order_status(self, orders: list[tuple[str, str, int]]) -> dict[str, FillStatus]:
        if not orders:
            return {}
        wanted = {no.lstrip("0") or "0": (no, qty) for no, _sym, qty in orders}
        fills = self.client.paginate(
            "ka10076",
            ACCOUNT_PATH,
            {"stk_cd": "", "qry_tp": "0", "sell_tp": "0", "ord_no": "", "stex_tp": "0"},
            "cntr",
            max_pages=5,
        )
        unfilled = self.client.paginate(
            "ka10075",
            ACCOUNT_PATH,
            {"all_stk_tp": "0", "trde_tp": "0", "stk_cd": "", "stex_tp": "0"},
            "oso",
            max_pages=5,
        )
        remaining = {str(r.get("ord_no", "")).lstrip("0"): to_int(r.get("oso_qty")) for r in unfilled}
        out: dict[str, FillStatus] = {}
        by_no: dict[str, list[dict]] = {}
        for r in fills:
            by_no.setdefault(str(r.get("ord_no", "")).lstrip("0"), []).append(r)
        for key, (order_no, qty) in wanted.items():
            rows = by_no.get(key, [])
            filled = max((to_int(r.get("cntr_qty")) for r in rows), default=0)
            price = None
            if rows and filled:
                best = max(rows, key=lambda r: to_int(r.get("cntr_qty")))
                price = float(abs(to_int(best.get("cntr_pric")))) or None
            fee = sum(to_float(r.get("tdy_trde_cmsn")) for r in rows)
            tax = sum(to_float(r.get("tdy_trde_tax")) for r in rows)
            status_text = " ".join(str(r.get("ord_stt") or "") for r in rows)
            left = remaining.get(key)
            if "거부" in status_text:
                state = "rejected"
            elif left is not None and left > 0:
                state = "partial" if filled else "submitted"
            elif filled >= qty:
                state = "filled"
            elif rows and ("취소" in status_text or filled):
                state = "cancelled"  # 일부만 체결되고 나머지가 취소됨
            else:
                state = "submitted"
            out[order_no] = FillStatus(order_no, filled, price, qty - filled, fee, tax, state)
        self.invalidate()
        return out

    def cancel(self, order_no: str, symbol: str) -> OrderResult:
        body = {"dmst_stex_tp": self.exchange, "orig_ord_no": order_no, "stk_cd": symbol, "cncl_qty": "0"}
        try:
            data = self.client.request("kt10003", ORDER_PATH, body).data
        except KiwoomError as exc:
            return OrderResult(False, message=exc.message)
        return OrderResult(True, str(data.get("ord_no") or ""), data.get("return_msg") or "취소 주문을 접수했습니다")
