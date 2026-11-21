import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
import jwt
from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg.errors import UniqueViolation
from . import db
from .physical_config import create_router as physical_config_router
from .roi_classification import create_router as roi_classification_router
from .alerts import create_router as alerts_router, monitor as alerts_monitor
from .visual_alerts import monitor as visual_alerts_monitor
from .calibrations import CalibrationError, create_router as calibrations_router
from .gateways import GatewayHub, create_router
from .devices import create_router as devices_router, close_discoveries
from .guide import create_router as guide_router, create_gateway_router as guide_gateway_router
from .rois import create_router as rois_router
from .pos import PosError, create_router as pos_router, error_response as pos_error_response
from .node_registration import make_router as node_router
from .telemetry import (create_router as telemetry_router, read_occupancy, InfluxSettings, replace)
from .registration_tokens import create_router as token_router, validate_branch_token, require_branch_access
from .permissions import require_actor, require_branch_actor, CUSTOMER, is_superuser, role
from .schemas import (BranchCreateRequest, BranchCreatedResponse, CustomerBranchItem, CustomerBranchesResponse,
                      CustomerCategoriesResponse, CustomerHomeResponse, CustomerProductItem, CustomerProductsResponse,
                      CustomerRegisterRequest, CustomerShelfItem, CustomerShelvesResponse,
                      LoginRequest,
                      LoginResponse, LogoutRequest, MessageResponse, NotificationPreferences,
                      NotificationPreferencesUpdate, PasswordChangeRequest, PendingUserResponse,
                      ProfileUpdateRequest, RefreshRequest, RefreshResponse,
                      RegisterRequest, RegisterResponse, UserResponse)
from .config import CUSTOMER_ORGANIZATION_SLUG, ACCESS_TOKEN_EXPIRE_MINUTES, REFRESH_TOKEN_EXPIRE_DAYS
from .security import (create_access_token, decode_access_token, hash_password,
                       hash_registration_token, verify_password, generate_registration_token,
                       generate_refresh_token, hash_refresh_token, needs_rehash)

# Roles con alcance de toda la organización (coherente con require_branch_access).
ORG_WIDE_ROLES = frozenset({'owner', 'gerencia'})

gateway_hub = GatewayHub(db.get_connection,
    validate_token=lambda branch_id, digest: validate_branch_token(db.get_connection, branch_id, digest))

@asynccontextmanager
async def lifespan(_):
    db.pool.open()
    try:
        await asyncio.to_thread(db.pool.wait, 30)
        stop_alerts = asyncio.Event()
        alerts_task = asyncio.create_task(alerts_monitor(db.get_connection, stop_alerts))
        stop_visual = asyncio.Event()
        visual_task = asyncio.create_task(
            visual_alerts_monitor(db.get_connection, stop_visual))
        try:
            yield
        finally:
            stop_alerts.set()
            stop_visual.set()
            await alerts_task
            await visual_task
    finally:
        await close_discoveries()
        await gateway_hub.close()
        db.pool.close()

app = FastAPI(title='SelfIQ API', version='2.0.0', lifespan=lifespan)
app.state.gateway_hub = gateway_hub
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_credentials=False,
                   allow_methods=['*'], allow_headers=['*'],
                   expose_headers=['X-Captured-At'])

@app.exception_handler(PosError)
async def pos_error_handler(_, exc):
    return pos_error_response(exc)

bearer = HTTPBearer(auto_error=False)

def user_response(user):
    state = 'PENDING_APPROVAL' if user['account_status']=='pending' else user['account_status'].upper()
    return UserResponse(id=str(user['id']), email=user['email'], first_name=user['first_name'],
        last_name=user['last_name'], phone_number=user['phone_number'],
        is_superuser=user['role'].lower() == 'sudo', account_status=state,
        role=user['role'], organization_id=str(user['organization_id']),
        branch_id=str(user['branch_id']) if user['branch_id'] else None)

