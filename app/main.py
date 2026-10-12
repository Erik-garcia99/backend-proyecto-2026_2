import asyncio
from contextlib import asynccontextmanager
from uuid import UUID, uuid4
import jwt
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg.errors import UniqueViolation
from . import db
from .config import SELFIQ_GATEWAYS
from .gateways import GatewayHub, create_router
from .registration_tokens import create_router as token_router, validate_branch_token, require_branch_access
from .schemas import (BranchCreateRequest, BranchCreatedResponse, LoginRequest, LoginResponse,
                      RegisterRequest, RegisterResponse, UserResponse)
from .security import (create_access_token, decode_access_token, hash_password,
                       hash_registration_token, verify_password, generate_registration_token)

gateway_hub = GatewayHub(SELFIQ_GATEWAYS,
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
        last_name=user['last_name'], is_superuser=user['role'].lower() == 'sudo', account_status=state,
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
