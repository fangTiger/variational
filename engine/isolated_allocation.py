"""隔离仓位目标保证金的纯计算，不访问账户或网络。"""
from decimal import Decimal


def required_allocation(
    notional: Decimal, maintenance_margin: Decimal, target_distance: Decimal,
) -> Decimal:
    """计算目标桶总额（非增量）；只接受有限且有经济意义的输入。"""
    try:
        notional, maintenance_margin, target_distance = (
            Decimal(str(value)) for value in (notional, maintenance_margin, target_distance)
        )
        if not all(value.is_finite() for value in (notional, maintenance_margin, target_distance)):
            raise ValueError("保证金输入必须为有限数")
        if notional <= 0 or maintenance_margin < 0 or maintenance_margin >= notional:
            raise ValueError("名义必须大于零，维持保证金必须在零和名义之间")
        if not 0 < target_distance < 1:
            raise ValueError("目标强平距离必须在零和一之间")
    except ArithmeticError as exc:
        raise ValueError("保证金输入无效") from exc
    return target_distance * (notional - maintenance_margin) + maintenance_margin