def customer_organization(connection, requested_slug, requested_code=None):
    """Resuelve la organización del cliente sin atarlo a una sucursal.

    Orden: código de organización del cliente -> slug enviado -> CUSTOMER_ORGANIZATION_SLUG
    -> única organización activa. Con varias organizaciones y sin código/slug, se rechaza.
    """
    code = (requested_code or '').strip()
    if code:
        row = connection.execute(
            'SELECT id FROM organization WHERE customer_code=%s AND is_active', (code,)).fetchone()
        if not row:
            raise HTTPException(400, 'El código de organización no existe o está inactivo')
        return row['id']
    slug = (requested_slug or CUSTOMER_ORGANIZATION_SLUG or '').strip()
    if slug:
        row = connection.execute('SELECT id FROM organization WHERE slug=%s AND is_active', (slug,)).fetchone()
        if not row:
            raise HTTPException(400, 'La organización indicada no existe o está inactiva')
        return row['id']
    rows = connection.execute('SELECT id FROM organization WHERE is_active ORDER BY created_at LIMIT 2').fetchall()
    if len(rows) == 1:
        return rows[0]['id']
    if not rows:
        raise HTTPException(503, 'No hay una organización activa para registrar clientes')
    raise HTTPException(400, 'El registro de clientes requiere indicar organization_code u organization_slug')

@app.get('/health')
def health():
    return {'status': 'ok'}

@app.post('/auth/register', response_model=RegisterResponse, status_code=201)
def register(request: RegisterRequest):
    with db.get_connection() as connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute('''SELECT t.id, t.branch_id, t.organization_id FROM organization_token t
                    JOIN branch b ON b.id=t.branch_id AND b.organization_id=t.organization_id
                    JOIN organization o ON o.id=b.organization_id
                    WHERE t.token_hash=%s AND t.is_active AND t.revoked_at IS NULL
                      AND b.is_active AND o.is_active AND 'register_user'=ANY(t.scopes)
                      AND (t.expires_at IS NULL OR t.expires_at>NOW())
                      AND (t.max_uses IS NULL OR t.use_count<t.max_uses) FOR UPDATE OF t''',
                    (hash_registration_token(request.registration_token),))
                token=cursor.fetchone()
                if not token: raise HTTPException(400,'Token de sucursal inválido o expirado')
                cursor.execute('''INSERT INTO "user" (organization_id,branch_id,email,first_name,last_name,
                    phone_number,password_hash,role,account_status)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,'employee','pending') RETURNING id''',
                    (token['organization_id'],token['branch_id'],request.email.lower(),request.first_name,
                     request.last_name,request.phone,hash_password(request.password)))
                user=cursor.fetchone()
                cursor.execute('UPDATE organization_token SET use_count=use_count+1,last_used_at=NOW() WHERE id=%s',(token['id'],))
            connection.commit()
        except UniqueViolation:
            connection.rollback()
            raise HTTPException(409,'El correo ya está registrado')
    return RegisterResponse(message='Cuenta creada; queda pendiente de aprobación',user_id=str(user['id']),account_status='PENDING_APPROVAL')

@app.post('/auth/customer-register', response_model=RegisterResponse, status_code=201)
def customer_register(request: CustomerRegisterRequest):
    """Autorregistro de cliente: activo de inmediato y sin sucursal fija.

    El cliente queda ligado a la organización (para poder listar sus sucursales
    después), pero `branch_id` se guarda NULL: no pertenece a una tienda.
    """
    with db.get_connection() as connection:
        organization_id = customer_organization(connection, request.organization_slug, request.organization_code)
        try:
            user = connection.execute('''INSERT INTO "user" (organization_id,branch_id,email,first_name,last_name,
                phone_number,password_hash,role,account_status)
                VALUES(%s,NULL,%s,%s,%s,%s,%s,'customer','active') RETURNING id''',
                (organization_id,request.email.lower(),request.first_name,request.last_name,
                 request.phone,hash_password(request.password))).fetchone()
            connection.commit()
        except UniqueViolation:
            connection.rollback()
            raise HTTPException(409,'El correo ya está registrado')
    return RegisterResponse(message='Cuenta de cliente creada; ya puedes iniciar sesión',
                            user_id=str(user['id']),account_status='ACTIVE')

@app.post('/auth/login', response_model=LoginResponse)
def login(request: LoginRequest):
    with db.get_connection() as connection:
        user=connection.execute('SELECT * FROM "user" WHERE lower(email)=%s',(request.email.lower(),)).fetchone()
    if not user or not verify_password(request.password,user['password_hash']):
        raise HTTPException(401,'Credenciales incorrectas')
    if user['deactivated_at'] is not None or user['account_status']!='active':
        raise HTTPException(403,'La cuenta no está activa o aprobada')
    # Rehash transparente bcrypt → Argon2id (D-C15).
    if needs_rehash(user['password_hash']):
        new_hash = hash_password(request.password)
        with db.get_connection() as connection:
            connection.execute('UPDATE "user" SET password_hash=%s, updated_at=NOW() WHERE id=%s',
                (new_hash, user['id']))
            connection.commit()
    # Crear sesión con refresh token (D-C3).
    refresh_token = generate_refresh_token()
    expires_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    with db.get_connection() as connection:
        connection.execute('''INSERT INTO auth_session (user_id, refresh_token_hash, platform,
            device_label, expires_at) VALUES (%s, %s, %s, %s, %s)''',
            (user['id'], hash_refresh_token(refresh_token), None, None, expires_at))
        connection.commit()
    return LoginResponse(access_token=create_access_token(str(user['id'])),
                         refresh_token=refresh_token,
                         expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
                         user=user_response(user))

