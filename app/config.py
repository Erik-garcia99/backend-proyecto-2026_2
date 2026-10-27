import os
from pathlib import Path

from dotenv import load_dotenv

# Solo desarrollo local; las variables de Railway siempre tienen prioridad.
load_dotenv(Path(__file__).resolve().parent.parent / '.env', override=False)

DATABASE_URL = os.environ["DATABASE_URL"]
JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALGORITHM = "HS256"
