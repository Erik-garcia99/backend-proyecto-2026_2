"""Lectura de temperatura en InfluxDB; los secretos nunca salen del backend."""
import csv
import io
import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel


@dataclass(frozen=True)
class InfluxSettings:
    version: str
    url: str
    token: str = field(repr=False)
    measurement: str
    temperature_field: str
    branch_tag: str
    org: str = ''
    bucket: str = ''
    database: str = ''
    retention_policy: str = ''
    lookback_hours: int = 24
    stale_seconds: int = 90
    timeout_seconds: int = 10

    @classmethod
    def from_env(cls):
        names = ['VERSION', 'URL', 'TOKEN', 'MEASUREMENT', 'TEMPERATURE_FIELD', 'BRANCH_TAG']
        values = {name: os.getenv('INFLUXDB_' + name, '').strip() for name in names}
        required = names + (['ORG', 'BUCKET'] if values['VERSION'] == '2' else ['DATABASE'])
        missing = [name for name in required if not os.getenv('INFLUXDB_' + name, '').strip()]
        if missing:
            raise HTTPException(503, 'Falta configurar en el backend: ' + ', '.join('INFLUXDB_' + n for n in missing))
        try:
            url = urlsplit(values['URL'])
        except ValueError:
            raise HTTPException(503, 'Revisa INFLUXDB_URL: debe ser una URL base HTTPS válida') from None
        if (values['VERSION'] not in {'2', '3'} or url.scheme != 'https' or not url.hostname
                or url.username or url.password or url.query or url.fragment or url.path not in {'', '/'}):
            raise HTTPException(503, 'Revisa INFLUXDB_VERSION (2 o 3) e INFLUXDB_URL (URL base HTTPS)')
        try:
            lookback = int(os.getenv('INFLUXDB_LOOKBACK_HOURS', '24'))
            stale = int(os.getenv('INFLUXDB_STALE_SECONDS', '90'))
            timeout = int(os.getenv('INFLUXDB_TIMEOUT_SECONDS', '10'))
            if not (1 <= lookback <= 168 and 30 <= stale <= 86400 and 1 <= timeout <= 12):
                raise ValueError
        except ValueError:
            raise HTTPException(503, 'Revisa los límites de tiempo de INFLUXDB en el backend') from None
        return cls(version=values['VERSION'], url=values['URL'].rstrip('/'), token=values['TOKEN'],
                   measurement=values['MEASUREMENT'], temperature_field=values['TEMPERATURE_FIELD'],
                   branch_tag=values['BRANCH_TAG'], org=os.getenv('INFLUXDB_ORG', '').strip(),
                   bucket=os.getenv('INFLUXDB_BUCKET', '').strip(),
                   database=os.getenv('INFLUXDB_DATABASE', '').strip(),
                   retention_policy=os.getenv('INFLUXDB_RETENTION_POLICY', '').strip(),
                   lookback_hours=lookback, stale_seconds=stale, timeout_seconds=timeout)


class TemperatureResponse(BaseModel):
    branch_id: UUID
    temperature_c: float | None
    measured_at: datetime | None
    age_seconds: int | None
    stale_after_seconds: int
    status: Literal['ok', 'stale', 'no_data']


def flux_string(value):
    # JSON quoting plus escaping Flux string interpolation.
    return json.dumps(str(value), ensure_ascii=False).replace('${', r'\${')


