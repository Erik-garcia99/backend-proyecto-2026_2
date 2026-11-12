"""Siembra productos de prueba para el POS.

Uso:
    python -m scripts.seed_pos_products <branch_id> [cantidad]

Requiere DATABASE_URL en el entorno (igual que la app).
"""
import sys
from uuid import UUID

from app import db


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit('Uso: python -m scripts.seed_pos_products <branch_id> [cantidad]')
    branch_id = UUID(sys.argv[1])
    amount = int(sys.argv[2]) if len(sys.argv) > 2 else 2000

    db.pool.open()
    try:
        with db.get_connection() as conn:
            branch = conn.execute('SELECT organization_id FROM branch WHERE id=%s', (branch_id,)).fetchone()
            if not branch:
                raise SystemExit('Sucursal no encontrada')
            org_id = branch['organization_id']
            for n in range(1, amount + 1):
                sku = f'POS-{n:05d}'
                product = conn.execute(
                    '''INSERT INTO product (organization_id, sku, name, barcode, sale_price, tax_rate)
                       VALUES (%s,%s,%s,%s,%s,0.16)
                       ON CONFLICT (organization_id, barcode) DO UPDATE SET name=EXCLUDED.name
                       RETURNING id''',
                    (org_id, sku, f'Producto de prueba {n}', sku, 10 + (n % 90))).fetchone()
                conn.execute(
                    '''INSERT INTO branch_inventory (branch_id, product_id, quantity_on_hand)
                       VALUES (%s,%s,%s)
                       ON CONFLICT (branch_id, product_id)
                       DO UPDATE SET quantity_on_hand=EXCLUDED.quantity_on_hand, updated_at=NOW()''',
                    (branch_id, product['id'], 100))
            conn.commit()
        print(f'{amount} productos sembrados en la sucursal {branch_id}')
    finally:
        db.pool.close()


if __name__ == '__main__':
    main()
