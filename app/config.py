import os
from .gateways import load_gateways


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://selfiq:selfiq_dev_password@localhost:5432/selfiq",
)
JWT_SECRET = os.getenv("JWT_SECRET", "cambia-esta-clave-en-produccion")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_MINUTES = int(os.getenv("ACCESS_TOKEN_MINUTES", "60"))
SELFIQ_GATEWAYS = load_gateways(os.getenv("SELFIQ_GATEWAYS", ""))