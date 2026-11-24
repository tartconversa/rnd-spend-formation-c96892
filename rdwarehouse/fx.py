"""汇率查找：只认业务当天汇率，缺日不插值，CNY 恒为 1。"""
from decimal import Decimal
from .money import D


class FxTable:
    def __init__(self, fx_rows):
        # fx_rows: raw_fx 当前视野行（字典，含 currency/date/cny_per_unit）
        self._rates = {
            (r["currency"], r["date"]): D(r["cny_per_unit"]) for r in fx_rows
        }

    def rate(self, currency: str, date):
        """返回当天 CNY/单位汇率；无当天记录返回 None（调用方列待核）。"""
        if currency == "CNY":
            return Decimal("1")
        return self._rates.get((currency, date))

    def to_cny(self, amount: Decimal, currency: str, date):
        """原币金额 -> (人民币金额或 None, 汇率或 None)。"""
        rate = self.rate(currency, date)
        if rate is None:
            return None, None
        return amount * rate, rate
