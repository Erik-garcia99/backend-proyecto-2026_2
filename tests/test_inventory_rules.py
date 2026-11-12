from decimal import Decimal

import pytest

from app.domain.inventory_rules import (
    apply_movement,
    sale_total,
    validate_return,
    validate_stock,
)


def test_apply_movement_suma_y_resta():
    assert apply_movement(10, -3) == 7
    assert apply_movement(10, 5) == 15


def test_validate_stock_no_permite_negativo():
    validate_stock(10, -10)
    with pytest.raises(ValueError, match='INSUFFICIENT_STOCK'):
        validate_stock(10, -11)


def test_validate_stock_puede_permitir_negativo_si_se_configura():
    validate_stock(0, -1, allow_negative=True)


def test_sale_total_suma_lineas():
    total = sale_total([
        {'unit_price': Decimal('10.50'), 'quantity': 2},
        {'unit_price': Decimal('3.25'), 'quantity': 4},
    ])
    assert total == Decimal('34.00')


def test_validate_return_no_excede_lo_vendido():
    validate_return(sold=5, already_returned=2, requested=3)
    with pytest.raises(ValueError, match='RETURN_EXCEEDS_SOLD'):
        validate_return(sold=5, already_returned=2, requested=4)


def test_validate_return_rechaza_cantidad_negativa():
    with pytest.raises(ValueError, match='INVALID_QUANTITY'):
        validate_return(sold=5, already_returned=0, requested=-1)
