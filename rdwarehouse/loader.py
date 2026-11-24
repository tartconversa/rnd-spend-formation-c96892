"""批次装载：把资料目录/文件按接收批次追加进 raw_*。

规则（docs/资料口径.md）：
- 相同文件摘要不增加事实（loaded_file 去重）
- 同一来源记录键的新文件表示整行修订；raw 保留全部历史版本，
  事实计算按批次视野取每个键的最新版本
- 历史事实的业务时点与本次接收时点分开：业务日期留在原字段，
  接收批次记在 _batch / load_batch
"""
from pathlib import Path
import datetime
import hashlib

import duckdb

from .contract import CONTRACT, KEYS, as_catalog_table
from .money import D
from .schema import DDL, RAW_TABLES
from sourcepack.catalog import read_table

# 契约列名 -> raw 表物理列名（DuckDB 保留字等）
RAW_COLUMN_MAP = {"organizations": {"end": "end_date"}}


def _convert(column_type, raw):
    if raw == "" or raw is None:
        return None
    if column_type == "date":
        return datetime.date.fromisoformat(raw)
    if column_type == "integer":
        return int(D(raw))
    if column_type == "number":
        return D(raw)
    return raw


class Warehouse:
    def __init__(self, path=":memory:"):
        self.con = duckdb.connect(path)
        self.con.execute(DDL)

    def close(self):
        self.con.close()

    # ---------- 装载 ----------
    def latest_batch(self):
        rows = self.con.execute("SELECT COALESCE(MAX(batch_id),0) FROM load_batch").fetchall()
        return rows[0][0]

    def latest_period(self, batch=None):
        """事实层已覆盖的最后业务月。"""
        horizon = self.latest_batch() if batch is None else batch
        row = self.con.execute(
            "SELECT MAX(period) FROM fact_line_state WHERE _batch=?", [horizon],
        ).fetchone()
        if not row or not row[0]:
            raise ValueError("事实尚未构建，请先 build")
        return row[0]

    def load_batch(self, source, received_at=None, note=None):
        """装载一个目录（含若干契约内 CSV）为新批次，返回批次报告。"""
        source = Path(source)
        if not source.is_dir():
            raise ValueError("装载源必须是目录：" + str(source))
        received_at = received_at or datetime.datetime.now().astimezone().replace(tzinfo=None)
        batch_id = self.latest_batch() + 1
        report = {"batch_id": batch_id, "files": []}

        files = sorted(p for p in source.glob("*.csv") if p.name[:-4] in CONTRACT)
        if not files:
            raise ValueError("目录中没有契约内资料文件：" + str(source))

        self.con.execute("INSERT INTO load_batch VALUES (?,?,?)", [batch_id, received_at, note])
        for path in files:
            table = path.name[:-4]
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            existed = self.con.execute(
                "SELECT 1 FROM loaded_file WHERE table_name=? AND sha256=?",
                [table, digest],
            ).fetchone()
            if existed:
                # 相同摘要不增加事实
                self.con.execute(
                    "INSERT INTO loaded_file VALUES (?,?,?,?,?,?)",
                    [batch_id, table, str(path), digest, 0, "duplicate_digest_skipped"],
                )
                report["files"].append({"table": table, "status": "skipped", "rows": 0})
                continue

            raw_rows = read_table(path, as_catalog_table(table))
            cols = [RAW_COLUMN_MAP.get(table, {}).get(c[0], c[0])
                    for c in CONTRACT[table]["columns"]]
            values = []
            for seq, row in enumerate(raw_rows):
                converted = [_convert(t, row[name]) for name, t, _ in CONTRACT[table]["columns"]]
                values.append([batch_id, path.name, seq] + converted)
            if values:
                placeholders = ",".join(["?"] * (len(cols) + 3))
                self.con.executemany(
                    f"INSERT INTO raw_{table} VALUES ({placeholders})",
                    values,
                )
            self.con.execute(
                "INSERT INTO loaded_file VALUES (?,?,?,?,?,?)",
                [batch_id, table, str(path), digest, len(raw_rows), "loaded"],
            )
            report["files"].append({"table": table, "status": "loaded", "rows": len(raw_rows)})
        return report

    # ---------- 视野 ----------
    def current_rows(self, table, batch=None):
        """取批次视野内每个记录键的最新版本（整行修订语义）。"""
        if table not in RAW_TABLES:
            raise ValueError("未知资料表：" + table)
        horizon = self.latest_batch() if batch is None else batch
        contract_cols = [c[0] for c in CONTRACT[table]["columns"]]
        phys_cols = [RAW_COLUMN_MAP.get(table, {}).get(c, c) for c in contract_cols]
        key_cols = [RAW_COLUMN_MAP.get(table, {}).get(k, k) for k in KEYS[table]]
        select_sql = ", ".join(
            f"{phys} AS {contract}" for phys, contract in zip(phys_cols, contract_cols)
        )
        key_sql = ", ".join(key_cols)
        rows = self.con.execute(
            f"""
            SELECT {select_sql} FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY {key_sql} ORDER BY _batch DESC, _row DESC
                ) AS rn
                FROM raw_{table} WHERE _batch <= ?
            ) WHERE rn = 1
            """,
            [horizon],
        ).fetchall()
        return [dict(zip(contract_cols, row)) for row in rows]

    def all_rows(self, table, batch=None):
        """取视野内全部原始版本（含被修订的旧版本），用于修订审计。"""
        horizon = self.latest_batch() if batch is None else batch
        contract_cols = [c[0] for c in CONTRACT[table]["columns"]]
        phys_cols = [RAW_COLUMN_MAP.get(table, {}).get(c, c) for c in contract_cols]
        select_sql = ", ".join(
            ["_batch", "_file", "_row"]
            + [f"{phys} AS {contract}" for phys, contract in zip(phys_cols, contract_cols)]
        )
        out_cols = ["_batch", "_file", "_row"] + contract_cols
        rows = self.con.execute(
            f"SELECT {select_sql} FROM raw_{table} WHERE _batch <= ? ORDER BY _batch, _row",
            [horizon],
        ).fetchall()
        return [dict(zip(out_cols, row)) for row in rows]
