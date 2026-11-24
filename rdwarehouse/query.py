"""集市查询：三种管理视角 + 行级下钻 + 预算占用反查 + 组织视角。

三视角对应三种口径，绝不混用金额列：
1. budget     预算使用视角：承诺(未履约) + 待核义务(已实收未验)
2. expense    当期费用视角：验收形成的 consumable/service + 启用后资产分担
3. cip        在建资产视角：equipment/installation 归集余额与启用进度
"""
from decimal import Decimal

from .money import D
from .org import OrgResolver


def _dicts(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


class Queries:
    def __init__(self, warehouse, batch=None):
        self.wh = warehouse
        self.con = warehouse.con
        self.batch = warehouse.latest_batch() if batch is None else batch
        self.org = OrgResolver(warehouse.current_rows("organizations", self.batch))

    # ---------- 组织标注 ----------
    def _annotate_org(self, rows, date_field=None, current_date=None, project_field="project"):
        for r in rows:
            project = r.get(project_field)
            if current_date is not None:
                r["organization"] = self.org.org_at(project, current_date) if project else None
                r["org_basis"] = "current"
            else:
                period = r.get("period")
                r["organization"] = (
                    self.org.org_for_period(project, period) if project and period else None
                )
                r["org_basis"] = "as_of_period"
        return rows

    # ---------- 视角一：预算使用（承诺 + 待核，含票款三差异） ----------
    def budget_view(self, period, project=None, current_date=None):
        args = [self.batch, period]
        where = "m._batch=? AND m.period=? AND m.measure IN " \
                "('budget_commitment','pending_obligation','unsettled','prepaid')"
        if project:
            where += " AND m.project=?"
            args.append(project)
        rows = _dicts(self.con.execute(
            f"""
            SELECT m.period, m.project, m.measure, m.amount_cent, m.basis, m.line_id
            FROM fact_project_measure m WHERE {where}
            ORDER BY m.project, m.measure, m.line_id
            """,
            args,
        ))
        return self._annotate_org(rows, current_date=current_date)

    # ---------- 视角二：当期费用 ----------
    def expense_view(self, period, project=None, current_date=None):
        args = [self.batch, period]
        where = "m._batch=? AND m.period=? AND m.measure IN ('expense_period','asset_share')"
        if project:
            where += " AND m.project=?"
            args.append(project)
        rows = _dicts(self.con.execute(
            f"""
            SELECT m.period, m.project, m.measure, m.amount_cent, m.basis,
                   m.line_id, m.acceptance_id, m.asset_id
            FROM fact_project_measure m WHERE {where}
            ORDER BY m.project, m.measure
            """,
            args,
        ))
        return self._annotate_org(rows, current_date=current_date)

    # ---------- 视角三：在建资产 ----------
    def cip_view(self, period, asset_id=None):
        """截至期末的在建归集余额、已启用资产的累计分担与待分残量。"""
        args = [self.batch, period]
        where = "c._batch=? AND c.fill_period<=?"
        if asset_id:
            where += " AND c.asset_id=?"
            args.append(asset_id)
        cip = _dicts(self.con.execute(
            f"""
            SELECT c.asset_id, c.line_id, c.acceptance_id, c.fill_period,
                   c.pre_enablement, c.amount_cny
            FROM fact_asset_cost c WHERE {where}
            ORDER BY c.asset_id, c.fill_period
            """,
            args,
        ))
        assets = {}
        for c in cip:
            a = assets.setdefault(c["asset_id"], {
                "asset_id": c["asset_id"], "cost_cum_cny": Decimal("0"),
                "pre_enablement_cny": Decimal("0"), "fill_later_cny": Decimal("0"),
                "lines": set(),
            })
            a["cost_cum_cny"] += D(c["amount_cny"])
            a["lines"].add(c["line_id"])
            if c["pre_enablement"]:
                a["pre_enablement_cny"] += D(c["amount_cny"])
            else:
                a["fill_later_cny"] += D(c["amount_cny"])

        # 截至期末的分担进度与待分
        for a in assets.values():
            prog = self.con.execute(
                """
                SELECT COALESCE(SUM(CASE WHEN project IS NOT NULL THEN amount_cent ELSE 0 END),0),
                       COALESCE(SUM(CASE WHEN project IS NULL THEN amount_cent ELSE 0 END),0),
                       COUNT(DISTINCT period)
                FROM fact_asset_alloc
                WHERE _batch=? AND asset_id=? AND period<=?
                """,
                [self.batch, a["asset_id"], period],
            ).fetchone()
            a["allocated_cum_cent"] = D(prog[0])
            a["unallocated_cum_cent"] = D(prog[1])
            a["share_months"] = prog[2]
            a["lines"] = sorted(a["lines"])
            for k in ("cost_cum_cny", "pre_enablement_cny", "fill_later_cny"):
                a[k] = a[k].quantize(Decimal("0.01"))
        return sorted(assets.values(), key=lambda x: x["asset_id"])

    # ---------- 采购行下钻：承诺 -> 收货/验收/票/款/资产 ----------
    def line_drilldown(self, line_id, period=None):
        p = period or self.wh.latest_period(self.batch)
        state = _dicts(self.con.execute(
            "SELECT * FROM fact_line_state WHERE _batch=? AND line_id=? AND period=?",
            [self.batch, line_id, p],
        ))
        formed = _dicts(self.con.execute(
            "SELECT * FROM fact_formed WHERE _batch=? AND line_id=? "
            "ORDER BY acceptance_date",
            [self.batch, line_id],
        ))
        docs = _dicts(self.con.execute(
            "SELECT * FROM fact_document WHERE _batch=? AND line_id=? ORDER BY doc_date",
            [self.batch, line_id],
        ))
        returns = _dicts(self.con.execute(
            "SELECT * FROM fact_return_link WHERE _batch=? AND line_id=?",
            [self.batch, line_id],
        ))
        costs = _dicts(self.con.execute(
            "SELECT * FROM fact_asset_cost WHERE _batch=? AND line_id=?",
            [self.batch, line_id],
        ))
        return {
            "period": p,
            "state": state,
            "acceptances": formed,
            "documents": docs,
            "return_links": returns,
            "asset_costs": costs,
        }

    # ---------- 预算占用反查：三差异为何不同 ----------
    def budget_occupation(self, period, project=None):
        """每项目/每行同时给出未履约、已形成未结算、现金已付三口径及差额桥。"""
        args = [self.batch, period]
        where = "s._batch=? AND s.period=?"
        if project:
            # funding 过滤
            where += " AND s.line_id IN (SELECT line_id FROM raw_funding WHERE project=?)"
            args.append(project)
        lines = _dicts(self.con.execute(
            f"""
            SELECT s.line_id, s.kind, s.currency,
                   s.commitment_qty, s.commitment_cny,
                   s.pending_qty, s.pending_cny,
                   s.formed_fc, s.formed_cny,
                   s.invoice_net_fc, s.invoice_net_cny,
                   s.cash_net_fc, s.cash_net_cny,
                   s.unsettled_fc, s.unsettled_cny,
                   s.prepaid_fc, s.prepaid_cny,
                   s.formed_fx_var, s.invoice_fx_var, s.cash_fx_var
            FROM fact_line_state s WHERE {where}
            ORDER BY s.line_id
            """,
            args,
        ))
        # 桥接恒等式（人民币口径）：承诺+待核+形成 = 有效订单价；
        # 形成 = 已结算净额(票) + 已形成未结算；现金 = 票净额 + 预付
        for r in lines:
            r["bridge_order_cny"] = (
                (D(r["commitment_cny"] or 0) + D(r["pending_cny"] or 0)
                 + D(r["formed_cny"] or 0)).quantize(Decimal("0.01"))
            )
            r["bridge_formed_check_cny"] = (
                D(r["invoice_net_cny"] or 0) + D(r["unsettled_cny"] or 0)
                - D(r["formed_cny"] or 0)
            ).quantize(Decimal("0.01"))
            r["bridge_cash_check_cny"] = (
                D(r["cash_net_cny"] or 0) - D(r["invoice_net_cny"] or 0)
                - D(r["prepaid_cny"] or 0)
            ).quantize(Decimal("0.01"))
        return lines

    # ---------- 项目月度总表（当前/历史组织视角） ----------
    def project_monthly(self, period, current_date=None):
        rows = _dicts(self.con.execute(
            """
            SELECT period, project, measure, SUM(amount_cent) AS amount_cent
            FROM fact_project_measure
            WHERE _batch=? AND period=?
            GROUP BY period, project, measure
            ORDER BY project, measure
            """,
            [self.batch, period],
        ))
        return self._annotate_org(rows, current_date=current_date)

    # ---------- 例外清单 ----------
    def exceptions(self, period=None, severity=None):
        sql = "SELECT * FROM fact_exception WHERE _batch=?"
        args = [self.batch]
        if period:
            sql += " AND period=?"
            args.append(period)
        if severity:
            sql += " AND severity=?"
            args.append(severity)
        return _dicts(self.con.execute(sql + " ORDER BY period, severity, ex_type", args))
