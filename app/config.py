import os
from pathlib import Path

from dotenv import load_dotenv

# Solo desarrollo local; las variables de Railway siempre tienen prioridad.
load_dotenv(Path(__file__).resolve().parent.parent / '.env', override=False)

DATABASE_URL = os.environ["DATABASE_URL"]
JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALGORITHM = "HS256"

# Organización por defecto para el autorregistro de clientes. Si está vacía y
# solo existe una organización activa, el endpoint la usa; si hay varias,
# exige `organization_slug` en la solicitud.
CUSTOMER_ORGANIZATION_SLUG = os.getenv("CUSTOMER_ORGANIZATION_SLUG", "").strip()

# Auth: duración de tokens
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "30"))
