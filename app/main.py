import asyncio
from contextlib import asynccontextmanager
from uuid import UUID, uuid4
import jwt
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg.errors import UniqueViolation
from . import db
from .gateways import GatewayHub, create_router
from .node_registration import make_router as node_router
from .telemetry import create_router as telemetry_router
from .registration_tokens import create_router as token_router, validate_branch_token, require_branch_access
from .schemas import (BranchCreateRequest, BranchCreatedResponse, LoginRequest, LoginResponse,
                      MessageResponse, NotificationPreferences, NotificationPreferencesUpdate,
                      PasswordChangeRequest, PendingUserResponse, ProfileUpdateRequest, RegisterRequest,
                      RegisterResponse, UserResponse)
from .security import (create_access_token, decode_access_token, hash_password,
                       hash_registration_token, verify_password, generate_registration_token)

gateway_hub = GatewayHub(db.get_connection,
    validate_token=lambda branch_id, digest: validate_branch_token(db.get_connection, branch_id, digest))

@asynccontextmanager
async def lifespan(_):
    db.pool.open()
    try:
        await asyncio.to_thread(db.pool.wait, 30)
        yield
    finally:
        await gateway_hub.close()
        db.pool.close()

app = FastAPI(title='SelfIQ API', version='2.0.0', lifespan=lifespan)
app.state.gateway_hub = gateway_hub
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_credentials=False,
                   allow_methods=['*'], allow_headers=['*'])
bearer = HTTPBearer(auto_error=False)

def user_response(user):
    state = 'PENDING_APPROVAL' if user['account_status']=='pending' else user['account_status'].upper()
    return UserResponse(id=str(user['id']), email=user['email'], first_name=user['first_name'],
        last_name=user['last_name'], phone_number=user['phone_number'],
        is_superuser=user['role'].lower() == 'sudo', account_status=state,
        role=user['role'], organization_id=str(user['organization_id']),
        branch_id=str(user['branch_id']) if user['branch_id'] else None)

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

@app.post('/auth/login', response_model=LoginResponse)
def login(request: LoginRequest):
    with db.get_connection() as connection:
        user=connection.execute('SELECT * FROM "user" WHERE lower(email)=%s',(request.email.lower(),)).fetchone()
    if not user or not verify_password(request.password,user['password_hash']):
        raise HTTPException(401,'Credenciales incorrectas')
    if user['deactivated_at'] is not None or user['account_status']!='active':
        raise HTTPException(403,'La cuenta no está activa o aprobada')
    return LoginResponse(access_token=create_access_token(str(user['id'])),user=user_response(user))

def current_user(credentials: HTTPAuthorizationCredentials=Depends(bearer)):
    if not credentials:raise HTTPException(401,'Se requiere un token Bearer')
    try: user_id=UUID(decode_access_token(credentials.credentials)['sub'])
    except (jwt.InvalidTokenError,KeyError,ValueError):raise HTTPException(401,'Token inválido o expirado')
    with db.get_connection() as connection:
        user=connection.execute('SELECT *, (lower(role)=\'sudo\') AS is_superuser FROM "user" WHERE id=%s AND deactivated_at IS NULL',(user_id,)).fetchone()
    if not user or user['account_status']!='active':raise HTTPException(403,'La cuenta no está activa')
    return user

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

ORG_WIDE_ROLES = {'owner', 'gerencia'}

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
app.include_router(telemetry_router(current_user, db.get_connection))
