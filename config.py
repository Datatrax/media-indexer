"""Configuración (.env) y comprobaciones de seguridad al arrancar."""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        sys.exit(f"[config] Falta la variable {name} en el .env")
    return value


API_ID = int(_required("API_ID"))
API_HASH = _required("API_HASH")
CHANNEL_ID = int(_required("CHANNEL_ID"))
SESSION_STRING = _required("SESSION_STRING")

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "")

ITEMS_PER_PAGE = int(os.getenv("ITEMS_PER_PAGE", "12"))
FULL_SYNC = os.getenv("FULL_SYNC", "0") == "1"   # fuerza releer todo el canal al arrancar

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
THUMB_DIR = DATA_DIR / "thumbs"
DB_PATH = DATA_DIR / "catalog.db"
DATA_DIR.mkdir(parents=True, exist_ok=True)
THUMB_DIR.mkdir(parents=True, exist_ok=True)

# ---- credenciales: no se arranca con una contraseña débil ----
_WEAK = {"", "cambia_esta_password", "admin", "password", "123456", "12345678", "changeme", "contraseña"}
if (ADMIN_PASS.lower() in _WEAK or len(ADMIN_PASS) < 10) and os.getenv("ALLOW_WEAK_PASSWORD") != "1":
    sys.exit(
        "\n[seguridad] ADMIN_PASS es débil o no está definida (mínimo 10 caracteres).\n"
        "Genera una con:\n"
        "    python -c \"import secrets; print(secrets.token_urlsafe(16))\"\n"
        "y ponla en tu .env como ADMIN_PASS=...\n"
        "(Solo para pruebas locales: ALLOW_WEAK_PASSWORD=1)\n"
    )
if ADMIN_USER.lower() in {"admin", "root", "user"}:
    print("[seguridad] Aviso: ADMIN_USER es un nombre muy común; conviene cambiarlo en el .env.")
