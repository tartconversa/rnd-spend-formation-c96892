"""项目-组织时态解释。

组织归属按 effective 日期解释历史：区间左闭右开 [start, end)。
- 历史视角：每条事实按其业务时点所在区间取当时组织（负责人换组织不改旧月）
- 当前视角：按查询日取当前组织
"""
import datetime


class OrgResolver:
    def __init__(self, org_rows):
        self._rels = {}
        for r in org_rows:
            self._rels.setdefault(r["project"], []).append(r)
        for rels in self._rels.values():
            rels.sort(key=lambda r: r["start"])

    def org_at(self, project, on_date):
        """返回项目在 on_date 当天的组织；无覆盖区间返回 None（待核，不猜）。"""
        for r in self._rels.get(project, []):
            end = r.get("end", r.get("end_date"))
            if r["start"] <= on_date and (end is None or on_date < end):
                return r["organization"]
        return None

    def org_for_period(self, project, period):
        """历史视角：用业务月最后一天定位当时组织。"""
        year, month = (int(x) for x in period.split("-"))
        if month == 12:
            last = datetime.date(year, 12, 31)
        else:
            last = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
        return self.org_at(project, last)
