"""端到端业务规则测试：用合成资料与自建批次覆盖关键口径。

每条规则都对应 docs/资料口径.md 中的明确约定。
"""
from pathlib import Path
import json
import shutil
import tempfile
import unittest

from rdwarehouse.loader import Warehouse
from rdwarehouse.transform import Engine
from rdwarehouse.package import Packager
from rdwarehouse.query import Queries
from rdwarehouse.org import OrgResolver
from rdwarehouse.chronos import add_months
from decimal import Decimal
import datetime

DATA = Path(__file__).resolve().parents[1] / "data"

# 各表契约列序，用于写批次夹具
HEADERS = {
    "orders": "line_id,kind,asset_id,quantity,unit_price,currency,date\n",
    "funding": "fund_id,line_id,project,share\n",
    "receipts": "receipt_id,line_id,kind,quantity,date\n",
    "acceptances": "acceptance_id,receipt_id,accepted_qty,date\n",
    "documents": "document_id,line_id,kind,amount,date\n",
    "assets": "asset_id,ready_at,useful_months\n",
    "usage": "usage_id,asset_id,period,project,hours,coverage_fraction\n",
    "organizations": "relation_id,project,organization,start,end\n",
    "fx": "currency,date,cny_per_unit\n",
    "revisions": "revision_id,line_id,new_total_quantity,date\n",
}


def write_batch(folder, files):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (folder / f"{name}.csv").write_text(HEADERS[name] + body, encoding="utf-8")


def build_warehouse(batches, db_path=None):
    """batches: [(目录文件dict, note)]；返回已构建事实的 Warehouse。"""
    wh = Warehouse(db_path or ":memory:")
    tmp = Path(tempfile.mkdtemp())
    try:
        for i, (files, note) in enumerate(batches, 1):
            folder = tmp / f"b{i}"
            write_batch(folder, files)
            wh.load_batch(folder, note=note)
        Engine(wh).build()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return wh


