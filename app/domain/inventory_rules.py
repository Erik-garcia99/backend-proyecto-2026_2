"""Reglas puras de inventario del POS (sin SQL)."""

from decimal import Decimal


MOVEMENT_TYPES = {'sale', 'sale_void', 'return', 'receipt', 'adjustment_in', 'adjustment_out'}


def apply_movement(current: int, delta: int) -> int:
    return current + delta


def validate_stock(current: int, delta: int, allow_negative: bool = False) -> None:
    if not allow_negative and current + delta < 0:
        raise ValueError('INSUFFICIENT_STOCK')


def sale_total(items: list[dict]) -> Decimal:
    total = Decimal('0')
    for item in items:
        total += Decimal(str(item['unit_price'])) * item['quantity']
    return total


def validate_return(sold: int, already_returned: int, requested: int) -> None:
    if requested < 0:
        raise ValueError('INVALID_QUANTITY')
    if already_returned + requested > sold:
        raise ValueError('RETURN_EXCEEDS_SOLD')