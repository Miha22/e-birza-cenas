import os
from pathlib import Path

if os.getenv("PANDA_STACK_PROD"):
    BASE_DIR = Path("/data")
else:
    BASE_DIR = Path(__file__).resolve().parent
TRUSTED_CLIENTS_FILE = BASE_DIR / "trusted_clients.txt"
SUBS_COOLDOWN = 300

if not TRUSTED_CLIENTS_FILE.exists():
    TRUSTED_CLIENTS_FILE.touch()

MASTER_PUBLIC_KEY_HEX = os.getenv(
    "MASTER_PUBLIC_KEY_HEX", 
    "insert_master_public"
)