@app.post('/auth/refresh', response_model=RefreshResponse)
def refresh(request: RefreshRequest):
    """Emite un nuevo access_token si el refresh_token es válido y no revocado."""
    digest = hash_refresh_token(request.refresh_token)
    with db.get_connection() as connection:
        session = connection.execute('''SELECT s.id, s.user_id, s.expires_at, s.revoked_at
            FROM auth_session s WHERE s.refresh_token_hash=%s FOR UPDATE''', (digest,)).fetchone()
        if not session:
            raise HTTPException(401, 'Refresh token inválido')
        if session['revoked_at'] is not None:
            raise HTTPException(401, 'La sesión fue cerrada')
        if session['expires_at'] < datetime.now(timezone.utc):
            raise HTTPException(401, 'El refresh token expiró; inicia sesión de nuevo')
        user = connection.execute('''SELECT * FROM "user" WHERE id=%s AND account_status='active'
            AND deactivated_at IS NULL''', (session['user_id'],)).fetchone()
        if not user:
            raise HTTPException(403, 'La cuenta no está activa')
        connection.execute('UPDATE auth_session SET last_used_at=NOW() WHERE id=%s', (session['id'],))
        connection.commit()
    return RefreshResponse(access_token=create_access_token(str(user['id'])),
                           expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60)

def current_user(credentials: HTTPAuthorizationCredentials=Depends(bearer)):
    if not credentials:raise HTTPException(401,'Se requiere un token Bearer')
    try: user_id=UUID(decode_access_token(credentials.credentials)['sub'])
    except (jwt.InvalidTokenError,KeyError,ValueError):raise HTTPException(401,'Token inválido o expirado')
    with db.get_connection() as connection:
        user=connection.execute('SELECT *, (lower(role)=\'sudo\') AS is_superuser FROM "user" WHERE id=%s AND deactivated_at IS NULL',(user_id,)).fetchone()
    if not user or user['account_status']!='active':raise HTTPException(403,'La cuenta no está activa')
    return user

@app.post('/auth/logout', response_model=MessageResponse)
def logout(request: LogoutRequest = None, user=Depends(current_user)):
    """Cierra la sesión. Si viene refresh_token, revoca solo esa; si no, revoca todas."""
    with db.get_connection() as connection:
        if request and request.refresh_token:
            digest = hash_refresh_token(request.refresh_token)
            result = connection.execute('''UPDATE auth_session SET revoked_at=NOW()
                WHERE user_id=%s AND refresh_token_hash=%s AND revoked_at IS NULL
                RETURNING id''', (user['id'], digest))
            if not result.fetchone():
                raise HTTPException(404, 'Sesión no encontrada o ya cerrada')
        else:
            connection.execute('''UPDATE auth_session SET revoked_at=NOW()
                WHERE user_id=%s AND revoked_at IS NULL''', (user['id'],))
        connection.commit()
    return MessageResponse(message='Sesión cerrada')

@app.get('/auth/me',response_model=UserResponse)
def me(user=Depends(current_user)):
    return user_response(user)

PROFILE_FIELDS={'first_name':'El nombre','last_name':'El apellido','email':'El correo'}

@app.patch('/auth/me',response_model=UserResponse)
def update_me(request:ProfileUpdateRequest,user=Depends(current_user)):
    changes=request.model_dump(exclude_unset=True)
    for field,label in PROFILE_FIELDS.items():
        if field in changes and changes[field] is None:
            raise HTTPException(422,f'{label} no puede estar vacío')
    if 'email' in changes:changes['email']=changes['email'].lower()
    if 'phone_number' in changes:changes['phone_number']=changes['phone_number'] or None
    if not changes:return user_response(user)
    assignments=', '.join(f'{field}=%s' for field in changes)
    try:
        with db.get_connection() as connection:
            updated=connection.execute(f'UPDATE "user" SET {assignments}, updated_at=NOW() WHERE id=%s RETURNING *',
                (*changes.values(),user['id'])).fetchone()
            connection.commit()
    except UniqueViolation:
        raise HTTPException(409,'El correo ya está registrado')
    if not updated:raise HTTPException(404,'Usuario no encontrado')
    return user_response(updated)

