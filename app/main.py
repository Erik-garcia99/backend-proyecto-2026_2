from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import jwt
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg.errors import UniqueViolation

from . import db
from .schemas import (BranchCreateRequest, BranchResponse, LoginRequest, LoginResponse,
                      RegisterRequest, RegisterResponse, UserResponse)
from .security import (
    create_access_token,
    decode_access_token,
    hash_password,
    hash_registration_token,
    verify_password,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.pool.open()
    try:
        yield
    finally:
        db.pool.close()


app = FastAPI(title="SelfIQ API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
bearer = HTTPBearer(auto_error=False)


def user_response(user: dict) -> UserResponse:
    return UserResponse(
        id=str(user["id"]), email=user["email"], first_name=user["first_name"],
        last_name=user["last_name"], is_superuser=user["is_superuser"],
        account_status=user["account_status"],
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/auth/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
def register(request: RegisterRequest):
    token_hash = hash_registration_token(request.registration_token)
    with db.get_connection() as connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, branch_id, max_uses, uses
                    FROM branch_registration_token
                    WHERE token_hash = %s AND token_type = 'USER_REGISTRATION'
                      AND is_active = TRUE
                      AND (expires_at IS NULL OR expires_at > NOW())
                      AND (max_uses IS NULL OR uses < max_uses)
                    FOR UPDATE
                    """,
                    (token_hash,),
                )
                registration_token = cursor.fetchone()
                if not registration_token:
                    raise HTTPException(status_code=400, detail="Token de registro inválido o expirado")

                cursor.execute("SELECT id FROM users WHERE email = %s", (str(request.email).lower(),))
                if cursor.fetchone():
                    raise HTTPException(status_code=409, detail="El correo ya está registrado")

                cursor.execute(
                    """
                    INSERT INTO users (email, password_hash, first_name, last_name, phone, account_status)
                    VALUES (%s, %s, %s, %s, %s, 'PENDING_APPROVAL')
                    RETURNING id, account_status
                    """,
                    (str(request.email).lower(), hash_password(request.password), request.first_name,
                     request.last_name, request.phone),
                )
                user = cursor.fetchone()
                cursor.execute(
                    """
                    INSERT INTO user_branch (user_id, branch_id, membership_status)
                    VALUES (%s, %s, 'PENDING')
                    """,
                    (user["id"], registration_token["branch_id"]),
                )
                cursor.execute(
                    "UPDATE branch_registration_token SET uses = uses + 1 WHERE id = %s",
                    (registration_token["id"],),
                )
            connection.commit()
        except UniqueViolation:
            connection.rollback()
            raise HTTPException(status_code=409, detail="El correo ya está registrado")
        except HTTPException:
            connection.rollback()
            raise

    return RegisterResponse(message="Cuenta creada; queda pendiente de aprobación", user_id=str(user["id"]), account_status=user["account_status"])


@app.post("/auth/login", response_model=LoginResponse)
def login(request: LoginRequest):
    with db.get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE email = %s", (str(request.email).lower(),))
            user = cursor.fetchone()
            if not user or not verify_password(request.password, user["password_hash"]):
                raise HTTPException(status_code=401, detail="Credenciales incorrectas")
            if not user["is_active"] or user["account_status"] != "ACTIVE":
                raise HTTPException(status_code=403, detail="La cuenta no está activa o aprobada")
            cursor.execute("UPDATE users SET last_login_at = NOW() WHERE id = %s", (user["id"],))
        connection.commit()
    return LoginResponse(access_token=create_access_token(str(user["id"])), user=user_response(user))


def current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer)):
    if not credentials:
        raise HTTPException(status_code=401, detail="Se requiere un token Bearer")
    try:
        payload = decode_access_token(credentials.credentials)
        user_id = UUID(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError):
        raise HTTPException(status_code=401, detail="Token inválido o expirado")

    with db.get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE id = %s AND is_active = TRUE", (user_id,))
            user = cursor.fetchone()
    if not user or user["account_status"] != "ACTIVE":
        raise HTTPException(status_code=403, detail="La cuenta no está activa")
    return user


@app.get("/auth/me", response_model=UserResponse)
def me(user: dict = Depends(current_user)):
    return user_response(user)


@app.post("/branches", response_model=BranchResponse, status_code=status.HTTP_201_CREATED)
def create_branch(request: BranchCreateRequest, creator: dict = Depends(current_user)):
    """Crea una sucursal usando un token de creación válido."""
    with db.get_connection() as connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT t.id, t.branch_id, t.max_uses, t.uses, b.business_id
                    FROM branch_registration_token t
                    JOIN branch b ON b.id = t.branch_id
                    WHERE t.token_hash = %s AND t.token_type = 'BRANCH_CREATION'
                      AND t.is_active = TRUE
                      AND (t.expires_at IS NULL OR t.expires_at > NOW())
                      AND (t.max_uses IS NULL OR t.uses < t.max_uses)
                    FOR UPDATE
                    """,
                    (hash_registration_token(request.registration_token),),
                )
                token = cursor.fetchone()
                if not token:
                    raise HTTPException(status_code=400, detail="Token de sucursal inválido o expirado")

                if not creator["is_superuser"]:
                    cursor.execute(
                        """
                        SELECT 1 FROM user_branch ub
                        JOIN role r ON r.id = ub.role_id
                        WHERE ub.user_id = %s AND ub.branch_id = %s
                          AND ub.membership_status = 'ACTIVE'
                          AND r.name IN ('OWNER', 'MANAGER')
                        LIMIT 1
                        """,
                        (creator["id"], token["branch_id"]),
                    )
                    if not cursor.fetchone():
                        raise HTTPException(status_code=403, detail="No tienes permisos para crear sucursales")

                code = f"SUC-{str(uuid4())[:8].upper()}"
                cursor.execute(
                    """
                    INSERT INTO branch (business_id, name, code, address, timezone, business_category)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id, name, code, address, timezone, business_category, business_id
                    """,
                    (token["business_id"], request.name, code, request.address,
                     request.timezone, request.category),
                )
                branch = cursor.fetchone()
                cursor.execute(
                    "UPDATE branch_registration_token SET uses = uses + 1 WHERE id = %s",
                    (token["id"],),
                )
            connection.commit()
        except HTTPException:
            connection.rollback()
            raise

    return BranchResponse(
        id=str(branch["id"]), name=branch["name"], code=branch["code"],
        address=branch["address"], timezone=branch["timezone"],
        category=branch["business_category"], business_id=str(branch["business_id"]),
    )


@app.post("/auth/approve/{user_id}", response_model=UserResponse)
def approve_user(user_id: UUID, approver: dict = Depends(current_user)):
    """Activa una cuenta pendiente; disponible para SUDO o MANAGER."""
    with db.get_connection() as connection:
        with connection.cursor() as cursor:
            if not approver["is_superuser"]:
                cursor.execute(
                    """
                    SELECT 1 FROM user_branch ub
                    JOIN role r ON r.id = ub.role_id
                    WHERE ub.user_id = %s AND ub.membership_status = 'ACTIVE'
                      AND r.name IN ('MANAGER', 'OWNER')
                    LIMIT 1
                    """,
                    (approver["id"],),
                )
                if not cursor.fetchone():
                    raise HTTPException(status_code=403, detail="No tienes permisos para aprobar cuentas")

            cursor.execute(
                """
                UPDATE users SET account_status = 'ACTIVE'
                WHERE id = %s AND account_status = 'PENDING_APPROVAL'
                RETURNING *
                """,
                (user_id,),
            )
            approved = cursor.fetchone()
            if not approved:
                raise HTTPException(status_code=404, detail="Cuenta pendiente no encontrada")
            cursor.execute(
                """
                UPDATE user_branch SET membership_status = 'ACTIVE', approved_by = %s,
                    approved_at = NOW()
                WHERE user_id = %s AND membership_status = 'PENDING'
                """,
                (approver["id"], user_id),
            )
        connection.commit()
    return user_response(approved)