def influx_identifier(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def flux_query(settings, branch_id):
    q = flux_string
    return (f'from(bucket: {q(settings.bucket)})\n'
            f'  |> range(start: -{settings.lookback_hours}h)\n'
            f'  |> filter(fn: (r) => r._measurement == {q(settings.measurement)}'
            f' and r._field == {q(settings.temperature_field)}'
            f' and r[{q(settings.branch_tag)}] == {q(branch_id)})\n'
            '  |> last()\n  |> group()\n  |> sort(columns: ["_time"], desc: true)\n'
            '  |> limit(n: 1)\n  |> keep(columns: ["_time", "_value"])')


def influxql_query(settings, branch_id):
    # branch_id is a server-validated UUID, never free-form query input.
    return (f'SELECT {influx_identifier(settings.temperature_field)} AS "temperature_c" '
            f'FROM {influx_identifier(settings.measurement)} '
            f'WHERE {influx_identifier(settings.branch_tag)} = \'{UUID(str(branch_id))}\' '
            f'AND time >= now() - {settings.lookback_hours}h AND time <= now() '
            f'AND {influx_identifier(settings.temperature_field)} >= -273.15 '
            'ORDER BY time DESC LIMIT 1')


def parse_flux(text):
    header = None
    point = None
    for row in csv.reader(io.StringIO(text)):
        if not row or row[0].startswith('#'):
            continue
        if '_time' in row and '_value' in row:
            header = row
            continue
        if 'error' in row:
            raise ValueError('Influx query error')
        if header:
            record = dict(zip(header, row))
            if record.get('_time') and record.get('_value') and point is None:
                point = record['_time'], record['_value']
    if text.strip() and header is None:
        raise ValueError('Unexpected Influx CSV')
    return point


def parse_influxql(data):
    if not isinstance(data, dict) or data.get('error') or not isinstance(data.get('results'), list):
        raise ValueError('Unexpected Influx response')
    points = []
    for result in data['results']:
        if result.get('error'):
            raise ValueError('Influx query error')
        for series in result.get('series', []):
            for row in series.get('values', []):
                record = dict(zip(series['columns'], row))
                if record.get('temperature_c') is not None:
                    points.append((record['time'], record['temperature_c']))
    return max(points, key=lambda p: datetime.fromisoformat(p[0].replace('Z', '+00:00'))) if points else None


def read_temperature(settings, branch_id):
    headers = {'Authorization': 'Token ' + settings.token}
    try:
        with httpx.Client(timeout=settings.timeout_seconds, follow_redirects=False) as client:
            if settings.version == '2':
                result = client.post(settings.url + '/api/v2/query', params={'org': settings.org},
                                     headers={**headers, 'Accept': 'application/csv',
                                              'Content-Type': 'application/vnd.flux'},
                                     content=flux_query(settings, branch_id).encode())
            else:
                params = {'db': settings.database, 'q': influxql_query(settings, branch_id)}
                if settings.retention_policy:
                    params['rp'] = settings.retention_policy
                result = client.get(settings.url + '/query', params=params,
                                    headers={**headers, 'Accept': 'application/json'})
            if result.status_code in {401, 403}:
                raise HTTPException(503, 'Revisa el token de lectura y sus permisos en InfluxDB')
            if result.status_code == 429:
                raise HTTPException(503, 'InfluxDB alcanzó su límite de consultas; intenta más tarde')
            result.raise_for_status()
            point = parse_flux(result.text) if settings.version == '2' else parse_influxql(result.json())
        now = datetime.now(timezone.utc)
        measured_at = datetime.fromisoformat(point[0].replace('Z', '+00:00')) if point else None
        temperature = float(point[1]) if point else None
        if point and (not math.isfinite(temperature) or measured_at.tzinfo is None or measured_at > now):
            raise ValueError('Invalid temperature or timestamp')
    except httpx.TimeoutException:
        raise HTTPException(504, 'InfluxDB tardó demasiado en responder') from None
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, OverflowError, csv.Error):
        # No devolver cuerpos/URLs del proveedor: pueden contener datos internos.
        raise HTTPException(502, 'No se pudo consultar InfluxDB; revisa versión, esquema y configuración') from None
    age = max(0, int((now - measured_at).total_seconds())) if measured_at else None
    return TemperatureResponse(branch_id=branch_id, temperature_c=temperature, measured_at=measured_at,
                               age_seconds=age, stale_after_seconds=settings.stale_seconds,
                               status='no_data' if point is None else 'stale' if age >= settings.stale_seconds else 'ok')


def can_read_branch(user, branch):
    return (user['is_superuser'] or (user['organization_id'] == branch['organization_id']
            and (user['role'].lower() == 'owner' or user['branch_id'] == branch['id'])))


def create_router(current_user, get_connection):
    router = APIRouter(tags=['Telemetry'])

    @router.get('/telemetry/branches')
    def branches(response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            rows = connection.execute('''SELECT b.id,b.name,b.organization_id FROM branch b
                JOIN organization o ON o.id=b.organization_id
                WHERE b.is_active AND o.is_active AND (%s OR (b.organization_id=%s
                    AND (%s OR b.id=%s))) ORDER BY b.name,b.id''',
                (user['is_superuser'], user['organization_id'], user['role'].lower() == 'owner',
                 user['branch_id'])).fetchall()
        response.headers['Cache-Control'] = 'no-store'
        return [{'id': str(row['id']), 'name': row['name']} for row in rows]

    @router.get('/branches/{branch_id}/telemetry/temperature/latest', response_model=TemperatureResponse)
    def latest(branch_id: UUID, response: Response, user=Depends(current_user)):
        with get_connection() as connection:
            branch = connection.execute('''SELECT b.id,b.organization_id FROM branch b
                JOIN organization o ON o.id=b.organization_id
                WHERE b.id=%s AND b.is_active AND o.is_active''', (branch_id,)).fetchone()
        if not branch:
            raise HTTPException(404, 'Sucursal no encontrada o inactiva')
        if not can_read_branch(user, branch):
            raise HTTPException(403, 'No tienes acceso a la telemetría de esta sucursal')
        response.headers['Cache-Control'] = 'no-store'
        return read_temperature(InfluxSettings.from_env(), branch_id)

    return router
