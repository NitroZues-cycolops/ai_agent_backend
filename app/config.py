import os
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

GOOGLE_CREDS_PATH = os.getenv("GOOGLE_CREDS_PATH")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

AI_SERVICE_URL = os.getenv("AI_SERVICE_URL", "http://localhost:8100")
AI_SERVICE_TIMEOUT = float(os.getenv("AI_SERVICE_TIMEOUT", "300"))
PASS1_BATCH_SIZE = int(os.getenv("PASS1_BATCH_SIZE", "10"))
PASS2_BATCH_SIZE = int(os.getenv("PASS2_BATCH_SIZE", "2"))
PASS2_TIMEOUT = float(os.getenv("PASS2_TIMEOUT", "400"))
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL not set in .env")
if not GOOGLE_CREDS_PATH:
    raise ValueError("GOOGLE_CREDS_PATH not set in .env")
if not GOOGLE_SHEET_ID:
    raise ValueError("GOOGLE_SHEET_ID not set in .env")