@app.post('/auth/change-password',response_model=MessageResponse)
def change_password(request:PasswordChangeRequest,user=Depends(current_user)):
    if not verify_password(request.current_password,user['password_hash']):
        raise HTTPException(400,'La contraseña actual no es correcta')
    if request.new_password==request.current_password:
        raise HTTPException(400,'La nueva contraseña debe ser distinta de la actual')
    with db.get_connection() as connection:
        connection.execute('UPDATE "user" SET password_hash=%s, updated_at=NOW() WHERE id=%s',
            (hash_password(request.new_password),user['id']))
        connection.commit()
    return MessageResponse(message='Contraseña actualizada')

@app.post('/organizations/{organization_id}/customer-code', response_model=MessageResponse)
def set_customer_code(organization_id: UUID, user=Depends(current_user)):
    """Genera/asigna un código simple de organización para registro de clientes.

    Solo owner/admin de la organización (o SUDO) pueden hacerlo.
    """
    if not (is_superuser(user) or (role(user) == 'owner' and str(user.get('organization_id')) == str(organization_id))):
        raise HTTPException(403, 'No tienes permisos para esta acción')
    code = 'ORG-' + uuid4().hex[:6].upper()
    with db.get_connection() as connection:
        org = connection.execute('SELECT id FROM organization WHERE id=%s AND is_active', (organization_id,)).fetchone()
        if not org:
            raise HTTPException(404, 'Organización no encontrada o inactiva')
        # Ensure uniqueness (retry a few times just in case)
        for _ in range(5):
            exists = connection.execute('SELECT 1 FROM organization WHERE customer_code=%s', (code,)).fetchone()
            if not exists:
                break
            code = 'ORG-' + uuid4().hex[:6].upper()
        connection.execute('UPDATE organization SET customer_code=%s WHERE id=%s', (code, organization_id))
        connection.commit()
    return MessageResponse(message=f'Código de organización generado: {code}')

@app.get('/auth/pending', response_model=list[PendingUserResponse])
def pending_users(user=Depends(current_user)):
    role = user['role'].lower()
    if not (user['is_superuser'] or role in {'owner', 'rh'}):
        raise HTTPException(403, 'No tienes permisos para aprobar usuarios')
    with db.get_connection() as connection:
        if user['is_superuser']:
            rows = connection.execute('''SELECT * FROM "user" WHERE account_status='pending' AND deactivated_at IS NULL
                ORDER BY created_at DESC''').fetchall()
        elif role in ORG_WIDE_ROLES:
            rows = connection.execute('''SELECT * FROM "user" WHERE account_status='pending' AND deactivated_at IS NULL
                AND organization_id=%s ORDER BY created_at DESC''', (user['organization_id'],)).fetchall()
        else:
            rows = connection.execute('''SELECT * FROM "user" WHERE account_status='pending' AND deactivated_at IS NULL
                AND organization_id=%s AND branch_id=%s ORDER BY created_at DESC''',
                (user['organization_id'], user['branch_id'])).fetchall()
    return [PendingUserResponse(id=str(r['id']), email=r['email'], first_name=r['first_name'],
        last_name=r['last_name'], phone_number=r['phone_number'], role=r['role'],
        account_status=r['account_status'], organization_id=str(r['organization_id']),
        branch_id=str(r['branch_id']) if r['branch_id'] else None, created_at=r['created_at'])
        for r in rows]

@app.post('/auth/reject/{user_id}', response_model=MessageResponse)
def reject_user(user_id: UUID, approver=Depends(current_user)):
    if not (approver['is_superuser'] or approver['role'].lower() in {'owner', 'rh'}):
        raise HTTPException(403, 'No tienes permisos para rechazar usuarios')
    with db.get_connection() as connection:
        target = connection.execute('''SELECT * FROM "user" WHERE id=%s AND account_status='pending'
            AND deactivated_at IS NULL FOR UPDATE''', (user_id,)).fetchone()
        if not target:
            raise HTTPException(404, 'Cuenta pendiente no encontrada')
        require_branch_access(approver, target['branch_id'], target['organization_id'])
        connection.execute('''UPDATE "user" SET account_status='rejected', deactivated_at=NOW(),
            updated_at=NOW() WHERE id=%s''', (user_id,))
        connection.commit()
    return MessageResponse(message='Cuenta rechazada')

