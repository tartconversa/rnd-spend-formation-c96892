"""命令行入口：inspect / load / build / query / package。

示例：
  python3 -m rdwarehouse inspect --data data
  python3 -m rdwarehouse load --db warehouse.duckdb --data data --note 基础批次
  python3 -m rdwarehouse build --db warehouse.duckdb
  python3 -m rdwarehouse query --db warehouse.duckdb --view budget --period 2026-10
  python3 -m rdwarehouse package --db warehouse.duckdb build --period 2026-10
  python3 -m rdwarehouse package --db warehouse.duckdb confirm --period 2026-10
  python3 -m rdwarehouse package --db warehouse.duckdb compare --period 2026-10 \
      --base-batch 1 --new-batch 2
"""
import argparse
import json
from decimal import Decimal
from pathlib import Path

from .loader import Warehouse
from .package import Packager
from .query import Queries
from .transform import Engine


def _default(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    import datetime
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    raise TypeError(type(value))


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=_default))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="rdwarehouse", description="研发支出数据仓（内部管理口径）")
    parser.add_argument("--db", default="warehouse.duckdb", help="DuckDB 文件，默认 warehouse.duckdb")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("inspect", help="运行资料容器检查（sourcepack），--data 可传项目根或资料目录").add_argument(
        "--data", default="data")

    p_load = sub.add_parser("load", help="按接收批次装载资料目录")
    p_load.add_argument("--data", required=True)
    p_load.add_argument("--note")

    sub.add_parser("build", help="按最新批次重算全部事实")

    p_q = sub.add_parser("query", help="集市查询")
    p_q.add_argument("--view", required=True,
                     choices=["budget", "expense", "cip", "line", "occupation",
                              "monthly", "exceptions"])
    p_q.add_argument("--period")
    p_q.add_argument("--project")
    p_q.add_argument("--line")
    p_q.add_argument("--asset")
    p_q.add_argument("--current-date", help="当前组织视角查询日，如 2026-12-01")
    p_q.add_argument("--severity")

    p_pkg = sub.add_parser("package", help="月度管理包")
    p_pkg.add_argument("action", choices=["build", "confirm", "compare", "list"])
    p_pkg.add_argument("--period")
    p_pkg.add_argument("--base-batch", type=int)
    p_pkg.add_argument("--new-batch", type=int)

    args = parser.parse_args(argv)

    if args.cmd == "inspect":
        from sourcepack.catalog import inspect
        target = Path(args.data).resolve()
        # 兼容传项目根（含 data/catalog.json）或直接传资料目录（含 catalog.json）
        if (target / "catalog.json").is_file() and not (target / "data" / "catalog.json").is_file():
            target = target.parent
        emit(inspect(target))
        return

    wh = Warehouse(args.db)
    try:
        if args.cmd == "load":
            emit(wh.load_batch(args.data, note=args.note))
        elif args.cmd == "build":
            report = Engine(wh).build()
            emit({"status": "built", **report})
        elif args.cmd == "query":
            q = Queries(wh)
            import datetime
            current_date = (
                datetime.date.fromisoformat(args.current_date) if args.current_date else None
            )
            period = args.period or wh.latest_period()
            if args.view == "budget":
                emit(q.budget_view(period, args.project, current_date))
            elif args.view == "expense":
                emit(q.expense_view(period, args.project, current_date))
            elif args.view == "cip":
                emit(q.cip_view(period, args.asset))
            elif args.view == "line":
                if not args.line:
                    parser.error("line 视角需要 --line")
                emit(q.line_drilldown(args.line, period))
            elif args.view == "occupation":
                emit(q.budget_occupation(period, args.project))
            elif args.view == "monthly":
                emit(q.project_monthly(period, current_date))
            elif args.view == "exceptions":
                emit(q.exceptions(period, args.severity))
        elif args.cmd == "package":
            pkg = Packager(wh)
            if args.action == "build":
                emit(pkg.build(args.period or wh.latest_period()))
            elif args.action == "confirm":
                emit(pkg.confirm(args.period or wh.latest_period()))
            elif args.action == "compare":
                if not (args.period and args.base_batch and args.new_batch):
                    parser.error("compare 需要 --period --base-batch --new-batch")
                emit(pkg.compare(args.period, args.base_batch, args.new_batch))
            elif args.action == "list":
                cur = wh.con.execute(
                    "SELECT * FROM pkg_header ORDER BY period, _batch"
                )
                cols = [d[0] for d in cur.description]
                emit([dict(zip(cols, r)) for r in cur.fetchall()])
    finally:
        wh.close()


if __name__ == "__main__":
    main()
