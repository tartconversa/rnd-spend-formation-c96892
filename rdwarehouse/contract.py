"""输入资料的字段契约，与 data/catalog.json 及 docs/资料口径.md 对齐。

后续批次的同格式文件按同一契约校验；记录键相同的新文件表示整行修订。
"""

# (列名, 类型, 可空)；类型与 sourcepack.catalog.read_table 支持的类型一致
CONTRACT = {
    "orders": {
        "columns": [
            ("line_id", "string", False),
            ("kind", "string", False),
            ("asset_id", "string", True),
            ("quantity", "number", False),
            ("unit_price", "number", False),
            ("currency", "string", False),
            ("date", "date", False),
        ],
        "key": ["line_id"],
    },
    "funding": {
        "columns": [
            ("fund_id", "string", False),
            ("line_id", "string", False),
            ("project", "string", False),
            ("share", "number", False),
        ],
        "key": ["fund_id"],
    },
    "receipts": {
        "columns": [
            ("receipt_id", "string", False),
            ("line_id", "string", False),
            ("kind", "string", False),
            ("quantity", "number", False),
            ("date", "date", False),
        ],
        "key": ["receipt_id"],
    },
    "acceptances": {
        "columns": [
            ("acceptance_id", "string", False),
            ("receipt_id", "string", False),
            ("accepted_qty", "number", False),
            ("date", "date", False),
        ],
        "key": ["acceptance_id"],
    },
    "documents": {
        "columns": [
            ("document_id", "string", False),
            ("line_id", "string", False),
            ("kind", "string", False),
            ("amount", "number", False),
            ("date", "date", False),
        ],
        "key": ["document_id"],
    },
    "assets": {
        "columns": [
            ("asset_id", "string", False),
            ("ready_at", "date", False),
            ("useful_months", "integer", False),
        ],
        "key": ["asset_id"],
    },
    "usage": {
        "columns": [
            ("usage_id", "string", False),
            ("asset_id", "string", False),
            ("period", "string", False),
            ("project", "string", False),
            ("hours", "number", False),
            ("coverage_fraction", "number", False),
        ],
        "key": ["usage_id"],
    },
    "organizations": {
        "columns": [
            ("relation_id", "string", False),
            ("project", "string", False),
            ("organization", "string", False),
            ("start", "date", False),
            ("end", "date", False),
        ],
        "key": ["relation_id"],
    },
    "fx": {
        "columns": [
            ("currency", "string", False),
            ("date", "date", False),
            ("cny_per_unit", "number", False),
        ],
        "key": ["currency", "date"],
    },
    "revisions": {
        "columns": [
            ("revision_id", "string", False),
            ("line_id", "string", False),
            ("new_total_quantity", "number", False),
            ("date", "date", False),
        ],
        "key": ["revision_id"],
    },
}

# 原始表 -> 记录键
KEYS = {name: spec["key"] for name, spec in CONTRACT.items()}


def as_catalog_table(name):
    """转成 sourcepack.catalog.read_table 需要的字典形状。"""
    spec = CONTRACT[name]
    return {
        "columns": [
            {"name": col, "type": typ, "nullable": nullable}
            for col, typ, nullable in spec["columns"]
        ],
        "key": list(spec["key"]),
    }
