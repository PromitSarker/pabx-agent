import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from agent.config import DATABASE_URL
from agent.db import get_connection

if not DATABASE_URL:
    print("Error: DATABASE_URL not found in environment.")
    sys.exit(1)

# Redact the password for logging
parts = DATABASE_URL.split('@')
safe_url = parts[-1] if len(parts) > 1 else "Unknown"
print(f"Attempting to connect to MySQL host: {safe_url.split('/')[0]}")

try:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION() AS version")
            row = cur.fetchone()
            print(f"Successfully connected to MySQL!\nDatabase Version: {row['version']}")
except Exception as e:
    print(f"Failed to connect: {e}")
