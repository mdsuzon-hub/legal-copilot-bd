#!/usr/bin/env python3
"""
apps/web/db.py
--------------
SQLite database manager for user authentication.
Handles user registration, login updates, and duplicate prevention.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import config


def get_db_connection() -> sqlite3.Connection:
    """Get a SQLite database connection with row factory."""
    db_path = Path(config.USER_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Initialize the users table and indexes if they do not exist."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                google_id TEXT UNIQUE,
                email TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                picture TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_google_id ON users(google_id)")
        conn.commit()


def get_or_create_user(
    google_id: Optional[str],
    email: str,
    name: str,
    picture: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """
    Retrieve an existing user by email or register a new user.
    Prevents duplicate accounts based on email.
    
    Returns:
        (user_dict, is_new_user)
    """
    clean_email = email.strip().lower()
    clean_name = name.strip() or clean_email.split("@")[0]
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    with get_db_connection() as conn:
        cursor = conn.cursor()

        # Check existing user by email
        cursor.execute("SELECT * FROM users WHERE email = ?", (clean_email,))
        row = cursor.fetchone()

        if row:
            # Existing user: update login timestamp and missing fields
            user_id = row["id"]
            new_google_id = google_id or row["google_id"]
            new_picture = picture or row["picture"]
            new_name = clean_name or row["name"]

            cursor.execute("""
                UPDATE users
                SET google_id = ?,
                    name = ?,
                    picture = ?,
                    last_login = ?
                WHERE id = ?
            """, (new_google_id, new_name, new_picture, now_str, user_id))
            conn.commit()

            cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            updated_row = cursor.fetchone()
            return dict(updated_row), False
        else:
            # New user: insert into database
            cursor.execute("""
                INSERT INTO users (google_id, email, name, picture, created_at, last_login)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (google_id, clean_email, clean_name, picture, now_str, now_str))
            conn.commit()
            new_id = cursor.lastrowid

            cursor.execute("SELECT * FROM users WHERE id = ?", (new_id,))
            new_row = cursor.fetchone()
            return dict(new_row), True


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    """Retrieve user by database ID."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Retrieve user by email."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),))
        row = cursor.fetchone()
        return dict(row) if row else None