@app.get('/auth/me/preferences',response_model=NotificationPreferences)
def get_preferences(user=Depends(current_user)):
    with db.get_connection() as connection:
        row=connection.execute('SELECT low_stock, capacity, temperature, offline, sound FROM user_preference WHERE user_id=%s',
            (user['id'],)).fetchone()
    return NotificationPreferences(**(row or {}))

@app.put('/auth/me/preferences',response_model=NotificationPreferences)
def update_preferences(request:NotificationPreferencesUpdate,user=Depends(current_user)):
    with db.get_connection() as connection:
        row=connection.execute('''INSERT INTO user_preference(user_id,low_stock,capacity,temperature,offline,sound)
            VALUES(%s,%s,%s,%s,%s,%s)
            ON CONFLICT(user_id) DO UPDATE SET low_stock=EXCLUDED.low_stock,capacity=EXCLUDED.capacity,
                temperature=EXCLUDED.temperature,offline=EXCLUDED.offline,sound=EXCLUDED.sound,updated_at=NOW()
            RETURNING low_stock, capacity, temperature, offline, sound''',
            (user['id'],request.low_stock,request.capacity,request.temperature,request.offline,request.sound)).fetchone()
        connection.commit()
    return NotificationPreferences(**row)

@app.post('/branches',response_model=BranchCreatedResponse,status_code=201)
def create_branch(request: BranchCreateRequest,response:Response,creator=Depends(current_user)):
    branch_token=generate_registration_token()
    with db.get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute('''SELECT t.id,t.business_id FROM business_registration_token t
                JOIN organization o ON o.id=t.business_id WHERE t.token_hash=%s AND t.is_active AND o.is_active
                AND (t.expires_at IS NULL OR t.expires_at>NOW())
                AND (t.max_uses IS NULL OR t.uses<t.max_uses) FOR UPDATE OF t''',(hash_registration_token(request.registration_token),))
            token=cursor.fetchone()
            if not token:raise HTTPException(400,'Token global de empresa inválido o expirado')
            if not creator['is_superuser'] and not (creator['role'].lower()=='owner' and creator['organization_id']==token['business_id']):
                raise HTTPException(403,'Se requiere SUDO o OWNER de esta empresa')
            code='SUC-'+uuid4().hex[:8].upper()
            cursor.execute('''INSERT INTO branch(organization_id,name,branch_code,address,timezone,business_category)
                VALUES(%s,%s,%s,%s,%s,%s) RETURNING *''',(token['business_id'],request.name,code,request.address,request.timezone,request.category))
            branch=cursor.fetchone()
            cursor.execute('''INSERT INTO organization_token(organization_id,branch_id,token_hash,token_prefix,created_by)
                VALUES(%s,%s,%s,%s,%s)''',(branch['organization_id'],branch['id'],hash_registration_token(branch_token),branch_token[:8],creator['id']))
            cursor.execute('UPDATE business_registration_token SET uses=uses+1 WHERE id=%s',(token['id'],))
        connection.commit()
    response.headers['Cache-Control']='no-store'
    return BranchCreatedResponse(id=str(branch['id']),name=branch['name'],code=branch['branch_code'],
        address=branch['address'],timezone=branch['timezone'],category=branch['business_category'],
        business_id=str(branch['organization_id']),branch_token=branch_token)

@app.post('/auth/approve/{user_id}',response_model=UserResponse)
def approve_user(user_id:UUID,approver=Depends(current_user)):
    if not (approver['is_superuser'] or approver['role'].lower() in {'owner', 'rh'}):
        raise HTTPException(403, 'No tienes permisos para aprobar usuarios')
    with db.get_connection() as connection:
        target=connection.execute('SELECT * FROM "user" WHERE id=%s AND account_status=\'pending\' AND deactivated_at IS NULL FOR UPDATE',(user_id,)).fetchone()
        if not target:raise HTTPException(404,'Cuenta pendiente no encontrada')
        require_branch_access(approver,target['branch_id'],target['organization_id'])
        user=connection.execute('UPDATE "user" SET account_status=\'active\',updated_at=NOW() WHERE id=%s RETURNING *',(user_id,)).fetchone()
        connection.execute('''INSERT INTO user_management(manager_id,employee_id,branch_id,assigned_role,updated_by)
            VALUES(%s,%s,%s,%s,%s) ON CONFLICT(manager_id,employee_id,branch_id)
            DO UPDATE SET is_active=TRUE,assigned_role=EXCLUDED.assigned_role,updated_by=EXCLUDED.updated_by''',
            (approver['id'],user['id'],user['branch_id'],user['role'],approver['id']))
        connection.commit()
    return user_response(user)

