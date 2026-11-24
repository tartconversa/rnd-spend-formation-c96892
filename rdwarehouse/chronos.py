"""期间工具：业务月用 'YYYY-MM' 字符串表示，左闭右开与口径一致。"""
import datetime


def month_of(date: datetime.date) -> str:
    return f"{date.year:04d}-{date.month:02d}"


def add_months(period: str, delta: int) -> str:
    year, month = (int(x) for x in period.split("-"))
    index = year * 12 + (month - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_range(start: str, count: int):
    return [add_months(start, i) for i in range(count)]


def period_end(period: str) -> datetime.date:
    """业务月最后一天（含）。"""
    nxt = add_months(period, 1)
    first_of_next = datetime.date(int(nxt[:4]), int(nxt[5:]), 1)
    return first_of_next - datetime.timedelta(days=1)


def sort_periods(periods):
    return sorted(set(periods))


def months_between(start: str, end: str) -> int:
    """end - start 的月数（end 可早于 start，返回负数）。"""
    sy, sm = (int(x) for x in start.split("-"))
    ey, em = (int(x) for x in end.split("-"))
    return ey * 12 + em - (sy * 12 + sm)
