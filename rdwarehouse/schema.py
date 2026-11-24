"""DuckDB 仓表定义。

三层：
  load_batch/loaded_file：接收批次与文件指纹（相同摘要不增加事实）
  raw_*：原始资料，按批次追加，只增不改
  fact_*：按批次视野重算的事实与例外
  pkg_*：冻结的月度管理包（草稿/已确认）
金额统一 DECIMAL(38,6) 保留精确值，到分结算额单独 DECIMAL(18,2)。
"""

DDL = """
CREATE TABLE IF NOT EXISTS load_batch (
    batch_id INTEGER PRIMARY KEY,
    received_at TIMESTAMP NOT NULL,
    note VARCHAR
);

CREATE TABLE IF NOT EXISTS loaded_file (
    batch_id INTEGER NOT NULL,
    table_name VARCHAR NOT NULL,
    file_path VARCHAR NOT NULL,
    sha256 VARCHAR NOT NULL,
    row_count INTEGER NOT NULL,
    status VARCHAR NOT NULL,
    PRIMARY KEY (batch_id, table_name, sha256)
);

-- ============ 原始层（追加，保留整行修订历史） ============
CREATE TABLE IF NOT EXISTS raw_orders (
    _batch INTEGER, _file VARCHAR, _row BIGINT, line_id VARCHAR, kind VARCHAR,
    asset_id VARCHAR, quantity DECIMAL(38,6), unit_price DECIMAL(38,6),
    currency VARCHAR, date DATE
);
CREATE TABLE IF NOT EXISTS raw_funding (
    _batch INTEGER, _file VARCHAR, _row BIGINT, fund_id VARCHAR, line_id VARCHAR,
    project VARCHAR, share DECIMAL(38,6)
);
CREATE TABLE IF NOT EXISTS raw_receipts (
    _batch INTEGER, _file VARCHAR, _row BIGINT, receipt_id VARCHAR, line_id VARCHAR,
    kind VARCHAR, quantity DECIMAL(38,6), date DATE
);
CREATE TABLE IF NOT EXISTS raw_acceptances (
    _batch INTEGER, _file VARCHAR, _row BIGINT, acceptance_id VARCHAR, receipt_id VARCHAR,
    accepted_qty DECIMAL(38,6), date DATE
);
CREATE TABLE IF NOT EXISTS raw_documents (
    _batch INTEGER, _file VARCHAR, _row BIGINT, document_id VARCHAR, line_id VARCHAR,
    kind VARCHAR, amount DECIMAL(38,6), date DATE
);
CREATE TABLE IF NOT EXISTS raw_assets (
    _batch INTEGER, _file VARCHAR, _row BIGINT, asset_id VARCHAR, ready_at DATE,
    useful_months BIGINT
);
CREATE TABLE IF NOT EXISTS raw_usage (
    _batch INTEGER, _file VARCHAR, _row BIGINT, usage_id VARCHAR, asset_id VARCHAR,
    period VARCHAR, project VARCHAR, hours DECIMAL(38,6),
    coverage_fraction DECIMAL(38,6)
);
CREATE TABLE IF NOT EXISTS raw_organizations (
    _batch INTEGER, _file VARCHAR, _row BIGINT, relation_id VARCHAR, project VARCHAR,
    organization VARCHAR, start DATE, end_date DATE
);
CREATE TABLE IF NOT EXISTS raw_fx (
    _batch INTEGER, _file VARCHAR, _row BIGINT, currency VARCHAR, date DATE,
    cny_per_unit DECIMAL(38,6)
);
CREATE TABLE IF NOT EXISTS raw_revisions (
    _batch INTEGER, _file VARCHAR, _row BIGINT, revision_id VARCHAR, line_id VARCHAR,
    new_total_quantity DECIMAL(38,6), date DATE
);

-- ============ 事实层（按批次视野重算） ============
-- 采购行逐月状态：承诺/待核/形成/票/款的独立口径与桥接
CREATE TABLE IF NOT EXISTS fact_line_state (
    _batch INTEGER, period VARCHAR, line_id VARCHAR, kind VARCHAR,
    asset_id VARCHAR, currency VARCHAR, unit_price DECIMAL(38,6),
    order_date DATE, basis_rate DECIMAL(38,6),
    order_qty DECIMAL(38,6), effective_qty DECIMAL(38,6),
    formed_qty DECIMAL(38,6), pending_qty DECIMAL(38,6),
    returned_qty DECIMAL(38,6), commitment_qty DECIMAL(38,6),
    commitment_fc DECIMAL(38,6), commitment_cny DECIMAL(38,6),
    pending_fc DECIMAL(38,6), pending_cny DECIMAL(38,6),
    formed_fc DECIMAL(38,6), formed_cny DECIMAL(38,6),
    formed_fx_var DECIMAL(38,6),
    invoice_fc DECIMAL(38,6), invoice_cny DECIMAL(38,6),
    credit_fc DECIMAL(38,6), credit_cny DECIMAL(38,6),
    invoice_net_fc DECIMAL(38,6), invoice_net_cny DECIMAL(38,6),
    invoice_fx_var DECIMAL(38,6),
    payment_fc DECIMAL(38,6), payment_cny DECIMAL(38,6),
    refund_fc DECIMAL(38,6), refund_cny DECIMAL(38,6),
    cash_net_fc DECIMAL(38,6), cash_net_cny DECIMAL(38,6),
    cash_fx_var DECIMAL(38,6),
    unsettled_fc DECIMAL(38,6), unsettled_cny DECIMAL(38,6),
    prepaid_fc DECIMAL(38,6), prepaid_cny DECIMAL(38,6)
);

-- 验收形成事实（决定费用/资产去向）
CREATE TABLE IF NOT EXISTS fact_formed (
    _batch INTEGER, acceptance_id VARCHAR, line_id VARCHAR,
    receipt_id VARCHAR, asset_id VARCHAR, exp_class VARCHAR,
    acceptance_date DATE, period VARCHAR, quantity DECIMAL(38,6),
    unit_price DECIMAL(38,6), fx_rate DECIMAL(38,6),
    amount_fc DECIMAL(38,6), amount_cny DECIMAL(38,6),
    fx_missing BOOLEAN
);

-- 票据与现金事实（四类独立，红冲/退款不靠负号推断）
CREATE TABLE IF NOT EXISTS fact_document (
    _batch INTEGER, document_id VARCHAR, line_id VARCHAR, kind VARCHAR,
    doc_date DATE, period VARCHAR, amount_fc DECIMAL(38,6),
    fx_rate DECIMAL(38,6), amount_cny DECIMAL(38,6), fx_missing BOOLEAN
);

-- 退货关联（无明确关联且候选不唯一时整笔列待核）
CREATE TABLE IF NOT EXISTS fact_return_link (
    _batch INTEGER, receipt_id VARCHAR, line_id VARCHAR,
    return_date DATE, return_qty DECIMAL(38,6),
    linked_receipt_id VARCHAR, linked_qty DECIMAL(38,6),
    unlinked_qty DECIMAL(38,6), status VARCHAR
);

-- 资产成本归集（启用前/后补均按实际服务发生日判断）
CREATE TABLE IF NOT EXISTS fact_asset_cost (
    _batch INTEGER, asset_id VARCHAR, line_id VARCHAR,
    acceptance_id VARCHAR, receipt_id VARCHAR,
    receipt_date DATE, ready_at DATE, pre_enablement BOOLEAN,
    fill_period VARCHAR, amount_cny DECIMAL(38,6)
);

-- 资产月度直线分担计划（补齐月起重算剩余月度额）
CREATE TABLE IF NOT EXISTS fact_asset_schedule (
    _batch INTEGER, asset_id VARCHAR, period VARCHAR,
    cost_basis_cny DECIMAL(38,6), monthly_exact DECIMAL(38,10),
    monthly_settled DECIMAL(18,2), segment_start VARCHAR,
    useful_months BIGINT
);

-- 资产月度项目分担（含待分行：project 为空）
CREATE TABLE IF NOT EXISTS fact_asset_alloc (
    _batch INTEGER, asset_id VARCHAR, period VARCHAR, project VARCHAR,
    hours DECIMAL(38,6), coverage_fraction DECIMAL(38,6),
    share_fraction DECIMAL(38,10), amount_cent DECIMAL(18,2),
    is_tail BOOLEAN, basis VARCHAR
);

-- 项目口径度量（长表：一个度量一行，不用一个金额列兼任口径）
CREATE TABLE IF NOT EXISTS fact_project_measure (
    _batch INTEGER, period VARCHAR, project VARCHAR, measure VARCHAR,
    amount_cny DECIMAL(38,6), amount_cent DECIMAL(18,2), basis VARCHAR,
    line_id VARCHAR, acceptance_id VARCHAR, document_id VARCHAR,
    asset_id VARCHAR
);

-- 例外/待核/未决差额（第一类公民，不静默修正）
CREATE TABLE IF NOT EXISTS fact_exception (
    _batch INTEGER, period VARCHAR, ex_type VARCHAR, severity VARCHAR,
    line_id VARCHAR, document_id VARCHAR, asset_id VARCHAR,
    ref_id VARCHAR, amount DECIMAL(38,6), currency VARCHAR,
    message VARCHAR
);

-- ============ 月度管理包（冻结副本，确认后不改原内容） ============
CREATE TABLE IF NOT EXISTS pkg_header (
    period VARCHAR, _batch INTEGER, built_at TIMESTAMP,
    status VARCHAR, confirmed_at TIMESTAMP,
    PRIMARY KEY (period, _batch)
);
CREATE TABLE IF NOT EXISTS pkg_measure (
    period VARCHAR, _batch INTEGER, project VARCHAR, measure VARCHAR,
    amount_cny DECIMAL(38,6), amount_cent DECIMAL(18,2), basis VARCHAR,
    line_id VARCHAR, acceptance_id VARCHAR, document_id VARCHAR,
    asset_id VARCHAR
);
CREATE TABLE IF NOT EXISTS pkg_line (
    period VARCHAR, _batch INTEGER, line_id VARCHAR, kind VARCHAR,
    asset_id VARCHAR, currency VARCHAR,
    commitment_qty DECIMAL(38,6), commitment_cny DECIMAL(38,6),
    pending_qty DECIMAL(38,6), pending_cny DECIMAL(38,6),
    formed_fc DECIMAL(38,6), formed_cny DECIMAL(38,6),
    formed_fx_var DECIMAL(38,6),
    invoice_net_fc DECIMAL(38,6), invoice_net_cny DECIMAL(38,6),
    cash_net_fc DECIMAL(38,6), cash_net_cny DECIMAL(38,6),
    unsettled_cny DECIMAL(38,6), prepaid_cny DECIMAL(38,6)
);
CREATE TABLE IF NOT EXISTS pkg_asset (
    period VARCHAR, _batch INTEGER, asset_id VARCHAR,
    cost_basis_cny DECIMAL(38,6), monthly_settled DECIMAL(18,2),
    allocated_cent DECIMAL(18,2), unallocated_cent DECIMAL(18,2)
);
CREATE TABLE IF NOT EXISTS pkg_exception (
    period VARCHAR, _batch INTEGER, ex_type VARCHAR, severity VARCHAR,
    line_id VARCHAR, document_id VARCHAR, asset_id VARCHAR,
    ref_id VARCHAR, amount DECIMAL(38,6), currency VARCHAR, message VARCHAR
);
"""

# 按批次重建的事实表（同批次重建=替换，保证幂等）
FACT_TABLES = [
    "fact_line_state",
    "fact_formed",
    "fact_document",
    "fact_return_link",
    "fact_asset_cost",
    "fact_asset_schedule",
    "fact_asset_alloc",
    "fact_project_measure",
    "fact_exception",
]

RAW_TABLES = [
    "orders", "funding", "receipts", "acceptances", "documents",
    "assets", "usage", "organizations", "fx", "revisions",
]
