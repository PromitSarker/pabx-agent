import os
import time
from contextlib import contextmanager
from urllib.parse import urlparse, unquote

import pymysql
from pymysql.cursors import DictCursor

from agent.config import DATABASE_URL


def _connect_kwargs() -> dict:
    """Translate DATABASE_URL (mysql://user:pass@host:port/dbname) into pymysql kwargs."""
    url = urlparse(DATABASE_URL)
    return {
        "host": url.hostname or "localhost",
        "port": url.port or 3306,
        "user": unquote(url.username or ""),
        "password": unquote(url.password or ""),
        "database": (url.path or "/").lstrip("/"),
        "charset": "utf8mb4",
        "connect_timeout": 10,
    }


def initialize_db():
    if not DATABASE_URL:
        print("DATABASE_URL is not set, skipping database initialization")
        return

    print("Initializing database from schema...")

    # We always run the schema to ensure IF NOT EXISTS tables are created
    schema_path = os.path.join(os.path.dirname(__file__), "..", "sql", "schema.sql")
    if not os.path.exists(schema_path):
        return
    with open(schema_path, "r", encoding="utf-8") as f:
        statements = [s.strip() for s in f.read().split(";") if s.strip()]

    # The db container may still be starting, so retry for a while.
    for attempt in range(30):
        try:
            conn = pymysql.connect(**_connect_kwargs())
            try:
                with conn.cursor() as cur:
                    for statement in statements:
                        cur.execute(statement)
                conn.commit()
            finally:
                conn.close()
            return
        except Exception as e:
            if attempt == 29:
                print(f"Failed to initialize schema: {e}")
            else:
                time.sleep(2)


@contextmanager
def get_connection():
    conn = pymysql.connect(cursorclass=DictCursor, **_connect_kwargs())
    try:
        yield conn
    finally:
        conn.close()


# Initialize schema when this module is loaded
initialize_db()
