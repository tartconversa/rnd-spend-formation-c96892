"""金额工具：全程 Decimal，禁止用 float 承载业务金额。

口径（docs/资料口径.md）：结算舍入到分，尾差归该账单分摊基数最大项，
并列按业务键升序；待分（project 为空）排在有项目键的记录之后。
"""
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")
ZERO = Decimal("0")


def D(value) -> Decimal:
    """把字符串/整数/Decimal 转成 Decimal；float 不允许，避免二进制误差。"""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise TypeError("金额与数量禁止使用 float：" + repr(value))
    if value is None or value == "":
        return None
    return Decimal(str(value))


def round_cent(value: Decimal) -> Decimal:
    """四舍五入到分（ROUND_HALF_UP）。"""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _key_rank(key):
    """尾差归属并列时的业务键次序：字符串键升序，空项目（待分）最后。"""
    return (1, "") if key is None else (0, str(key))


def allocate(total_cent: Decimal, parts):
    """把已经到分的总额按分摊基数切到分。

    parts: 可迭代 (key, base_fraction)，base_fraction 为精确 Decimal，
    各基数之和应为 1（含待分残量）。返回 key -> 到分金额，各行之和
    严格等于 total_cent；舍入尾差加给基数最大项，并列按业务键升序。
    """
    parts = [(key, D(base)) for key, base in parts]
    if not parts:
        return {}
    rounded = {key: round_cent(total_cent * base) for key, base in parts}
    tail = total_cent - sum(rounded.values(), ZERO)
    if tail != ZERO:
        # 尾差归基数最大项；并列按业务键升序（待分最后）
        max_base = max(base for _, base in parts)
        winner = min(
            (item for item in parts if item[1] == max_base),
            key=lambda item: _key_rank(item[0]),
        )
        rounded[winner[0]] += tail
    return rounded


def split_by_shares(total: Decimal, shares):
    """按出资/使用份额切分精确金额（未到分也可），返回到分结果。

    shares: (key, fraction)。份额之和不足 1 时，残量以 key=None 保留，
    不向有记录者归一到 100%。
    """
    shares = [(key, D(f)) for key, f in shares]
    covered = sum((f for _, f in shares), ZERO)
    parts = list(shares)
    if covered < 1:
        parts.append((None, 1 - covered))
    return allocate(round_cent(total), parts)