app.include_router(create_router(gateway_hub,current_user,db.get_connection))
app.include_router(token_router(current_user,db.get_connection,gateway_hub))
app.include_router(node_router(db.get_connection, gateway_hub, current_user))
app.include_router(devices_router(current_user, db.get_connection, gateway_hub))
app.include_router(guide_router(current_user, db.get_connection, gateway_hub))
app.include_router(guide_gateway_router(db.get_connection))
app.include_router(rois_router(current_user, db.get_connection, gateway_hub))
app.include_router(calibrations_router(current_user, db.get_connection, gateway_hub))
app.include_router(telemetry_router(current_user, db.get_connection))
app.include_router(physical_config_router(current_user, db.get_connection))
app.include_router(roi_classification_router(current_user, db.get_connection))
app.include_router(pos_router(current_user, db.get_connection))

def classify_occupancy(occupancy: int | None, status: str, max_capacity: int | None, near_pct: float = 80.0) -> str:
    if occupancy is None or status != 'ok':
        return 'no_data'
    if max_capacity and max_capacity > 0:
        ratio = (occupancy / max_capacity) * 100.0
        if occupancy >= max_capacity or ratio >= 100.0:
            return 'full'
        if ratio >= near_pct:
            return 'near'
        return 'available'
    if occupancy >= 100:
        return 'full'
    if occupancy >= near_pct:
        return 'near'
    return 'available'


@app.get('/customer/branches', response_model=CustomerBranchesResponse)
def customer_branches(page: int = Query(1, ge=1),
                      page_size: int = Query(5, ge=1, le=5),
                      q: str | None = Query(None, max_length=100),
                      user=Depends(current_user)):
    require_actor(user, CUSTOMER)
    org_id = user.get('organization_id')
    if not org_id:
        raise HTTPException(403, 'No tienes una organización asignada')

    with db.get_connection() as connection:
        org = connection.execute(
            'SELECT id FROM organization WHERE id=%s AND is_active=TRUE',
            (org_id,)).fetchone()
        if not org:
            raise HTTPException(403, 'Organización inactiva o no encontrada')

        params = [org_id]
        where_clauses = ['b.organization_id = %s', 'b.is_active = TRUE']

        if q and q.strip():
            where_clauses.append('(b.name ILIKE %s OR b.address ILIKE %s)')
            term = f'%{q.strip()}%'
            params.extend([term, term])

        where_sql = ' AND '.join(where_clauses)

        total_row = connection.execute(
            f'SELECT COUNT(*) as total FROM branch b WHERE {where_sql}',
            tuple(params)
        ).fetchone()
        total = int(total_row['total']) if total_row else 0

        total_pages = (total + page_size - 1) // page_size if total > 0 else 1
        offset = (page - 1) * page_size

        query_params = list(params)
        query_params.extend([page_size, offset])

        rows = connection.execute(
            f'''SELECT b.id, b.name, b.address, b.business_category, b.max_capacity_people
                FROM branch b
                WHERE {where_sql}
                ORDER BY b.name ASC
                LIMIT %s OFFSET %s''',
            tuple(query_params)
        ).fetchall()

    items = [
        CustomerBranchItem(
            id=str(r['id']),
            name=r['name'],
            address=r.get('address'),
            business_category=r.get('business_category'),
            max_capacity_people=r.get('max_capacity_people'),
        )
        for r in rows
    ]
    return CustomerBranchesResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages
    )


def map_customer_availability(roi_status: str | None) -> tuple[str, str]:
    if not roi_status or roi_status in ('unknown', 'no_data', 'stale'):
        return 'no_data', 'Sin información reciente'
    if roi_status in ('adequate', 'medium'):
        return 'available', 'Disponible en repisa'
    if roi_status == 'low':
        return 'low', 'Disponibilidad baja'
    if roi_status in ('empty', 'out_of_stock'):
        return 'out_of_stock', 'Agotado en repisa'
    return 'no_data', 'Sin información reciente'


