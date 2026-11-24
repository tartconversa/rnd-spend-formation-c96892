"""金额与期间纯函数测试。"""
from decimal import Decimal
import unittest

from rdwarehouse.money import D, round_cent, allocate, split_by_shares
from rdwarehouse.chronos import month_of, add_months, months_between
import datetime


class MoneyTests(unittest.TestCase):
    def test_decimal_never_float(self):
        self.assertEqual(D("10.25"), Decimal("10.25"))
        with self.assertRaises(TypeError):
            D(1.5)

    def test_round_half_up(self):
        self.assertEqual(round_cent(Decimal("1.005")), Decimal("1.01"))
        self.assertEqual(round_cent(Decimal("1.004")), Decimal("1.00"))

    def test_allocate_sums_exactly_with_distinct_keys(self):
        # 100 元按 1/3 切三份，各行到分之和必须严格等于 100
        out = allocate(Decimal("100.00"), [
            ("A", Decimal(1) / 3), ("B", Decimal(1) / 3), ("C", Decimal(1) / 3),
        ])
        self.assertEqual(sum(out.values(), Decimal("0")), Decimal("100.00"))
        self.assertEqual(set(out), {"A", "B", "C"})

    def test_allocate_distinct_keys_tail_to_largest_base(self):
        # 2/7+2/7+3/7：取整后 2.86+2.86+4.29=10.01，多出 1 分从基数最大项 Z 扣回
        out = allocate(Decimal("10.00"), [
            ("X", Decimal(2) / 7), ("Y", Decimal(2) / 7), ("Z", Decimal(3) / 7),
        ])
        self.assertEqual(sum(out.values()), Decimal("10.00"))
        self.assertEqual(out["Z"], Decimal("4.28"))
        self.assertEqual(out["X"], Decimal("2.86"))

    def test_allocate_tie_breaks_by_key(self):
        # 三个相同基数 1/3：3.33*3=9.99，欠 0.01；并列按业务键升序，A 拿尾差
        out = allocate(Decimal("10.00"), [
            ("C", Decimal(1) / 3), ("A", Decimal(1) / 3), ("B", Decimal(1) / 3),
        ])
        self.assertEqual(sum(out.values()), Decimal("10.00"))
        self.assertEqual(out["A"], Decimal("3.34"))
        self.assertEqual(out["B"], Decimal("3.33"))
        self.assertEqual(out["C"], Decimal("3.33"))

    def test_split_keeps_uncovered_tail(self):
        # coverage 0.8：残量 0.2 以 None 保留，不归一到 100%
        out = split_by_shares(Decimal("100.00"), [("A", Decimal("0.3")), ("B", Decimal("0.5"))])
        self.assertIn(None, out)
        self.assertEqual(sum(out.values()), Decimal("100.00"))
        self.assertEqual(out[None], Decimal("20.00"))

    def test_split_full_coverage_no_tail(self):
        out = split_by_shares(Decimal("200.00"), [("A", Decimal("0.6")), ("B", Decimal("0.4"))])
        self.assertNotIn(None, out)
        self.assertEqual(out["A"], Decimal("120.00"))
        self.assertEqual(out["B"], Decimal("80.00"))


class ChronosTests(unittest.TestCase):
    def test_month_and_shift(self):
        self.assertEqual(month_of(datetime.date(2026, 10, 31)), "2026-10")
        self.assertEqual(add_months("2026-12", 1), "2027-01")
        self.assertEqual(add_months("2026-01", -1), "2025-12")
        self.assertEqual(months_between("2026-10", "2027-01"), 3)


if __name__ == "__main__":
    unittest.main()
