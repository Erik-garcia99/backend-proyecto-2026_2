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
            category_names = ['Abarrotes', 'Bebidas', 'Limpieza']
            for name in category_names:
                conn.execute(
                    '''INSERT INTO product_category (organization_id, name)
                       SELECT %s, %s
                       WHERE NOT EXISTS (
                         SELECT 1 FROM product_category WHERE organization_id=%s AND name=%s
                       )''',
                    (org_id, name, org_id, name))
            category_ids = [r['id'] for r in conn.execute(
                '''SELECT id FROM product_category
                   WHERE organization_id=%s AND name=ANY(%s)
                   ORDER BY name''', (org_id, category_names)).fetchall()]
            for n in range(1, amount + 1):
                sku = f'POS-{n:05d}'
                category_id = category_ids[(n - 1) % len(category_ids)] if category_ids else None
                product = conn.execute(
                    '''INSERT INTO product (organization_id, sku, name, barcode, sale_price, tax_rate, category_id)
                       VALUES (%s,%s,%s,%s,%s,0.16,%s)
                       ON CONFLICT (organization_id, barcode) WHERE barcode IS NOT NULL
                       DO UPDATE SET name=EXCLUDED.name, category_id=EXCLUDED.category_id
                       RETURNING id''',
                    (org_id, sku, f'Producto de prueba {n}', sku, 10 + (n % 90), category_id)).fetchone()
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