def state(wh, line_id, period):
    cur = wh.con.execute(
        "SELECT * FROM fact_line_state WHERE _batch=? AND line_id=? AND period=?",
        [wh.latest_batch(), line_id, period],
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return rows[0]


class SamplePackTests(unittest.TestCase):
    """对仓库自带合成资料的全量口径核对（手算值）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.wh = Warehouse(":memory:")
        cls.wh.load_batch(DATA, note="基础批次")
        Engine(cls.wh).build()
        cls.q = Queries(cls.wh)

    def test_revision_conflict_exposed_not_deleted(self):
        # RV1=40 < 已验收 50：承诺归零，已形成 500 元保留，冲突进例外
        s = state(self.wh, "L3", "2026-10")
        self.assertEqual(s["effective_qty"], Decimal("40"))
        self.assertEqual(s["commitment_qty"], Decimal("0"))
        self.assertEqual(s["formed_cny"], Decimal("500.000000"))
        ex = self.q.exceptions("2026-10")
        kinds = {e["ex_type"] for e in ex}
        self.assertIn("revision_below_obligation", kinds)

    def test_three_amount_bases_differ_on_l1(self):
        # 采购价/形成/付款不是同一个数：形成 71500，票 71800，现金 71250
        s = state(self.wh, "L1", "2026-10")
        self.assertEqual(s["formed_cny"], Decimal("71500.000000"))
        self.assertEqual(s["invoice_net_cny"], Decimal("71800.000000"))
        self.assertEqual(s["cash_net_cny"], Decimal("71250.000000"))
        # 汇兑差三分量可区分
        self.assertEqual(s["formed_fx_var"], Decimal("1400.000000"))
        self.assertEqual(s["invoice_fx_var"], Decimal("1700.000000"))
        self.assertEqual(s["cash_fx_var"], Decimal("1150.000000"))

    def test_credit_refund_return_are_independent(self):
        # L3：红冲 100（票）、退款 80（现金）、退货 10 件（物），不互相推断
        s = state(self.wh, "L3", "2026-10")
        self.assertEqual(s["credit_cny"], Decimal("100.000000"))
        self.assertEqual(s["refund_cny"], Decimal("80.000000"))
        self.assertEqual(s["returned_qty"], Decimal("10.000000"))
        link = self.wh.con.execute(
            "SELECT linked_receipt_id, linked_qty, status FROM fact_return_link"
        ).fetchone()
        self.assertEqual(link, ("R2", Decimal("10.000000"), "linked"))

    def test_return_cannot_reduce_accepted(self):
        # R4 退回的是 R2 中尚未验收的 10 件；A2 的 50 件不自动减少
        s = state(self.wh, "L3", "2026-10")
        self.assertEqual(s["formed_qty"], Decimal("50.000000"))
        self.assertEqual(s["pending_qty"], Decimal("0.000000"))

    def test_bridge_identities_balance(self):
        for r in self.q.budget_occupation("2026-10"):
            self.assertEqual(r["bridge_formed_check_cny"], Decimal("0.00"), r["line_id"])
            self.assertEqual(r["bridge_cash_check_cny"], Decimal("0.00"), r["line_id"])

    def test_asset_pre_enablement_collection_and_schedule(self):
        # L2 安装实收 10-14 早于启用 10-16，归启用前；成本 73500，36 月直线
        costs = self.wh.con.execute(
            "SELECT line_id, pre_enablement FROM fact_asset_cost ORDER BY line_id"
        ).fetchall()
        self.assertEqual(costs, [("L1", True), ("L2", True)])
        sch = self.wh.con.execute(
            "SELECT period, cost_basis_cny, monthly_settled FROM fact_asset_schedule "
            "ORDER BY period"
        ).fetchall()
        self.assertEqual(sch[0], ("2026-11", Decimal("73500.000000"), Decimal("2041.67")))

    def test_usage_coverage_tail_not_reallocated(self):
        # 11 月 coverage 0.8：A 30%、B 50%、待分 20%，合计等于月度额
        rows = self.wh.con.execute(
            "SELECT project, share_fraction, amount_cent FROM fact_asset_alloc "
            "WHERE period='2026-11' ORDER BY project NULLS LAST"
        ).fetchall()
        self.assertEqual(rows[0][1], Decimal("0.3000000000"))  # A
        self.assertEqual(rows[1][1], Decimal("0.5000000000"))  # B
        self.assertIsNone(rows[2][0])                          # 待分行
        self.assertEqual(rows[2][2], Decimal("408.33"))
        self.assertEqual(sum(r[2] for r in rows), Decimal("2041.67"))

    def test_full_coverage_no_tail(self):
        rows = self.wh.con.execute(
            "SELECT SUM(amount_cent) FROM fact_asset_alloc "
            "WHERE period='2026-12' AND project IS NOT NULL"
        ).fetchone()[0]
        tail = self.wh.con.execute(
            "SELECT COUNT(*) FROM fact_asset_alloc WHERE period='2026-12' AND project IS NULL"
        ).fetchone()[0]
        self.assertEqual(rows, Decimal("2041.67"))
        self.assertEqual(tail, 0)

    def test_org_temporal_historical_vs_current(self):
        # 10 月（换组织前）A 属 ORG1；11 月起 A 属 ORG2，旧月不被改写
        self.assertEqual(self.q.org.org_for_period("A", "2026-10"), "ORG1")
        self.assertEqual(self.q.org.org_for_period("A", "2026-11"), "ORG2")
        current = {
            (r["project"], r["organization"])
            for r in self.q.project_monthly("2026-10", datetime.date(2026, 12, 1))
            if r["project"] == "A"
        }
        self.assertEqual(current, {("A", "ORG2")})  # 当前视角按查询日
        historical = {
            (r["project"], r["organization"])
            for r in self.q.project_monthly("2026-10")
            if r["project"] == "A"
        }
        self.assertEqual(historical, {("A", "ORG1")})  # 历史视角保留当时组织

    def test_funding_share_not_usage(self):
        # L1 出资 60/40 只解释预算承担；资产分担按 usage 工时，不用出资比例
        nov = self.wh.con.execute(
            "SELECT project, amount_cent FROM fact_asset_alloc "
            "WHERE period='2026-11' AND project IS NOT NULL ORDER BY project"
        ).fetchall()
        self.assertEqual(nov[0][1], Decimal("612.50"))   # A: 30/80*0.8
        self.assertEqual(nov[1][1], Decimal("1020.84"))  # B: 50/80*0.8

    def test_expense_vs_cip_classification(self):
        # consumable/service 验收即费用；equipment/installation 归 cip
        classes = {
            r[0]: r[1] for r in self.wh.con.execute(
                "SELECT DISTINCT line_id, exp_class FROM fact_formed"
            ).fetchall()
        }
        self.assertEqual(classes["L3"], "expense")
        self.assertEqual(classes["L4"], "expense")
        self.assertEqual(classes["L1"], "cip")
        self.assertEqual(classes["L2"], "cip")


class FxTests(unittest.TestCase):
    def test_missing_fx_rate_is_exception_not_interpolated(self):
        files = {
            "orders": "L1,consumable,,10,5,USD,2026-03-01\n",
            "funding": "F1,L1,A,1\n",
            "receipts": "R1,L1,receive,10,2026-03-02\n",
            "acceptances": "A1,R1,10,2026-03-03\n",
            "fx": "USD,2026-03-01,7.00\n",  # 缺 03-03 验收日汇率
            "documents": "",
        }
        wh = build_warehouse([(files, "缺汇率")])
        ex = [e for e in Queries(wh).exceptions() if e["ex_type"] == "fx_missing"]
        self.assertTrue(ex)
        s = state(wh, "L1", "2026-03")
        self.assertIsNone(s["formed_cny"])  # 不插值，人民币待核
        self.assertEqual(s["formed_fc"], Decimal("50.000000"))


class ReturnLinkTests(unittest.TestCase):
    def _base(self, receipts, acceptances=""):
        files = {
            "orders": "L1,consumable,,100,1,CNY,2026-03-01\n",
            "funding": "F1,L1,A,1\n",
            "receipts": receipts,
            "acceptances": acceptances,
            "documents": "",
        }
        return build_warehouse([(files, "退货")])

    def test_unlinked_return_is_pending(self):
        wh = self._base("R9,L1,return,5,2026-03-05\n")  # 无任何在先收货
        row = wh.con.execute(
            "SELECT status, unlinked_qty FROM fact_return_link"
        ).fetchone()
        self.assertEqual(row[0], "unlinked_pending")
        self.assertEqual(row[1], Decimal("5.000000"))

    def test_return_into_accepted_warns_and_keeps_obligation(self):
        wh = self._base(
            "R1,L1,receive,10,2026-03-02\nR2,L1,return,10,2026-03-06\n",
            "A1,R1,10,2026-03-03\n",
        )
        link = wh.con.execute(
            "SELECT status, unlinked_qty FROM fact_return_link"
        ).fetchone()
        self.assertEqual(link[0], "linked_partly_accepted")
        self.assertEqual(link[1], Decimal("10.000000"))  # 全冲已验收，须独立核对
        self.assertEqual(state(wh, "L1", "2026-03")["formed_qty"], Decimal("10.000000"))

    def test_ambiguous_return_not_arbitrarily_picked(self):
        wh = self._base(
            "R1,L1,receive,5,2026-03-02\nR2,L1,receive,5,2026-03-03\n"
            "R3,L1,return,2,2026-03-06\n",
        )
        self.assertEqual(
            wh.con.execute("SELECT status FROM fact_return_link").fetchone()[0],
            "ambiguous_pending",
        )


class BatchTests(unittest.TestCase):
    def test_identical_digest_adds_no_facts(self):
        files = {
            "orders": "L1,consumable,,10,1,CNY,2026-03-01\n",
            "funding": "F1,L1,A,1\n",
        }
        wh = build_warehouse([(files, "b1"), (files, "b2")])
        self.assertEqual(
            wh.con.execute("SELECT COUNT(*) FROM raw_orders").fetchone()[0], 1
        )
        statuses = wh.con.execute(
            "SELECT DISTINCT status FROM loaded_file WHERE table_name='orders'"
        ).fetchall()
        self.assertIn(("duplicate_digest_skipped",), statuses)

    def test_row_revision_takes_latest_but_keeps_history(self):
        b1 = {"orders": "L1,consumable,,100,1,CNY,2026-03-01\n",
              "funding": "F1,L1,A,1\n"}
        b2 = {"orders": "L1,consumable,,80,1,CNY,2026-03-01\n"}  # 同键整行修订
        wh = build_warehouse([(b1, "b1"), (b2, "b2")])
        # raw 保留两版；视野取最新订单量，原始 100 可从 all_rows 审计
        self.assertEqual(wh.con.execute("SELECT COUNT(*) FROM raw_orders").fetchone()[0], 2)
        self.assertEqual(wh.current_rows("orders")[0]["quantity"], Decimal("80"))
        self.assertEqual(len(wh.all_rows("orders")), 2)
        self.assertEqual(
            wh.all_rows("orders")[0]["quantity"], Decimal("100")
        )
        s = state(wh, "L1", "2026-03")
        self.assertEqual(s["order_qty"], Decimal("80"))      # 当前订单视野
        self.assertEqual(s["effective_qty"], Decimal("80"))  # 无 revisions，有效量同视野


class FillInCostTests(unittest.TestCase):
    """后补启用前安装费：补齐月起重算剩余月度额，已付现金不再记支出。"""

    def test_late_installation_recomputes_schedule(self):
        b1 = {
            "orders": (
                "L1,equipment,AS1,1,10000,CNY,2026-09-01\n"
                "L2,installation,AS1,1,2000,CNY,2026-09-05\n"
            ),
            "funding": "F1,L1,A,1\nF2,L2,A,1\n",
            "receipts": (
                "R1,L1,receive,1,2026-09-10\n"
                "R2,L2,receive,1,2026-10-14\n"  # 安装实际发生在启用前
            ),
            "acceptances": (
                "A1,R1,1,2026-09-15\n"          # 设备先验收
                "A2,R2,1,2026-12-20\n"          # 安装 12 月才验收（补齐月）
            ),
            "assets": "AS1,2026-10-16,36\n",
            "documents": (
                "D1,L1,payment,10000,2026-09-12\n"   # 设备现金早已支付
                "D2,L2,payment,2000,2026-09-20\n"    # 安装现金也已支付
            ),
            "usage": (
                "U1,AS1,2026-11,A,10,1\n"
                "U2,AS1,2026-12,A,10,1\n"
            ),
        }
        wh = build_warehouse([(b1, "后补")])
        # 11 月：仅设备 10000，启用次月起按 36 个月直线：10000/36=277.78
        nov = wh.con.execute(
            "SELECT cost_basis_cny, monthly_settled, segment_start "
            "FROM fact_asset_schedule WHERE period='2026-11'"
        ).fetchone()
        self.assertEqual(nov[0], Decimal("10000.000000"))
        self.assertEqual(nov[1], Decimal("277.78"))
        self.assertEqual(nov[2], "2026-11")
        # 12 月：补到 2000（补齐月=验收月 12 月），基数 12000，
        # 从补齐月起重算剩余 35 个月：12000/35=342.86
        dec = wh.con.execute(
            "SELECT cost_basis_cny, monthly_settled, segment_start "
            "FROM fact_asset_schedule WHERE period='2026-12'"
        ).fetchone()
        self.assertEqual(dec[0], Decimal("12000.000000"))
        self.assertEqual(dec[1], Decimal("342.86"))
        self.assertEqual(dec[2], "2026-12")
        # 安装费 12 月才形成支出；9 月的付款只挂预付，不提前记费用
        s2_sep = state(wh, "L2", "2026-09")
        self.assertEqual(s2_sep["formed_cny"], Decimal("0E-6"))
        self.assertEqual(s2_sep["prepaid_cny"], Decimal("2000.000000"))
        s2_dec = state(wh, "L2", "2026-12")
        self.assertEqual(s2_dec["formed_cny"], Decimal("2000.000000"))


class PackageCompareTests(unittest.TestCase):
    def test_confirmed_package_immutable_and_comparable(self):
        b1 = {
            "orders": "L1,service,,10,100,CNY,2026-10-01\n",
            "funding": "F1,L1,A,1\n",
            "receipts": "R1,L1,receive,5,2026-10-10\n",
            "acceptances": "A1,R1,5,2026-10-12\n",
            "documents": "",
        }
        b2 = {
            "receipts": "R2,L1,receive,3,2026-11-05\n",
            "acceptances": "A2,R2,3,2026-11-08\n",
        }
        tmp = Path(tempfile.mkdtemp())
        wh = Warehouse(":memory:")
        f1, f2 = tmp / "b1", tmp / "b2"
        write_batch(f1, b1)
        write_batch(f2, b2)
        wh.load_batch(f1, note="b1")
        Engine(wh).build()
        pkg = Packager(wh)
        pkg.build("2026-10")
        pkg.confirm("2026-10")
        before = wh.con.execute(
            "SELECT amount_cent FROM pkg_measure WHERE measure='expense_period'"
        ).fetchone()[0]
        self.assertEqual(before, Decimal("500.00"))

        # 第二批补资料
        wh.load_batch(f2, note="b2")
        Engine(wh).build()
        # 已确认包内容不变
        after = wh.con.execute(
            "SELECT amount_cent FROM pkg_measure WHERE period='2026-10' AND _batch=1 "
            "AND measure='expense_period'"
        ).fetchone()[0]
        self.assertEqual(before, after)
        # 新批次 10 月形成量仍为 500（新验收在 11 月），11 月为 800
        self.assertEqual(state(wh, "L1", "2026-11")["formed_cny"], Decimal("800.000000"))
        pkg2 = Packager(wh)
        pkg2.build("2026-11")
        # 批次1的 10 月包已确认，对同一 (period,batch) 的重建必须拒绝
        with self.assertRaises(ValueError):
            pkg2.build("2026-10", batch=1)
        # 批次2尚未建过 10 月包，可以建（新版本，不动批次1的冻结内容）
        pkg2.build("2026-10", batch=2)
        frozen = wh.con.execute(
            "SELECT amount_cent FROM pkg_measure WHERE period='2026-10' AND _batch=1 "
            "AND measure='expense_period'"
        ).fetchone()[0]
        self.assertEqual(frozen, before)

        # 11 月包两批次比较：批次1无 11 月包（空基线），批次2新增费用 300
        Packager(wh).build("2026-11", batch=1)
        diff = Packager(wh).compare("2026-11", 1, 2)
        added = {(d["measure"], d["project"], d["delta_cent"]) for d in diff["measure_diff"]}
        self.assertIn(("expense_period", "A", Decimal("300.00")), added)


if __name__ == "__main__":
    unittest.main()
