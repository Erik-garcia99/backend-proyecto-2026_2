"""Canal saliente de gateways; las órdenes y conexiones viven en memoria."""
import asyncio
import json
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from .security_tokens import token_hash
from .gateway_registry import enroll, verify_proof, active_token
import secrets


class GatewayCommand(BaseModel):
    command: Literal['tcp.enable', 'tcp.disable', 'status']


class GatewayTokenRequest(BaseModel):
    branch_token: str


class GatewayHub:
    def __init__(self, get_connection, timeout=60, validate_token=None):
        self.get_connection, self.timeout = get_connection, timeout
        self.validate_token = validate_token
        self.sessions = {}
        self.heartbeat_seconds = 30

    async def listen(self, ws: WebSocket, identifier: str):
        await ws.accept()
        nonce = secrets.token_urlsafe(32)
        await ws.send_json({'type': 'challenge', 'nonce': nonce})
        try:
            raw = await asyncio.wait_for(ws.receive_text(), 10)
            if len(raw.encode()) > 4096:
                raise ValueError('Mensaje demasiado grande')
            proof = json.loads(raw)
            public_key = proof['public_key']
            raw_token = proof['branch_token']
            if not isinstance(raw_token, str) or not 1 <= len(raw_token) <= 255:
                raise ValueError('Token incorrecto')
            verify_proof(identifier, nonce, public_key, proof['signature'])
            gateway = await asyncio.to_thread(enroll, self.get_connection, identifier, public_key, raw_token)
        except HTTPException as exc:
            await ws.send_json({'type': 'auth_status', 'status': 'registration_rejected', 'message': exc.detail})
            await ws.close(code=1008)
            return
        except (ValueError, TypeError, KeyError, asyncio.TimeoutError):
            await ws.close(code=1008)
            return
        except WebSocketDisconnect:
            return
        except Exception:
            await ws.send_json({'type': 'auth_status', 'status': 'backend_unavailable'})
            await ws.close(code=1013)
            return
        session = {'ws': ws, 'pending': {}, 'lock': asyncio.Lock(),
                   'branch_id': gateway['branch_id'], 'token_hash': gateway['token_hash'],
                   'authorized': gateway['token_valid']}
        previous = self.sessions.get(identifier)
        self.sessions[identifier] = session
        try:
            if previous:
                await previous['ws'].close(code=1012)
            await ws.send_json({'type': 'auth_status',
                'status': 'registered' if session['authorized'] else 'token_invalid',
                'gateway_id': identifier, 'branch_id': str(session['branch_id'])})
            while True:
                try:
                    raw = await asyncio.wait_for(ws.receive_text(), self.heartbeat_seconds)
                except asyncio.TimeoutError:
                    try:
                        session['authorized'] = await asyncio.to_thread(
                            self.validate_token, session['branch_id'], session['token_hash'])
                        status = 'registered' if session['authorized'] else 'token_invalid'
                    except Exception:
                        status = 'backend_unavailable'
                    await ws.send_json({'type':'auth_status','status':status,
                                        'gateway_id':identifier,'branch_id':str(session['branch_id'])})
                    continue
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

    async def send(self, identifier, command, branch_token=None, payload=None):
        session = self.sessions.get(identifier)
        if session is None:
            raise HTTPException(503, 'Raspberry desconectada; la orden no se encola')
        if command != 'gateway.token':
            try:
                valid = session['authorized'] and await asyncio.to_thread(
                    self.validate_token, session['branch_id'], session['token_hash'])
            except Exception:
                raise HTTPException(503, 'No se pudo comprobar el token de sucursal') from None
            if not valid:
                session['authorized'] = False
                raise HTTPException(409, 'Token de sucursal inválido; actualícelo sin cambiar Wi-Fi')
        if self.sessions.get(identifier) is not session:
            raise HTTPException(503, 'Cambió la conexión; consulte estado antes de repetir')
        if len(session['pending']) >= 16:
            raise HTTPException(429, 'Demasiadas órdenes pendientes')
        message_id = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        session['pending'][message_id] = future
        try:
            async with session['lock']:
                message = {'type': 'command', 'id': message_id, 'command': command}
                if command == 'gateway.token':
                    message['branch_token'] = branch_token
                elif isinstance(payload, dict):
                    message.update(payload)
                await session['ws'].send_json(message)
            response = await asyncio.wait_for(future, self.timeout)
            if response is None:
                raise HTTPException(503, 'Conexión perdida; consulte estado antes de repetir')
            if command == 'gateway.token' and response.get('ok') is True:
                try:
                    await session['ws'].close(code=1012)
                except (RuntimeError, OSError, WebSocketDisconnect):
                    pass
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
        with get_connection() as connection:
            gateway = connection.execute('''SELECT g.*,b.organization_id FROM gateway g
                JOIN branch b ON b.id=g.branch_id JOIN organization o ON o.id=b.organization_id
                WHERE g.rpi_identifier=%s AND g.is_active AND b.is_active AND o.is_active''',
                (gateway_id,)).fetchone()
        if not gateway:
            raise HTTPException(404, 'Gateway no registrado o inactivo')
        require_branch_access(user, gateway['branch_id'], gateway['organization_id'])
        return gateway

    @router.websocket('/ws/gateways/{gateway_id}')
    async def link(ws: WebSocket, gateway_id: str):
        await hub.listen(ws, gateway_id)

    def gateway_state(gateway_id, gateway):
        session = hub.sessions.get(gateway_id)
        with get_connection() as connection:
            valid = connection.execute('''SELECT 1 FROM organization_token WHERE id=%s AND is_active
                AND revoked_at IS NULL AND 'register_gateway'=ANY(scopes)
                AND (expires_at IS NULL OR expires_at>NOW())''', (gateway['registered_with_token'],)).fetchone()
        token_valid = bool(valid) and (session is None or session['authorized'])
        status = ('registered' if token_valid else 'token_invalid') if gateway['device_public_key'] else 'not_enrolled'
        return {'gateway_id': gateway_id, 'branch_id': str(gateway['branch_id']),
                'name': gateway.get('hostname') or gateway_id,
                'connected': session is not None and token_valid,
                'recovery_available': session is not None,
                'registration_status': status,
                'alert': 'Actualice el token de sucursal' if status == 'token_invalid' else None}

    @router.get('/gateways')
    def list_gateways(user: dict = Depends(current_user)):
        from .registration_tokens import require_branch_access
        with get_connection() as connection:
            rows = connection.execute('''SELECT g.*,b.organization_id FROM gateway g
                JOIN branch b ON b.id=g.branch_id JOIN organization o ON o.id=b.organization_id
                WHERE g.is_active AND b.is_active AND o.is_active ORDER BY g.created_at''').fetchall()
        result = []
        for gateway in rows:
            try:
                require_branch_access(user, gateway['branch_id'], gateway['organization_id'])
            except HTTPException:
                continue
            result.append(gateway_state(gateway['rpi_identifier'], gateway))
        return result

    @router.get('/gateways/{gateway_id}')
    def state(gateway_id: str, gateway=Depends(authorized)):
        return gateway_state(gateway_id, gateway)

    @router.post('/gateways/{gateway_id}/token')
    async def replace_token(gateway_id: str, body: GatewayTokenRequest, gateway=Depends(authorized)):
        if not 1 <= len(body.branch_token) <= 255:
            raise HTTPException(422, 'Token incorrecto')
        with get_connection() as connection:
            if not active_token(connection, token_hash(body.branch_token), gateway['branch_id']):
                raise HTTPException(400, 'El token no es vigente o pertenece a otra sucursal')
        return await hub.send(gateway_id, 'gateway.token', body.branch_token)

    @router.post('/gateways/{gateway_id}/commands')
    async def command(gateway_id: str, body: GatewayCommand, gateway=Depends(authorized)):
        return await hub.send(gateway_id, body.command)

    return router