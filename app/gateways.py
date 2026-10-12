"""Canal saliente de gateways; las órdenes y conexiones viven en memoria."""
import asyncio
from dataclasses import dataclass
import json
import re
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from .security_tokens import token_hash


@dataclass(frozen=True)
class Gateway:
    branch_id: UUID


def load_gateways(raw: str) -> dict[str, Gateway]:
    """IDs y sucursales fijos; el token de sucursal se valida contra PostgreSQL."""
    try:
        entries = json.loads(raw or '{}')
        if not isinstance(entries, dict):
            raise ValueError
        result = {}
        for identifier, entry in entries.items():
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', identifier) or not isinstance(entry, dict):
                raise ValueError
            # Older JSON may contain 'token'; it is ignored and never trusted.
            result[identifier] = Gateway(branch_id=UUID(entry['branch_id']))
        return result
    except (ValueError, TypeError, KeyError, AttributeError):
        # Do not include raw JSON or tokens in startup errors.
        raise ValueError('SELFIQ_GATEWAYS debe contener IDs y branch_id UUID') from None


class GatewayCommand(BaseModel):
    command: Literal['tcp.enable', 'tcp.disable', 'status']


class GatewayHub:
    def __init__(self, gateways, timeout=60, validate_token=None):
        self.gateways, self.timeout = gateways, timeout
        self.validate_token = validate_token
        self.sessions = {}

    async def listen(self, ws: WebSocket, identifier: str):
        gateway = self.gateways.get(identifier)
        header = ws.headers.get('authorization', '')
        raw_token = header[7:] if header.startswith('Bearer ') else ''
        digest = token_hash(raw_token)
        if not gateway or not 1 <= len(raw_token) <= 255 or self.validate_token is None:
            await ws.close(code=1008)
            return
        try:
            valid = await asyncio.to_thread(self.validate_token, gateway.branch_id, digest)
        except Exception:
            await ws.close(code=1013)
            return
        if not valid:
            await ws.close(code=1008)
            return
        await ws.accept()
        session = {'ws': ws, 'pending': {}, 'lock': asyncio.Lock(),
                   'branch_id': gateway.branch_id, 'token_hash': digest}
        previous = self.sessions.get(identifier)
        self.sessions[identifier] = session
        try:
            if previous:
                await previous['ws'].close(code=1012)
            while True:
                raw = await ws.receive_text()
                if len(raw.encode()) > 8192:
                    await ws.close(code=1009)
                    break
                try:
                    message = json.loads(raw)
                except ValueError:
                    await ws.close(code=1008)
                    break
                if not isinstance(message, dict):
                    await ws.close(code=1008)
                    break
                message_id = message.get('id')
                future = session['pending'].get(message_id) if isinstance(message_id, str) else None
                if message.get('type') == 'result' and future and not future.done():
                    if not isinstance(message.get('ok'), bool):
                        await ws.close(code=1008)
                        break
                    future.set_result(message)
        except WebSocketDisconnect:
            pass
        finally:
            if self.sessions.get(identifier) is session:
                self.sessions.pop(identifier, None)
            for future in session['pending'].values():
                if not future.done():
                    future.set_result(None)

    async def send(self, identifier, command):
        session = self.sessions.get(identifier)
        if session is None:
            raise HTTPException(503, 'Raspberry desconectada; la orden no se encola')
        # Recheck expiry/revocation even when the WebSocket was opened earlier.
        try:
            valid = await asyncio.to_thread(self.validate_token, session['branch_id'], session['token_hash'])
        except Exception:
            raise HTTPException(503, 'No se pudo comprobar el token de sucursal') from None
        if not valid:
            await session['ws'].close(code=1008)
            raise HTTPException(503, 'Token de sucursal revocado o expirado; vuelva a configurar la Raspberry')
        if self.sessions.get(identifier) is not session:
            raise HTTPException(503, 'Cambió la conexión; consulte estado antes de repetir')
        if len(session['pending']) >= 16:
            raise HTTPException(429, 'Demasiadas órdenes pendientes')
        message_id = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        session['pending'][message_id] = future
        try:
            async with session['lock']:
                await session['ws'].send_json({'type': 'command', 'id': message_id, 'command': command})
            response = await asyncio.wait_for(future, self.timeout)
            if response is None:
                raise HTTPException(503, 'Conexión perdida; consulte estado antes de repetir')
            return response
        except asyncio.TimeoutError:
            raise HTTPException(504, 'Sin confirmación; consulte estado antes de repetir')
        except (RuntimeError, OSError, WebSocketDisconnect):
            raise HTTPException(503, 'No pudo enviarse; consulte estado antes de repetir')
        finally:
            session['pending'].pop(message_id, None)

    async def close(self):
        for session in tuple(self.sessions.values()):
            try:
                await session['ws'].close(code=1001)
            except (RuntimeError, OSError):
                pass

    async def disconnect_branch(self, branch_id):
        for session in tuple(self.sessions.values()):
            if session['branch_id'] == branch_id:
                try:
                    await session['ws'].close(code=1008)
                except (RuntimeError, OSError):
                    pass


def create_router(hub, current_user, get_connection):
    router = APIRouter(tags=['Gateways'])

    def authorized(gateway_id: str, user: dict = Depends(current_user)):
        from .registration_tokens import require_branch_access
        gateway=hub.gateways.get(gateway_id)
        if gateway is None:
            raise HTTPException(404,'Gateway no configurado')
        with get_connection() as connection:
            branch=connection.execute('''SELECT b.id,b.organization_id FROM branch b JOIN organization o ON o.id=b.organization_id
                WHERE b.id=%s AND b.is_active AND o.is_active''',(gateway.branch_id,)).fetchone()
        if not branch:
            raise HTTPException(404,'Sucursal inexistente o inactiva')
        require_branch_access(user,gateway.branch_id,branch['organization_id'])
        return gateway

    @router.websocket('/ws/gateways/{gateway_id}')
    async def link(ws: WebSocket, gateway_id: str):
        await hub.listen(ws, gateway_id)

    @router.get('/gateways/{gateway_id}')
    async def state(gateway_id: str, gateway: Gateway = Depends(authorized)):
        return {'gateway_id': gateway_id, 'branch_id': str(gateway.branch_id),
                'connected': gateway_id in hub.sessions}

    @router.post('/gateways/{gateway_id}/commands')
    async def command(gateway_id: str, body: GatewayCommand, gateway: Gateway = Depends(authorized)):
        return await hub.send(gateway_id, body.command)

    return router