@app.get('/customer/branches/{branch_id}/categories', response_model=CustomerCategoriesResponse)
def customer_categories(branch_id: UUID, user=Depends(current_user)):
    with db.get_connection() as connection:
        branch = require_branch_actor(connection, branch_id, user, customer=True, staff=True, admin=True)
        rows = connection.execute(
            '''SELECT DISTINCT c.name FROM product_category c
               JOIN product p ON p.category_id=c.id
               WHERE p.organization_id=%s AND p.is_active=TRUE
               ORDER BY c.name ASC''',
            (branch['organization_id'],)
        ).fetchall()
    return CustomerCategoriesResponse(categories=[r['name'] for r in rows if r.get('name')])


@app.get('/customer/branches/{branch_id}/products', response_model=CustomerProductsResponse)
def customer_products(branch_id: UUID,
                      q: str | None = Query(None, max_length=100),
                      category: str | None = Query(None, max_length=100),
                      page: int = Query(1, ge=1),
                      page_size: int = Query(20, ge=1, le=50),
                      user=Depends(current_user)):
    with db.get_connection() as connection:
        branch = require_branch_actor(connection, branch_id, user, customer=True, staff=True, admin=True)
        org_id = branch['organization_id']

        params = [org_id]
        where_clauses = ['p.organization_id = %s', 'p.is_active = TRUE']

        if q and q.strip():
            term = f'%{q.strip()}%'
            where_clauses.append('(p.name ILIKE %s OR p.sku ILIKE %s OR p.barcode ILIKE %s OR p.description ILIKE %s)')
            params.extend([term, term, term, term])

        if category and category.strip():
            where_clauses.append('c.name = %s')
            params.append(category.strip())

        where_sql = ' AND '.join(where_clauses)

        count_sql = f'''SELECT COUNT(*) as total FROM product p
                        LEFT JOIN product_category c ON c.id=p.category_id
                        WHERE {where_sql}'''
        total_row = connection.execute(count_sql, tuple(params)).fetchone()
        total = int(total_row['total']) if total_row else 0

        total_pages = (total + page_size - 1) // page_size if total > 0 else 1
        offset = (page - 1) * page_size

        query_params = [branch_id, branch_id, branch_id]
        query_params.extend(params)
        query_params.extend([page_size, offset])

        query_sql = f'''
            SELECT p.id, p.name, p.sku, p.barcode, p.description, p.sale_price,
                   c.name AS category_name,
                   COALESCE(sh_guide.id, sh_roi.id) AS shelf_id,
                   COALESCE(sh_guide.name, sh_roi.name) AS shelf_name,
                   COALESCE(sh_guide.location_label, sh_roi.location_label) AS location_label,
                   CASE WHEN gpl.id IS NOT NULL THEN TRUE ELSE FALSE END AS has_guide,
                   rs.status AS roi_status
            FROM product p
            LEFT JOIN product_category c ON c.id = p.category_id
            LEFT JOIN guide_product_light gpl ON gpl.product_id = p.id AND gpl.branch_id = %s
            LEFT JOIN led_guide_point lgp ON lgp.id = gpl.guide_point_id
            LEFT JOIN shelf sh_guide ON sh_guide.id = lgp.shelf_id AND sh_guide.is_active = TRUE
            LEFT JOIN LATERAL (
                SELECT s.id, s.name, s.location_label
                FROM roi r
                JOIN shelf s ON s.id = r.shelf_id AND s.branch_id = %s AND s.is_active = TRUE
                WHERE r.product_id = p.id AND r.is_active = TRUE
                ORDER BY r.updated_at DESC
                LIMIT 1
            ) sh_roi ON TRUE
            LEFT JOIN LATERAL (
                SELECT rs_sub.status
                FROM roi r_sub
                JOIN shelf s_sub ON s_sub.id = r_sub.shelf_id AND s_sub.branch_id = %s AND s_sub.is_active = TRUE
                LEFT JOIN roi_state rs_sub ON rs_sub.roi_id = r_sub.id
                WHERE r_sub.product_id = p.id AND r_sub.is_active = TRUE
                ORDER BY r_sub.updated_at DESC
                LIMIT 1
            ) rs ON TRUE
            WHERE {where_sql}
            ORDER BY p.name ASC
            LIMIT %s OFFSET %s
        '''
        rows = connection.execute(query_sql, tuple(query_params)).fetchall()

    items = []
    for r in rows:
        avail, label = map_customer_availability(r.get('roi_status'))
        items.append(CustomerProductItem(
            id=str(r['id']),
            name=r['name'],
            sku=r['sku'],
            barcode=r.get('barcode'),
            description=r.get('description'),
            sale_price=float(r['sale_price'] or 0.0),
            category=r.get('category_name'),
            shelf_id=str(r['shelf_id']) if r.get('shelf_id') else None,
            shelf_name=r.get('shelf_name'),
            location_label=r.get('location_label'),
            has_guide=bool(r.get('has_guide')),
            availability=avail,
            availability_label=label
        ))

    return CustomerProductsResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages
    )


