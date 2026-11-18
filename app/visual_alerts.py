"""Fase 7 (B4) — F-18/P-04: productor visual de alertas.

Convierte los estados confirmados por ``roi_classification`` en episodios de
alerta ``stock_low`` / ``stock_empty`` deduplicados por sucursal + ROI + tipo.
Un episodio persistente no se repite; la recuperación confirmada lo cierra y
una nueva caída abre otro. Reutiliza la tabla ``alert`` y ``system_log``;
no crea otra bandeja ni modifica inventario lógico (F-21, C-17).
"""
import asyncio
import logging
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from .roi_classification import STATE_ROWS_SQL, scan_branch_states

log = logging.getLogger(__name__)
VISUAL_INTERVAL = 5
STOCK_TYPES = {'low': 'stock_low', 'empty': 'stock_empty'}
ACTIVE = ('open', 'acknowledged')
monitor_health = {'last_success_at': None, 'error': False}


def _metadata(row, status, occupancy_pct):
    return {
        'source': 'roi_state_monitor', 'roi_id': str(row['roi_id']),
        'product_id': str(row['product_id']), 'sku': row.get('sku'),
        'shelf_name': row.get('shelf_name'), 'status': status,
        'occupancy_pct': occupancy_pct,
        'config_version': row.get('config_version'),
        'calibration_version': row.get('calibration_version'),
    }


def visual_episode(connection, *, row, quality, status):
    """Abre, cierra o deja intacto el episodio visual de una ROI. Sin commit."""
    if quality != 'ok':
        # Una lectura antigua, sin datos o con cámara movida no puede abrir ni
        # cerrar una alerta: solo se actúa con estado confirmado y fresco.
        return None
    desired = STOCK_TYPES.get(status)
    key_base = f"roi:{row['roi_id']}"
    connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', (key_base,))
    existing = connection.execute(
        """SELECT id,alert_type FROM alert WHERE branch_id=%s AND dedupe_key LIKE %s
           AND status=ANY(%s) FOR UPDATE""",
        (row['branch_id'], key_base + ':stock_%', list(ACTIVE))).fetchall()

    if desired is None:
        for alert in existing:
            connection.execute(
                "UPDATE alert SET status='resolved',resolved_at=NOW() WHERE id=%s", (alert['id'],))
            connection.execute(
                """INSERT INTO system_log(branch_id,node_id,gateway_id,category,severity,
                     event_type,message,metadata) VALUES(%s,%s,%s,'alerts','info',
                     'alert.recovered','La repisa volvió a un estado disponible',%s)""",
                (row['branch_id'], row.get('node_id'), row.get('gateway_id'),
                 Jsonb(_metadata(row, status, row.get('occupancy_pct')))))
        return None

    for alert in existing:
        if alert['alert_type'] == desired:
            return None  # misma condición: no repetir
        # Cambió la severidad (bajo -> agotado o viceversa): cierra el episodio anterior.
        connection.execute(
            "UPDATE alert SET status='resolved',resolved_at=NOW() WHERE id=%s", (alert['id'],))

    severity = 'critical' if desired == 'stock_empty' else 'warning'
    label = 'Agotado' if desired == 'stock_empty' else 'Bajo'
    message = (f"Repisa {row.get('shelf_name') or 'sin nombre'}: producto "
               f"{row.get('sku') or 'sin SKU'} en estado {label} con "
               f"{row.get('occupancy_pct')}% de ocupación visual.")
    inserted = connection.execute(
        """INSERT INTO alert(branch_id,alert_type,severity,node_id,gateway_id,message,
             metadata,dedupe_key) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (row['branch_id'], desired, severity, row.get('node_id'), row.get('gateway_id'),
         message, Jsonb(_metadata(row, status, row.get('occupancy_pct'))),
         key_base + ':' + desired)).fetchone()
    connection.execute(
        """INSERT INTO system_log(branch_id,node_id,gateway_id,category,severity,
             event_type,message,metadata) VALUES(%s,%s,%s,'alerts','info',
             'alert.opened',%s,%s)""",
        (row['branch_id'], row.get('node_id'), row.get('gateway_id'), message,
         Jsonb({'alert_id': str(inserted['id']),
                **_metadata(row, status, row.get('occupancy_pct'))})))
    return inserted['id']


def scan_visual_states(get_connection, reader=None):
    """Clasifica todas las ROI activas y aplica su episodio visual."""
    with get_connection() as connection:
        branches = connection.execute(
            """SELECT DISTINCT s.branch_id FROM roi r JOIN shelf s ON s.id=r.shelf_id
               WHERE r.is_active AND s.is_active""").fetchall()
    for branch in branches:
        branch_id = branch['branch_id']
        scan_branch_states(get_connection, branch_id, reader)
        with get_connection() as connection:
            rows = connection.execute(STATE_ROWS_SQL, (branch_id,)).fetchall()
            for row in rows:
                last = row.get('last_reading_at')
                age = max(0, int((datetime.now(timezone.utc) - last).total_seconds())) if last else None
                if row['calibration_status'] != 'calibrated':
                    quality = 'not_calibrated'
                elif age is None:
                    quality = 'no_data'
                elif age >= 60:
                    quality = 'stale'
                else:
                    quality = 'ok'
                visual_episode(connection, row={**row, 'branch_id': branch_id},
                               quality=quality, status=row.get('status'))
            connection.commit()
    return len(branches)


async def monitor(get_connection, stop, reader=None):
    monitor_health.update(last_success_at=None, error=False)
    while not stop.is_set():
        try:
            await asyncio.to_thread(scan_visual_states, get_connection, reader)
            monitor_health.update(last_success_at=datetime.now(timezone.utc), error=False)
        except Exception:
            monitor_health['error'] = True
            log.exception('No se pudo evaluar el estado visual de las repisas para Alertas')
        try:
            await asyncio.wait_for(stop.wait(), VISUAL_INTERVAL)
        except asyncio.TimeoutError:
            pass
