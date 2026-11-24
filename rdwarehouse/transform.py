"""事实重算引擎：每个批次视野内从 raw_* 重算全部事实。

设计原则：
- 事实表整批可重建（幂等），raw 永不改写
- 承诺、待核义务、验收形成、票据、现金各走各的事件链，不用一个金额列兼任
- 缺汇率、修订冲突、退货无关联、usage 覆盖不足全部进 fact_exception，不静默修正
"""
import datetime
from decimal import Decimal

from .chronos import month_of, add_months, months_between, month_range
from .fx import FxTable
from .money import D, ZERO, round_cent, allocate, split_by_shares
from .schema import FACT_TABLES

EXPENSE_KINDS = {"consumable", "service"}
ASSET_KINDS = {"equipment", "installation"}


def _periods_between(first, last):
    return month_range(first, months_between(first, last) + 1)


class Engine:
    def __init__(self, warehouse, batch=None):
        self.wh = warehouse
        self.con = warehouse.con
        self.batch = warehouse.latest_batch() if batch is None else batch
        self.exceptions = []
        # 状态型例外只在首次出现的月份落一条；周期型事件（如缺使用记录）逐月落
        self._seen_state_exc = set()

    # ---------- 工具 ----------
    def _insert_many(self, sql, rows):
        if rows:
            self.con.executemany(sql, rows)

    def _exc(self, period, ex_type, severity, message, *, recurring=False, **refs):
        """recurring=True 表示当月事件（逐月独立）；否则为状态型例外，去重到首现月。"""
        dedup_key = None
        if not recurring:
            dedup_key = (
                ex_type, refs.get("line_id"), refs.get("document_id"),
                refs.get("asset_id"), refs.get("ref_id"),
            )
            if dedup_key in self._seen_state_exc:
                return
            self._seen_state_exc.add(dedup_key)
        row = {
            "period": period, "ex_type": ex_type, "severity": severity,
            "line_id": refs.get("line_id"), "document_id": refs.get("document_id"),
            "asset_id": refs.get("asset_id"), "ref_id": refs.get("ref_id"),
            "amount": refs.get("amount"), "currency": refs.get("currency"),
            "message": message,
        }
        self.exceptions.append(row)

    def _cny(self, amount, currency, date, line_id, ref_id):
        cny, rate = self.fx.to_cny(amount, currency, date)
        if rate is None:
            self._exc(
                month_of(date), "fx_missing", "error",
                f"{date} 无 {currency} 当天汇率，不插值，人民币金额待核",
                line_id=line_id, ref_id=ref_id, amount=amount, currency=currency,
            )
        return cny, rate

    # ---------- 主流程 ----------
    def build(self):
        b = self.batch
        self.con.execute("BEGIN")
        try:
            for table in FACT_TABLES:
                self.con.execute(f"DELETE FROM {table} WHERE _batch = ?", [b])
            self._load_inputs()
            self._build_formed_and_documents()
            self._build_return_links()
            self._build_line_states()
            self._build_assets()
            self._build_project_measures()
            self._flush_exceptions()
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise
        return {"batch": b, "exceptions": len(self.exceptions)}

    def _load_inputs(self):
        self.orders = {r["line_id"]: r for r in self.wh.current_rows("orders", self.batch)}
        self.funding = self.wh.current_rows("funding", self.batch)
        self.receipts = self.wh.current_rows("receipts", self.batch)
        self.acceptances = self.wh.current_rows("acceptances", self.batch)
        self.documents = self.wh.current_rows("documents", self.batch)
        self.assets = {r["asset_id"]: r for r in self.wh.current_rows("assets", self.batch)}
        self.usage = self.wh.current_rows("usage", self.batch)
        self.orgs = self.wh.current_rows("organizations", self.batch)
        self.fx = FxTable(self.wh.current_rows("fx", self.batch))
        self.revisions = self.wh.current_rows("revisions", self.batch)

        event_periods = [month_of(o["date"]) for o in self.orders.values()]
        for rows, key in (
            (self.receipts, "date"), (self.acceptances, "date"),
            (self.documents, "date"), (self.revisions, "date"),
        ):
            event_periods += [month_of(r[key]) for r in rows]
        event_periods += [u["period"] for u in self.usage]
        self.horizon_first = min(event_periods)
        self.horizon_last = max(event_periods)
        self.periods = _periods_between(self.horizon_first, self.horizon_last)

        # 索引
        self.rec_by_line = {}
        for r in self.receipts:
            self.rec_by_line.setdefault(r["line_id"], []).append(r)
        self.acc_by_receipt = {}
        for a in self.acceptances:
            self.acc_by_receipt.setdefault(a["receipt_id"], []).append(a)
        self.doc_by_line = {}
        for d in self.documents:
            self.doc_by_line.setdefault(d["line_id"], []).append(d)
        self.rev_by_line = {}
        for rv in self.revisions:
            self.rev_by_line.setdefault(rv["line_id"], []).append(rv)
        self.fund_by_line = {}
        for f in self.funding:
            self.fund_by_line.setdefault(f["line_id"], []).append(f)
        self.usage_by_asset_period = {}
        for u in self.usage:
            self.usage_by_asset_period[(u["asset_id"], u["period"])] = u

    # ---------- 验收形成 / 票款事实 ----------
    def _build_formed_and_documents(self):
        formed_rows, doc_rows = [], []
        self.formed_by_line = {lid: [] for lid in self.orders}
        for acc in sorted(self.acceptances, key=lambda a: (a["date"], a["acceptance_id"])):
            receipt = next((r for r in self.receipts if r["receipt_id"] == acc["receipt_id"]), None)
            if receipt is None:
                self._exc(
                    month_of(acc["date"]), "acceptance_orphan", "error",
                    f"验收 {acc['acceptance_id']} 找不到收货 {acc['receipt_id']}",
                    ref_id=acc["acceptance_id"],
                )
                continue
            line = self.orders.get(receipt["line_id"])
            if line is None:
                self._exc(
                    month_of(acc["date"]), "acceptance_orphan", "error",
                    f"验收 {acc['acceptance_id']} 的收货行 {receipt['line_id']} 无订单",
                    line_id=receipt["line_id"], ref_id=acc["acceptance_id"],
                )
                continue
            qty, price = D(acc["accepted_qty"]), D(line["unit_price"])
            amount_fc = qty * price
            cny, rate = self._cny(
                amount_fc, line["currency"], acc["date"],
                line["line_id"], acc["acceptance_id"],
            )
            exp_class = "expense" if line["kind"] in EXPENSE_KINDS else "cip"
            row = (
                self.batch, acc["acceptance_id"], line["line_id"], receipt["receipt_id"],
                line["asset_id"], exp_class, acc["date"], month_of(acc["date"]),
                qty, price, rate, amount_fc, cny, rate is None,
            )
            formed_rows.append(row)
            self.formed_by_line[line["line_id"]].append(
                {**dict(zip(
                    ["_batch", "acceptance_id", "line_id", "receipt_id", "asset_id",
                     "exp_class", "acceptance_date", "period", "quantity",
                     "unit_price", "fx_rate", "amount_fc", "amount_cny", "fx_missing"],
                    row,
                ))}
            )
        self._insert_many(
            "INSERT INTO fact_formed VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", formed_rows,
        )

        for d in sorted(self.documents, key=lambda d: (d["date"], d["document_id"])):
            line = self.orders.get(d["line_id"])
            currency = line["currency"] if line else "CNY"
            amount = D(d["amount"])
            cny, rate = (amount, Decimal("1")) if currency == "CNY" else self._cny(
                amount, currency, d["date"], d["line_id"], d["document_id"],
            )
            doc_rows.append((
                self.batch, d["document_id"], d["line_id"], d["kind"], d["date"],
                month_of(d["date"]), amount, rate, cny, rate is None,
            ))
        self._insert_many(
            "INSERT INTO fact_document VALUES (?,?,?,?,?,?,?,?,?,?)", doc_rows,
        )

    # ---------- 退货关联 ----------
    def _build_return_links(self):
        rows = []
        for ret in sorted((r for r in self.receipts if r["kind"] == "return"),
                          key=lambda r: (r["date"], r["receipt_id"])):
            lid = ret["line_id"]
            period = month_of(ret["date"])
            candidates = [
                r for r in self.rec_by_line.get(lid, [])
                if r["kind"] == "receive" and r["date"] <= ret["date"]
            ]
            qty = D(ret["quantity"])
            if not candidates:
                self._exc(period, "return_unlinked", "error",
                          f"退货 {ret['receipt_id']} 无任何在先收货，整笔列待核",
                          line_id=lid, ref_id=ret["receipt_id"], amount=qty)
                rows.append((self.batch, ret["receipt_id"], lid, ret["date"], qty,
                             None, ZERO, qty, "unlinked_pending"))
                continue
            if len(candidates) > 1:
                # 无明确关联资料，不任意选取
                self._exc(period, "return_ambiguous", "error",
                          f"退货 {ret['receipt_id']} 对应多笔收货且无明确关联，整笔列待核",
                          line_id=lid, ref_id=ret["receipt_id"], amount=qty)
                rows.append((self.batch, ret["receipt_id"], lid, ret["date"], qty,
                             None, ZERO, qty, "ambiguous_pending"))
                continue

            rec = candidates[0]
            accepted = sum((D(a["accepted_qty"]) for a in self.acc_by_receipt.get(rec["receipt_id"], [])), ZERO)
            unaccepted_pool = D(rec["quantity"]) - accepted
            linked_to_unaccepted = min(qty, max(unaccepted_pool, ZERO))
            linked_to_accepted = qty - linked_to_unaccepted
            if linked_to_accepted > 0:
                self._exc(period, "return_into_accepted", "warning",
                          f"退货 {ret['receipt_id']} 有 {linked_to_accepted} 件冲及已验收量，"
                          "已验收义务不自动减少，须独立核对",
                          line_id=lid, ref_id=ret["receipt_id"], amount=linked_to_accepted)
            rows.append((
                self.batch, ret["receipt_id"], lid, ret["date"], qty,
                rec["receipt_id"], linked_to_unaccepted, linked_to_accepted,
                "linked" if linked_to_accepted == 0 else "linked_partly_accepted",
            ))
        self._insert_many(
            "INSERT INTO fact_return_link VALUES (?,?,?,?,?,?,?,?,?)", rows,
        )

    # ---------- 采购行逐月状态与桥接 ----------
    def _effective_qty(self, line, period_end):
        qty = D(line["quantity"])
        latest = None
        for rv in self.rev_by_line.get(line["line_id"], []):
            if rv["date"] <= period_end and (latest is None or rv["date"] >= latest["date"]):
                latest = rv
        return qty if latest is None else D(latest["new_total_quantity"]), latest

    def _build_line_states(self):
        rows = []
        for lid, line in sorted(self.orders.items()):
            currency = line["currency"]
            price = D(line["unit_price"])
            basis_rate = self.fx.rate(currency, line["date"])
            if basis_rate is None:
                self._exc(month_of(line["date"]), "fx_missing", "error",
                          f"订单 {lid} 订购日无 {currency} 汇率，承诺人民币口径待核",
                          line_id=lid, amount=price, currency=currency)
            for period in self.periods:
                pend = self._period_end(period)
                effective_qty, latest_rev = self._effective_qty(line, pend)

                receives = [r for r in self.rec_by_line.get(lid, [])
                            if r["kind"] == "receive" and r["date"] <= pend]
                returns = [r for r in self.rec_by_line.get(lid, [])
                           if r["kind"] == "return" and r["date"] <= pend]
                received_qty = sum((D(r["quantity"]) for r in receives), ZERO)
                returned_qty = sum((D(r["quantity"]) for r in returns), ZERO)
                net_received = received_qty - returned_qty

                formed_qty = sum((
                    D(a["accepted_qty"]) for a in self.acceptances
                    if a["date"] <= pend
                    and any(r["receipt_id"] == a["receipt_id"] and r["line_id"] == lid
                            for r in self.receipts)
                ), ZERO)
                pending_qty = net_received - formed_qty
                if pending_qty < 0:
                    self._exc(period, "accepted_exceeds_receipt", "error",
                              f"{lid} 累计验收 {formed_qty} 超过净收货 {net_received}",
                              line_id=lid, amount=-pending_qty)
                    pending_qty = ZERO

                if latest_rev is not None and effective_qty < formed_qty:
                    self._exc(period, "revision_below_obligation", "error",
                              f"修订 {latest_rev['revision_id']} 把总量降到 {effective_qty}，"
                              f"低于已验收义务 {formed_qty}；承诺可减，已形成义务保留，"
                              f"冲突量 {formed_qty - effective_qty}",
                              line_id=lid, ref_id=latest_rev["revision_id"],
                              amount=formed_qty - effective_qty)
                if latest_rev is not None and effective_qty < net_received:
                    self._exc(period, "revision_below_receipt", "warning",
                              f"修订 {latest_rev['revision_id']} 总量 {effective_qty} "
                              f"低于净收货 {net_received}，收货义务不抹除",
                              line_id=lid, ref_id=latest_rev["revision_id"],
                              amount=net_received - effective_qty)

                commitment_qty = max(effective_qty - net_received, ZERO)

                # 各口径原币/人民币
                formed_events = [f for f in self.formed_by_line[lid]
                                 if f["acceptance_date"] <= pend]
                formed_fc = sum((f["amount_fc"] for f in formed_events), ZERO)
                # 任一形成事件缺当天汇率 -> 人民币口径整体待核（None），不用零顶替
                formed_cny = (
                    None if any(f["amount_cny"] is None for f in formed_events)
                    else sum((f["amount_cny"] for f in formed_events), ZERO)
                )

                docs = [d for d in self.doc_by_line.get(lid, []) if d["date"] <= pend]
                def doc_sum(kind):
                    fc = sum((D(d["amount"]) for d in docs if d["kind"] == kind), ZERO)
                    cny_parts = []
                    for d in docs:
                        if d["kind"] != kind:
                            continue
                        if line["currency"] == "CNY":
                            cny_parts.append(D(d["amount"]))
                        else:
                            part, _ = self.fx.to_cny(D(d["amount"]), currency, d["date"])
                            cny_parts.append(part)  # 缺汇率为 None
                    cny = None if any(p is None for p in cny_parts) else sum(cny_parts, ZERO)
                    return fc, cny
                invoice_fc, invoice_cny = doc_sum("invoice")
                credit_fc, credit_cny = doc_sum("credit")
                payment_fc, payment_cny = doc_sum("payment")
                refund_fc, refund_cny = doc_sum("refund")
                invoice_net_fc = invoice_fc - credit_fc
                invoice_net_cny = (
                    None if invoice_cny is None or credit_cny is None
                    else invoice_cny - credit_cny
                )
                cash_net_fc = payment_fc - refund_fc
                cash_net_cny = (
                    None if payment_cny is None or refund_cny is None
                    else payment_cny - refund_cny
                )

                commitment_fc = commitment_qty * price
                pending_fc = pending_qty * price
                commitment_cny = None if basis_rate is None else commitment_fc * basis_rate
                pending_cny = None if basis_rate is None else pending_fc * basis_rate
                formed_fx_var = (
                    formed_cny - formed_fc * basis_rate
                    if basis_rate is not None and formed_cny is not None else None
                )
                invoice_fx_var = (
                    invoice_net_cny - invoice_net_fc * basis_rate
                    if basis_rate is not None and invoice_net_cny is not None else None
                )
                cash_fx_var = (
                    cash_net_cny - cash_net_fc * basis_rate
                    if basis_rate is not None and cash_net_cny is not None else None
                )
                unsettled_fc = formed_fc - invoice_net_fc
                unsettled_cny = (
                    None if formed_cny is None or invoice_net_cny is None
                    else formed_cny - invoice_net_cny
                )
                prepaid_fc = cash_net_fc - invoice_net_fc
                prepaid_cny = (
                    None if cash_net_cny is None or invoice_net_cny is None
                    else cash_net_cny - invoice_net_cny
                )

                rows.append((
                    self.batch, period, lid, line["kind"], line["asset_id"], currency,
                    price, line["date"], basis_rate,
                    D(line["quantity"]), effective_qty, formed_qty, pending_qty,
                    returned_qty, commitment_qty,
                    commitment_fc, commitment_cny, pending_fc, pending_cny,
                    formed_fc, formed_cny, formed_fx_var,
                    invoice_fc, invoice_cny, credit_fc, credit_cny,
                    invoice_net_fc, invoice_net_cny, invoice_fx_var,
                    payment_fc, payment_cny, refund_fc, refund_cny,
                    cash_net_fc, cash_net_cny, cash_fx_var,
                    unsettled_fc, unsettled_cny, prepaid_fc, prepaid_cny,
                ))
        self._insert_many(
            "INSERT INTO fact_line_state VALUES (" + ",".join(["?"] * 40) + ")", rows,
        )

    # ---------- 资产归集、直线分担与共享分摊 ----------
    def _build_assets(self):
        cost_rows, schedule_rows, alloc_rows = [], [], []
        costs_by_asset = {}
        for f in sorted(
            (f for fl in self.formed_by_line.values() for f in fl),
            key=lambda f: (f["acceptance_date"], f["acceptance_id"]),
        ):
            if f["exp_class"] != "cip" or not f["asset_id"]:
                continue
            asset = self.assets.get(f["asset_id"])
            if asset is None:
                self._exc(f["period"], "asset_missing", "error",
                          f"验收 {f['acceptance_id']} 指向资产 {f['asset_id']} 但无资产资料",
                          line_id=f["line_id"], asset_id=f["asset_id"])
                continue
            receipt = next(r for r in self.receipts if r["receipt_id"] == f["receipt_id"])
            pre = receipt["date"] < asset["ready_at"]
            cost_rows.append((
                self.batch, f["asset_id"], f["line_id"], f["acceptance_id"],
                f["receipt_id"], receipt["date"], asset["ready_at"], pre,
                f["period"], f["amount_cny"],
            ))
            if f["amount_cny"] is None:
                self._exc(f["period"], "asset_cost_fx_missing", "error",
                          f"验收 {f['acceptance_id']} 缺当天汇率，资产归集额待核，"
                          "不参与分担基数",
                          line_id=f["line_id"], asset_id=f["asset_id"],
                          ref_id=f["acceptance_id"])
                continue
            costs_by_asset.setdefault(f["asset_id"], []).append({
                "fill_period": f["period"], "amount": f["amount_cny"],
                "line_id": f["line_id"], "acceptance_id": f["acceptance_id"],
            })
        self._insert_many(
            "INSERT INTO fact_asset_cost VALUES (?,?,?,?,?,?,?,?,?,?)", cost_rows,
        )

        for asset_id, asset in sorted(self.assets.items()):
            costs = costs_by_asset.get(asset_id, [])
            useful = int(asset["useful_months"])
            first_share = add_months(month_of(asset["ready_at"]), 1)
            # 分段：首个分担段含启用前归集；每个补到启用后的成本开启新段并重算剩余月度额
            events = sorted(costs, key=lambda c: c["fill_period"])
            segments = []
            basis = ZERO
            seg_start = first_share
            for c in events:
                if c["fill_period"] < first_share:
                    basis += c["amount"]
                else:
                    # 补齐月开启新段（首月不早于启用次月）
                    if basis > 0:
                        segments.append((seg_start, basis))
                    basis += c["amount"]
                    seg_start = max(c["fill_period"], first_share)
            segments.append((seg_start, basis))

            # 各月适用的段与月度额（视野只生成到 horizon_last）
            seg_index = 0
            for period in self.periods:
                if period < first_share:
                    continue
                while seg_index + 1 < len(segments) and segments[seg_index + 1][0] <= period:
                    seg_index += 1
                seg_start_p, basis_p = segments[seg_index]
                elapsed = months_between(first_share, seg_start_p)
                remaining = useful - elapsed
                if remaining <= 0:
                    self._exc(period, "asset_life_exceeded", "error",
                              f"资产 {asset_id} 补造成本超出使用年限", asset_id=asset_id)
                    continue
                monthly_exact = basis_p / remaining
                monthly_settled = round_cent(monthly_exact)
                schedule_rows.append((
                    self.batch, asset_id, period, basis_p, monthly_exact,
                    monthly_settled, seg_start_p, useful,
                ))

                usage = self.usage_by_asset_period.get((asset_id, period))
                if usage is None:
                    # 无使用记录：全部留待分，不丢给任何项目
                    alloc_rows.append((
                        self.batch, asset_id, period, None, ZERO, ZERO,
                        Decimal("1"), monthly_settled, True, "no_usage_pending",
                    ))
                    self._exc(period, "usage_missing", "warning",
                              f"资产 {asset_id} 在 {period} 无使用记录，当月分担全额待分",
                              recurring=True, asset_id=asset_id, amount=monthly_settled)
                    continue
                coverage = D(usage["coverage_fraction"])
                project_rows = [
                    u for u in self.usage
                    if u["asset_id"] == asset_id and u["period"] == period
                ]
                total_hours = sum((D(u["hours"]) for u in project_rows), ZERO)
                if total_hours <= 0:
                    self._exc(period, "usage_zero_hours", "error",
                              f"资产 {asset_id} 在 {period} 使用工时为零", asset_id=asset_id)
                    continue
                parts = []
                seen_coverage = set()
                for u in sorted(project_rows, key=lambda u: u["project"]):
                    fraction = D(u["hours"]) / total_hours * coverage
                    parts.append((u["project"], fraction))
                    seen_coverage.add(D(u["coverage_fraction"]))
                if len(seen_coverage) > 1:
                    self._exc(period, "coverage_inconsistent", "error",
                              f"资产 {asset_id} 在 {period} 同资产同月 coverage_fraction 不一致",
                              asset_id=asset_id)
                covered = sum((f for _, f in parts), ZERO)
                if covered < 1:
                    parts.append((None, 1 - covered))  # 未覆盖残量待分，不归一到 100%
                amounts = allocate(monthly_settled, parts)
                for u in sorted(project_rows, key=lambda u: u["project"]):
                    fraction = D(u["hours"]) / total_hours * coverage
                    alloc_rows.append((
                        self.batch, asset_id, period, u["project"], D(u["hours"]),
                        coverage, fraction, amounts[u["project"]], False, "usage",
                    ))
                if None in amounts:
                    alloc_rows.append((
                        self.batch, asset_id, period, None, ZERO, coverage,
                        1 - covered, amounts[None], True, "coverage_tail",
                    ))
        self._insert_many(
            "INSERT INTO fact_asset_schedule VALUES (?,?,?,?,?,?,?,?)", schedule_rows,
        )
        self._insert_many(
            "INSERT INTO fact_asset_alloc VALUES (?,?,?,?,?,?,?,?,?,?)", alloc_rows,
        )

    # ---------- 项目度量长表 ----------
    def _build_project_measures(self):
        rows = []

        def add(period, project, measure, amount_cny, basis, **refs):
            if amount_cny is None:
                return
            rows.append((
                self.batch, period, project, measure, amount_cny,
                round_cent(amount_cny), basis, refs.get("line_id"),
                refs.get("acceptance_id"), refs.get("document_id"), refs.get("asset_id"),
            ))

        # 行级口径按 funding 出资比例解释预算承担
        state = self.con.execute(
            "SELECT * FROM fact_line_state WHERE _batch = ? ORDER BY period, line_id",
            [self.batch],
        ).fetchall()
        cols = [d[0] for d in self.con.description]
        for tup in state:
            s = dict(zip(cols, tup))
            shares = [(f["project"], D(f["share"])) for f in self.fund_by_line.get(s["line_id"], [])]
            covered = sum((f for _, f in shares), ZERO)
            if covered < 1:
                self._exc(s["period"], "funding_gap", "warning",
                          f"{s['line_id']} 出资比例合计 {covered} < 1，残量预算承担待分",
                          line_id=s["line_id"], amount=1 - covered)
            for measure, col in (
                ("budget_commitment", "commitment_cny"),
                ("pending_obligation", "pending_cny"),
                ("invoice_net", "invoice_net_cny"),
                ("cash_net", "cash_net_cny"),
                ("unsettled", "unsettled_cny"),
                ("prepaid", "prepaid_cny"),
            ):
                total = s[col]
                if total is None:
                    continue
                amounts = split_by_shares(total, shares)
                for project, amount in amounts.items():
                    add(s["period"], project, measure, amount, "funding_share",
                        line_id=s["line_id"])

        # 当期费用 / 在建归集（验收形成事件，逐笔）
        for f in sorted(
            (f for fl in self.formed_by_line.values() for f in fl),
            key=lambda f: (f["period"], f["acceptance_id"]),
        ):
            shares = [(x["project"], D(x["share"]))
                      for x in self.fund_by_line.get(f["line_id"], [])]
            measure = "expense_period" if f["exp_class"] == "expense" else "cip_addition"
            if f["amount_cny"] is None:
                continue
            amounts = split_by_shares(f["amount_cny"], shares)
            for project, amount in amounts.items():
                add(f["period"], project, measure, amount, "funding_share",
                    line_id=f["line_id"], acceptance_id=f["acceptance_id"],
                    asset_id=f["asset_id"])

        # 共享仪器分担按实际 usage（出资比例不代替 usage）
        allocs = self.con.execute(
            "SELECT * FROM fact_asset_alloc WHERE _batch = ? ORDER BY period, asset_id, project",
            [self.batch],
        ).fetchall()
        acols = [d[0] for d in self.con.description]
        for tup in allocs:
            a = dict(zip(acols, tup))
            add(a["period"], a["project"], "asset_share", D(a["amount_cent"]),
                a["basis"], asset_id=a["asset_id"])

        self._insert_many(
            "INSERT INTO fact_project_measure VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows,
        )

    # ---------- 例外落表 ----------
    def _flush_exceptions(self):
        rows = [
            (self.batch, e["period"], e["ex_type"], e["severity"], e["line_id"],
             e["document_id"], e["asset_id"], e["ref_id"], e["amount"],
             e["currency"], e["message"])
            for e in self.exceptions
        ]
        self._insert_many(
            "INSERT INTO fact_exception VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows,
        )

    @staticmethod
    def _period_end(period):
        nxt = add_months(period, 1)
        return datetime.date(int(nxt[:4]), int(nxt[5:]), 1) - datetime.timedelta(days=1)
