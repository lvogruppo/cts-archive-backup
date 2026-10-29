from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timedelta

import httpx
from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.database import OneTimeCode, SessionLocal, UserSession

COOKIE_NAME = "cts_session"
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))+$")
OTP_TTL_MINUTES = 10
SESSION_TTL_HOURS = 8
CODE_ATTEMPT_LIMIT = 5
CODE_RATE_LIMIT = 3
CODE_RATE_WINDOW_SECONDS = 15 * 60
_RATE_LIMIT_LOCK = threading.Lock()
_REQUEST_TIMES: dict[str, deque[float]] = {}


def normalize_email(value: str) -> str:
    return value.strip().lower()


def valid_email(value: str) -> bool:
    return len(value) <= 320 and bool(EMAIL_RE.fullmatch(value))


def normalize_domain(value: str) -> str:
    return value.strip().lower().rstrip(".")


def valid_domain(value: str) -> bool:
    return len(value) <= 253 and bool(DOMAIN_RE.fullmatch(value))


def email_is_allowed(_db: Session, email: str) -> bool:
    """The public OTP entrypoint accepts every syntactically valid email address."""
    return valid_email(normalize_email(email))


def _consume_request_limit(email: str) -> bool:
    """Apply the same process-local limit to authorized and unknown addresses."""
    now = time.monotonic()
    cutoff = now - CODE_RATE_WINDOW_SECONDS
    with _RATE_LIMIT_LOCK:
        bucket = _REQUEST_TIMES.setdefault(email, deque())
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= CODE_RATE_LIMIT:
            return False
        bucket.append(now)
        if len(_REQUEST_TIMES) > 10_000:
            stale = [key for key, values in _REQUEST_TIMES.items() if not values or values[-1] <= cutoff]
            for key in stale:
                _REQUEST_TIMES.pop(key, None)
            while len(_REQUEST_TIMES) > 10_000:
                _REQUEST_TIMES.pop(next(iter(_REQUEST_TIMES)))
        return True


def _hash_code(salt: str, code: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", code.encode(), bytes.fromhex(salt), 120_000).hex()


def _send_code(email: str, code: str) -> None:
    api_key = os.getenv("RESEND_API_KEY")
    from_email = os.getenv("RESEND_FROM_EMAIL")
    if not api_key or not from_email:
        raise HTTPException(status_code=503, detail="Il servizio email non è ancora configurato.")
    body = {
        "from": from_email,
        "to": [email],
        "subject": "Codice di accesso — Archivio CTS",
        "html": (
            "<div style='font-family:Arial,sans-serif;max-width:520px;margin:auto'>"
            "<h2>Accesso all'Archivio CTS</h2>"
            f"<p>Il tuo codice monouso è <strong style='font-size:28px;letter-spacing:5px'>{code}</strong>.</p>"
            f"<p>Scade tra {OTP_TTL_MINUTES} minuti. Se non hai richiesto tu il codice, ignora questa email.</p>"
            "</div>"
        ),
    }
    try:
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Invio email temporaneamente non disponibile.") from exc
    if response.status_code >= 300:
        raise HTTPException(status_code=502, detail="Resend non ha accettato l'invio del codice.")


def issue_code(db: Session, email: str) -> bool:
    email = normalize_email(email)
    now = datetime.utcnow()
    # Every valid email address can use the same request flow and rate limit.
    if not _consume_request_limit(email):
        return False
    code = f"{secrets.randbelow(1_000_000):06d}"
    salt = secrets.token_bytes(16).hex()
    row = OneTimeCode(
        email=email,
        salt=salt,
        code_hash=_hash_code(salt, code),
        attempts=0,
        created_at=now,
        expires_at=now + timedelta(minutes=OTP_TTL_MINUTES),
    )
    db.add(row)
    db.commit()
    try:
        _send_code(email, code)
    except Exception:
        row.consumed_at = datetime.utcnow()
        db.commit()
        raise
    return True


def verify_code(db: Session, email: str, code: str) -> tuple[str, str]:
    email = normalize_email(email)
    now = datetime.utcnow()
    row = (
        db.query(OneTimeCode)
        .filter(
            OneTimeCode.email == email,
            OneTimeCode.consumed_at.is_(None),
            OneTimeCode.expires_at > now,
        )
        .order_by(OneTimeCode.created_at.desc())
        .with_for_update()
        .first()
    )
    if not row or row.attempts >= CODE_ATTEMPT_LIMIT:
        raise HTTPException(status_code=400, detail="Codice non valido o scaduto.")
    row.attempts += 1
    expected = _hash_code(row.salt, code)
    if not hmac.compare_digest(expected, row.code_hash):
        db.commit()
        raise HTTPException(status_code=400, detail="Codice non valido o scaduto.")

    if not email_is_allowed(db, email):
        row.consumed_at = now
        db.commit()
        raise HTTPException(status_code=403, detail="Indirizzo non autorizzato.")

    row.consumed_at = now
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db.add(
        UserSession(
            token_hash=token_hash,
            email=email,
            created_at=now,
            expires_at=now + timedelta(hours=SESSION_TTL_HOURS),
        )
    )
    db.commit()
    return email, token


def current_user_from_request(request: Request) -> str | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token or SessionLocal is None:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        session = (
            db.query(UserSession)
            .filter(
                UserSession.token_hash == token_hash,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > now,
            )
            .first()
        )
        if not session:
            return None
        if not email_is_allowed(db, session.email):
            return None
        return session.email
    finally:
        db.close()


def revoke_session(request: Request) -> None:
    token = request.cookies.get(COOKIE_NAME)
    if not token or SessionLocal is None:
        return
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db = SessionLocal()
    try:
        session = db.query(UserSession).filter(UserSession.token_hash == token_hash).first()
        if session and session.revoked_at is None:
            session.revoked_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()


def cookie_options() -> dict:
    # Cloud Preview is public HTTPS even when the internal development request is HTTP.
    # Set APP_COOKIE_SECURE=false only for direct plain-HTTP local testing.
    secure = os.getenv("APP_COOKIE_SECURE", "true").lower() not in {"0", "false", "no"}
    return {"httponly": True, "secure": secure, "samesite": "none" if secure else "lax", "path": "/"}