@app.get('/customer/branches/{branch_id}/shelves', response_model=CustomerShelvesResponse)
def customer_shelves(branch_id: UUID,
                     page: int = Query(1, ge=1),
                     page_size: int = Query(5, ge=1, le=20),
                     user=Depends(current_user)):
    with db.get_connection() as connection:
        branch = require_branch_actor(connection, branch_id, user, customer=True, staff=True, admin=True)
        org_id = branch['organization_id']

        total_row = connection.execute(
            'SELECT COUNT(*) AS total FROM shelf WHERE branch_id=%s AND is_active=TRUE',
            (branch_id,)).fetchone()
        total = int(total_row['total']) if total_row else 0
        total_pages = (total + page_size - 1) // page_size if total > 0 else 1
        offset = (page - 1) * page_size

        shelf_rows = connection.execute(
            '''SELECT id, name, location_label FROM shelf
               WHERE branch_id=%s AND is_active=TRUE
               ORDER BY name ASC LIMIT %s OFFSET %s''',
            (branch_id, page_size, offset)).fetchall()

        items = []
        for sh in shelf_rows:
            shelf_id = sh['id']
            prod_row = connection.execute(
                '''SELECT COUNT(DISTINCT pid) AS c FROM (
                       SELECT r.product_id AS pid
                         FROM roi r
                         JOIN product p ON p.id=r.product_id AND p.is_active
                        WHERE r.shelf_id=%s AND r.is_active=TRUE AND p.organization_id=%s
                       UNION
                       SELECT gpl.product_id AS pid
                         FROM guide_product_light gpl
                         JOIN product p ON p.id=gpl.product_id AND p.is_active
                         JOIN led_guide_point gp ON gp.id=gpl.guide_point_id
                        WHERE gpl.branch_id=%s AND gp.shelf_id=%s AND p.organization_id=%s
                   ) t''',
                (shelf_id, org_id, branch_id, shelf_id, org_id)).fetchone()
            guide_row = connection.execute(
                '''SELECT COUNT(DISTINCT gpl.product_id) AS c
                     FROM guide_product_light gpl
                     JOIN product p ON p.id=gpl.product_id AND p.is_active
                     JOIN led_guide_point gp ON gp.id=gpl.guide_point_id
                    WHERE gpl.branch_id=%s AND gp.shelf_id=%s AND p.organization_id=%s''',
                (branch_id, shelf_id, org_id)).fetchone()

            items.append(CustomerShelfItem(
                id=str(shelf_id),
                name=sh['name'],
                location=sh.get('location_label'),
                product_count=int((prod_row or {}).get('c') or 0),
                guide_count=int((guide_row or {}).get('c') or 0),
            ))

        return CustomerShelvesResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        )


@app.get('/customer/home', response_model=CustomerHomeResponse)
def customer_home(branch_id: UUID, user=Depends(current_user)):
    with db.get_connection() as connection:
        branch = require_branch_actor(connection, branch_id, user, customer=True)
        nodes = connection.execute('''SELECT n.id FROM esp32_node n
                JOIN gateway g ON g.id=n.gateway_id
                WHERE g.branch_id=%s AND g.is_active AND n.node_code IS NOT NULL
                  AND n.registration_status='registered' ''', (branch_id,)).fetchall()
    settings = replace(InfluxSettings.from_env(), node_ids=tuple(str(n['id']) for n in nodes))
    occupancy = read_occupancy(settings, branch_id)
    near_pct = float(branch.get('occupancy_near_pct') or 80.0)
    max_cap = branch.get('max_capacity_people')
    level = classify_occupancy(occupancy.occupancy, occupancy.status, max_cap, near_pct)
    return CustomerHomeResponse(branch_id=str(branch['id']), branch_name=branch.get('name') or 'Sucursal',
                               occupancy=occupancy.occupancy, measured_at=occupancy.measured_at,
                               age_seconds=occupancy.age_seconds,
                               stale_after_seconds=occupancy.stale_after_seconds,
                               status=occupancy.status, has_catalog=True,
                               occupancy_level=level, max_capacity_people=max_cap)
app.include_router(alerts_router(current_user, db.get_connection))
