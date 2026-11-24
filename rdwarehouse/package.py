"""月度管理包：冻结副本、确认、补资料后的版本比较。

- 同一 (period, batch) 重复构建只允许覆盖草稿；已确认包不可变
- 包内保存确认当时的事实快照，后续批次补到资料不会改原内容，
  只能产生新批次包并通过 compare_packages 比较变化
"""
import datetime

from .money import D

DRAFT, CONFIRMED = "draft", "confirmed"


class Packager:
    def __init__(self, warehouse):
        self.wh = warehouse
        self.con = warehouse.con

    def build(self, period, batch=None):
        batch = self.wh.latest_batch() if batch is None else batch
        status = self.con.execute(
            "SELECT status FROM pkg_header WHERE period=? AND _batch=?",
            [period, batch],
        ).fetchone()
        if status and status[0] == CONFIRMED:
            raise ValueError(f"管理包 {period} 批次 {batch} 已确认，不可改写")
        self.con.execute("BEGIN")
        try:
            for table in ("pkg_measure", "pkg_line", "pkg_asset", "pkg_exception"):
                self.con.execute(
                    f"DELETE FROM {table} WHERE period=? AND _batch=?", [period, batch],
                )
            self.con.execute(
                "DELETE FROM pkg_header WHERE period=? AND _batch=?", [period, batch],
            )
            self.con.execute(
                "INSERT INTO pkg_header VALUES (?,?,?,?,?)",
                [period, batch, datetime.datetime.now().replace(microsecond=0), DRAFT, None],
            )
            self.con.execute(
                """
                INSERT INTO pkg_measure
                SELECT ?, _batch, project, measure, amount_cny, amount_cent, basis,
                       line_id, acceptance_id, document_id, asset_id
                FROM fact_project_measure WHERE _batch=? AND period=?
                """,
                [period, batch, period],
            )
            self.con.execute(
                """
                INSERT INTO pkg_line
                SELECT ?, _batch, line_id, kind, asset_id, currency,
                       commitment_qty, commitment_cny, pending_qty, pending_cny,
                       formed_fc, formed_cny, formed_fx_var,
                       invoice_net_fc, invoice_net_cny, cash_net_fc, cash_net_cny,
                       unsettled_cny, prepaid_cny
                FROM fact_line_state WHERE _batch=? AND period=?
                """,
                [period, batch, period],
            )
            self.con.execute(
                """
                INSERT INTO pkg_asset
                SELECT ?, s._batch, s.asset_id, s.cost_basis_cny, s.monthly_settled,
                       COALESCE(SUM(a.amount_cent),0),
                       s.monthly_settled - COALESCE(SUM(a.amount_cent),0)
                FROM fact_asset_schedule s
                LEFT JOIN fact_asset_alloc a
                  ON a._batch=s._batch AND a.asset_id=s.asset_id
                 AND a.period=s.period AND a.project IS NOT NULL
                WHERE s._batch=? AND s.period=?
                GROUP BY s._batch, s.asset_id, s.cost_basis_cny, s.monthly_settled
                """,
                [period, batch, period],
            )
            self.con.execute(
                """
                INSERT INTO pkg_exception
                SELECT ?, _batch, ex_type, severity, line_id, document_id, asset_id,
                       ref_id, amount, currency, message
                FROM fact_exception WHERE _batch=? AND period=?
                """,
                [period, batch, period],
            )
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise
        return {"period": period, "batch": batch, "status": DRAFT}

    def confirm(self, period, batch=None):
        batch = self.wh.latest_batch() if batch is None else batch
        exists = self.con.execute(
            "SELECT status FROM pkg_header WHERE period=? AND _batch=?",
            [period, batch],
        ).fetchone()
        if not exists:
            raise ValueError("管理包不存在，请先 build")
        if exists[0] == CONFIRMED:
            return {"period": period, "batch": batch, "status": CONFIRMED, "changed": False}
        self.con.execute(
            "UPDATE pkg_header SET status=?, confirmed_at=? WHERE period=? AND _batch=?",
            [CONFIRMED, datetime.datetime.now().replace(microsecond=0), period, batch],
        )
        return {"period": period, "batch": batch, "status": CONFIRMED, "changed": True}

    def compare(self, period, base_batch, new_batch):
        """比较同一月份两个批次包：度量/行/资产/例外的变化。"""
        def rows(sql, args):
            cur = self.con.execute(sql, args)
            return [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]

        measure_diff = rows(
            """
            WITH a AS (
                SELECT project, measure, line_id, acceptance_id, document_id, asset_id,
                       amount_cent AS base_cent
                FROM pkg_measure WHERE period=? AND _batch=?
            ), b AS (
                SELECT project, measure, line_id, acceptance_id, document_id, asset_id,
                       amount_cent AS new_cent
                FROM pkg_measure WHERE period=? AND _batch=?
            )
            SELECT COALESCE(a.project,b.project) project,
                   COALESCE(a.measure,b.measure) measure,
                   COALESCE(a.line_id,b.line_id) line_id,
                   COALESCE(a.acceptance_id,b.acceptance_id) acceptance_id,
                   COALESCE(a.document_id,b.document_id) document_id,
                   COALESCE(a.asset_id,b.asset_id) asset_id,
                   COALESCE(a.base_cent,0) base_cent,
                   COALESCE(b.new_cent,0) new_cent,
                   COALESCE(b.new_cent,0)-COALESCE(a.base_cent,0) delta_cent
            FROM a FULL OUTER JOIN b
              ON a.project IS NOT DISTINCT FROM b.project
             AND a.measure=b.measure
             AND a.line_id IS NOT DISTINCT FROM b.line_id
             AND a.acceptance_id IS NOT DISTINCT FROM b.acceptance_id
             AND a.document_id IS NOT DISTINCT FROM b.document_id
             AND a.asset_id IS NOT DISTINCT FROM b.asset_id
            WHERE COALESCE(b.new_cent,0) <> COALESCE(a.base_cent,0)
            ORDER BY project, measure
            """,
            [period, base_batch, period, new_batch],
        )
        line_diff = rows(
            """
            WITH a AS (SELECT * FROM pkg_line WHERE period=? AND _batch=?),
                 b AS (SELECT * FROM pkg_line WHERE period=? AND _batch=?)
            SELECT COALESCE(a.line_id,b.line_id) line_id,
                   COALESCE(b.commitment_cny,0)-COALESCE(a.commitment_cny,0) d_commitment,
                   COALESCE(b.pending_cny,0)-COALESCE(a.pending_cny,0) d_pending,
                   COALESCE(b.formed_cny,0)-COALESCE(a.formed_cny,0) d_formed,
                   COALESCE(b.invoice_net_cny,0)-COALESCE(a.invoice_net_cny,0) d_invoice,
                   COALESCE(b.cash_net_cny,0)-COALESCE(a.cash_net_cny,0) d_cash
            FROM a FULL OUTER JOIN b USING (line_id)
            """,
            [period, base_batch, period, new_batch],
        )
        ex_base = rows(
            "SELECT ex_type, line_id, ref_id, message FROM pkg_exception "
            "WHERE period=? AND _batch=?",
            [period, base_batch],
        )
        ex_new = rows(
            "SELECT ex_type, line_id, ref_id, message FROM pkg_exception "
            "WHERE period=? AND _batch=?",
            [period, new_batch],
        )
        key = lambda e: (e["ex_type"], e["line_id"], e["ref_id"])
        bset, aset = {key(e) for e in ex_new}, {key(e) for e in ex_base}
        return {
            "period": period,
            "base_batch": base_batch,
            "new_batch": new_batch,
            "measure_diff": measure_diff,
            "line_diff": [d for d in line_diff if any(
                D(d[k]) != 0 for k in
                ("d_commitment", "d_pending", "d_formed", "d_invoice", "d_cash")
            )],
            "exceptions_added": [e for e in ex_new if key(e) not in aset],
            "exceptions_cleared": [e for e in ex_base if key(e) not in bset],
        }
