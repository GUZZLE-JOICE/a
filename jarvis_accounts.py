"""Local JARVIS account storage and optional Azure Speech secret handling.

The account database belongs to the JARVIS server.  Browser/app clients receive
opaque session tokens; Azure resource keys are encrypted with Windows DPAPI and
are never returned through the profile API.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any


PASSWORD_ITERATIONS = 350_000
SESSION_LIFETIME_SECONDS = 30 * 24 * 60 * 60
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
REGION_RE = re.compile(r"^[a-z0-9-]{2,32}$")
DEFAULT_AZURE_VOICE = "en-US-RyanMultilingualNeural"
ALLOWED_AZURE_VOICES = {
    "en-US-RyanMultilingualNeural",
    "en-US-AndrewMultilingualNeural",
    "en-US-BrianMultilingualNeural",
}


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi_protect(value: str) -> str:
    if os.name != "nt":
        raise RuntimeError("Azure voice secrets can only be saved by the Windows JARVIS app.")
    raw = value.encode("utf-8")
    in_buffer = ctypes.create_string_buffer(raw)
    in_blob = _DataBlob(len(raw), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        ctypes.c_wchar_p("JARVIS Account Secret"),
        None,
        None,
        None,
        0x1,
        ctypes.byref(out_blob),
    ):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))
    return base64.b64encode(encrypted).decode("ascii")


def _dpapi_unprotect(value: str) -> str:
    if os.name != "nt":
        raise RuntimeError("Azure voice secrets can only be opened by the Windows JARVIS app.")
    raw = base64.b64decode(value.encode("ascii"), validate=True)
    in_buffer = ctypes.create_string_buffer(raw)
    in_blob = _DataBlob(len(raw), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0x1, ctypes.byref(out_blob)
    ):
        raise ctypes.WinError()
    try:
        clear = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))
    return clear.decode("utf-8")


def _password_hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)


class AccountStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=8.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    profile_json TEXT NOT NULL DEFAULT '{}',
                    azure_key_blob TEXT NOT NULL DEFAULT '',
                    azure_region TEXT NOT NULL DEFAULT '',
                    azure_voice TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
                """
            )

    @staticmethod
    def _validate_credentials(username: str, password: str) -> tuple[str, str]:
        username = str(username or "").strip()
        password = str(password or "")
        if not USERNAME_RE.fullmatch(username):
            raise ValueError("Username must be 3-32 letters, numbers, dots, dashes, or underscores.")
        if len(password) < 8 or len(password) > 256:
            raise ValueError("Password must be 8-256 characters.")
        return username, password

    def _new_session(self, db: sqlite3.Connection, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = int(time.time())
        db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        db.execute(
            "INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)",
            (token_hash, user_id, now + SESSION_LIFETIME_SECONDS),
        )
        return token

    def register(self, username: str, password: str) -> dict[str, str]:
        username, password = self._validate_credentials(username, password)
        salt = secrets.token_bytes(16)
        digest = _password_hash(password, salt)
        now = int(time.time())
        try:
            with self._connect() as db:
                cursor = db.execute(
                    "INSERT INTO users(username,password_salt,password_hash,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (username, salt, digest, now, now),
                )
                token = self._new_session(db, int(cursor.lastrowid))
        except sqlite3.IntegrityError as exc:
            raise ValueError("That username already exists.") from exc
        return {"username": username, "token": token}

    def login(self, username: str, password: str) -> dict[str, str]:
        username, password = self._validate_credentials(username, password)
        with self._connect() as db:
            row = db.execute(
                "SELECT id,username,password_salt,password_hash FROM users WHERE username=? COLLATE NOCASE",
                (username,),
            ).fetchone()
            if row is None or not hmac.compare_digest(
                bytes(row["password_hash"]), _password_hash(password, bytes(row["password_salt"]))
            ):
                raise ValueError("Username or password is incorrect.")
            token = self._new_session(db, int(row["id"]))
            return {"username": str(row["username"]), "token": token}

    def user_for_token(self, token: str) -> sqlite3.Row | None:
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = int(time.time())
        with self._connect() as db:
            db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            return db.execute(
                """SELECT users.* FROM sessions
                   JOIN users ON users.id=sessions.user_id
                   WHERE sessions.token_hash=? AND sessions.expires_at>?""",
                (token_hash, now),
            ).fetchone()

    def get_profile(self, user_id: int) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT profile_json FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            return {}
        try:
            value = json.loads(str(row["profile_json"] or "{}"))
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}

    def set_profile(self, user_id: int, data: dict[str, Any]) -> None:
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 2 * 1024 * 1024:
            raise ValueError("Profile data is too large.")
        with self._connect() as db:
            db.execute(
                "UPDATE users SET profile_json=?,updated_at=? WHERE id=?",
                (encoded, int(time.time()), user_id),
            )

    def voice_status(self, user_id: int) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT azure_key_blob,azure_region,azure_voice FROM users WHERE id=?", (user_id,)
            ).fetchone()
        if row is None:
            return {"configured": False}
        return {
            "configured": bool(row["azure_key_blob"] and row["azure_region"]),
            "region": str(row["azure_region"] or ""),
            "voice": str(row["azure_voice"] or DEFAULT_AZURE_VOICE),
        }

    def set_voice(self, user_id: int, key: str, region: str, voice: str = DEFAULT_AZURE_VOICE) -> None:
        key = str(key or "").strip()
        region = str(region or "").strip().lower()
        voice = str(voice or DEFAULT_AZURE_VOICE).strip()
        if len(key) < 16 or len(key) > 256:
            raise ValueError("Enter the Azure Speech key from Keys and Endpoint.")
        if not REGION_RE.fullmatch(region):
            raise ValueError("Enter the Azure region, for example eastus.")
        if voice not in ALLOWED_AZURE_VOICES:
            raise ValueError("Choose one of the supported JARVIS Azure voices.")
        protected = _dpapi_protect(key)
        with self._connect() as db:
            db.execute(
                "UPDATE users SET azure_key_blob=?,azure_region=?,azure_voice=?,updated_at=? WHERE id=?",
                (protected, region, voice, int(time.time()), user_id),
            )

    def clear_voice(self, user_id: int) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE users SET azure_key_blob='',azure_region='',azure_voice='',updated_at=? WHERE id=?",
                (int(time.time()), user_id),
            )

    def voice_secret(self, user_id: int) -> dict[str, str]:
        with self._connect() as db:
            row = db.execute(
                "SELECT azure_key_blob,azure_region,azure_voice FROM users WHERE id=?", (user_id,)
            ).fetchone()
        if row is None or not row["azure_key_blob"] or not row["azure_region"]:
            raise ValueError("Azure voice is not activated for this account.")
        return {
            "key": _dpapi_unprotect(str(row["azure_key_blob"])),
            "region": str(row["azure_region"]),
            "voice": str(row["azure_voice"] or DEFAULT_AZURE_VOICE),
        }
