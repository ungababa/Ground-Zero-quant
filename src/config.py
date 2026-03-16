import os
from dotenv import load_dotenv

load_dotenv()

ROOSTOO_API_KEY = os.getenv("ROOSTOO_API_KEY", "")
ROOSTOO_SECRET_KEY = os.getenv("ROOSTOO_SECRET_KEY", "")
ROOSTOO_BASE_URL = os.getenv("ROOSTOO_BASE_URL", "https://mock-api.roostoo.com")

if not ROOSTOO_API_KEY:
    print("Warning: ROOSTOO_API_KEY is not set.")
if not ROOSTOO_SECRET_KEY:
    print("Warning: ROOSTOO_SECRET_KEY is not set.")