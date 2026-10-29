from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import (
    Chunk,
    Document,
    Page,
    SessionLocal,
    create_schema,
    db_session,
    engine,
)
from app.retrieval.search import search_chunks
from app.security import (
    COOKIE_NAME,
    cookie_options,
    current_user_from_request,
    issue_code,
    normalize_email,
    revoke_session,
    valid_email,
    verify_code,
)
from app.services.answers import AnswerServiceError, answer_with_sources
from app.services.coverage import archive_coverage

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
logger = logging.getLogger("ctsrag")

app = FastAPI(title="Archivio CTS", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


class CodeRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class CodeVerify(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class SearchRequest(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    date_from: str | None = Field(default=None, max_length=10, pattern=r"^\d{4}-\d{2}-\d{2}$")
    date_to: str | None = Field(default=None, max_length=10, pattern=r"^\d{4}-\d{2}-\d{2}$")
    meeting_number: str | None = Field(default=None, max_length=80)
    limit: int = Field(default=15, ge=1, le=30)

    @field_validator("date_from", "date_to")
    @classmethod
    def validate_calendar_date(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("La data deve essere valida e nel formato AAAA-MM-GG.") from exc
        return value


def get_db() -> Session:
    if SessionLocal is None:
        raise HTTPException(status_code=503, detail="Database non configurato.")
    return db_session()


def db_dependency():
    db = get_db()
    try:
        yield db
    finally:
        db.close()


def require_user(request: Request) -> str:
    email = current_user_from_request(request)
    if not email:
        raise HTTPException(status_code=401, detail="Accesso richiesto.")
    return email


def otp_ready() -> bool:
    return bool(os.getenv("RESEND_API_KEY") and os.getenv("RESEND_FROM_EMAIL"))


def consultation_ready(db: Session) -> bool:
    if not otp_ready():
        return False
    coverage = archive_coverage(db)
    return coverage.complete and bool(coverage.eligible_document_ids)


def require_otp_ready() -> None:
    if not otp_ready():
        raise HTTPException(status_code=503, detail="L'invio dei codici di accesso non è ancora configurato.")


def require_consultation_ready(db: Session) -> set[int]:
    if not otp_ready():
        raise HTTPException(status_code=503, detail="L'archivio non è ancora pronto per la consultazione riservata.")
    coverage = archive_coverage(db)
    if not coverage.complete or not coverage.eligible_document_ids:
        raise HTTPException(status_code=503, detail="L'archivio non è ancora pronto per la consultazione riservata.")
    return set(coverage.eligible_document_ids)


def locked_page(request: Request, email: str):
    return templates.TemplateResponse(
        request=request,
        name="locked.html",
        context={"email": email},
        status_code=503,
    )


@app.on_event("startup")
def initialize_database() -> None:
    # Additive create-if-missing only; no tables or user data are dropped.
    if engine is None:
        logger.warning("DATABASE_URL non configurato; il database CTS non è inizializzato.")
        return
    try:
        create_schema()
    except Exception as exc:
        logger.exception("Inizializzazione additiva dello schema CTS non riuscita (%s).", type(exc).__name__)


@app.get("/health")
def health() -> JSONResponse:
    if engine is None:
        return JSONResponse(status_code=503, content={"status": "not_ready", "database_configured": False})
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("Health check database non riuscito (%s).", type(exc).__name__)
        return JSONResponse(status_code=503, content={"status": "not_ready", "database_configured": True})
    return JSONResponse(
        content={
            "status": "ok",
            "database_configured": True,
            "auth_provider_configured": bool(os.getenv("RESEND_API_KEY") and os.getenv("RESEND_FROM_EMAIL")),
            "llm_service_configured": bool(os.getenv("MANUS_API_URL") and os.getenv("MANUS_API_KEY")),
        }
    )


@app.get("/manus-routes.json")
def route_manifest() -> FileResponse:
    return FileResponse(PROJECT_DIR / "public" / "manus-routes.json", media_type="application/json")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(db_dependency)):
    email = current_user_from_request(request)
    if email:
        if consultation_ready(db):
            return RedirectResponse("/", status_code=303)
        return locked_page(request, email)
    email_delivery_ready = otp_ready()
    consultation_available = consultation_ready(db)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "email_delivery_ready": email_delivery_ready,
            "consultation_available": consultation_available,
        },
    )


@app.get("/", response_class=HTMLResponse)
def archive_page(request: Request, db: Session = Depends(db_dependency)):
    email = current_user_from_request(request)
    if not email:
        return RedirectResponse("/login", status_code=303)
    if not consultation_ready(db):
        return locked_page(request, email)
    return templates.TemplateResponse(request=request, name="archive.html", context={"email": email})


@app.post("/api/auth/request-code")
def request_login_code(payload: CodeRequest, db: Session = Depends(db_dependency)):
    require_otp_ready()
    email = normalize_email(payload.email)
    if not valid_email(email):
        raise HTTPException(status_code=422, detail="Inserisci un indirizzo email valido.")
    issue_code(db, email)
    # Same response for authorized and unauthorized addresses to avoid allowlist disclosure.
    return JSONResponse(
        status_code=202,
        content={"ok": True, "message": "Se l'indirizzo è abilitato, riceverai un codice monouso."},
    )


@app.post("/api/auth/verify-code")
def verify_login_code(payload: CodeVerify, db: Session = Depends(db_dependency)):
    require_consultation_ready(db)
    email = normalize_email(payload.email)
    if not valid_email(email):
        raise HTTPException(status_code=400, detail="Codice non valido o scaduto.")
    _, token = verify_code(db, email, payload.code)
    response = JSONResponse({"ok": True, "redirect": "/"})
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=8 * 60 * 60,
        **cookie_options(),
    )
    return response


@app.post("/api/auth/logout")
def logout(request: Request):
    revoke_session(request)
    options = cookie_options()
    response = JSONResponse({"ok": True, "redirect": "/login"})
    response.delete_cookie(
        key=COOKIE_NAME,
        path=options["path"],
        secure=options["secure"],
        httponly=options["httponly"],
        samesite=options["samesite"],
    )
    return response


@app.get("/api/status")
def archive_status(
    email: str = Depends(require_user),
    db: Session = Depends(db_dependency),
) -> dict[str, Any]:
    require_consultation_ready(db)
    total_documents = db.query(Document).count()
    processed_documents = db.query(Document).filter(Document.status == "processed").count()
    duplicate_documents = db.query(Document).filter(Document.status == "duplicate").count()
    failed_documents = db.query(Document).filter(Document.status.in_(["error", "partial"])).count()
    total_pages = db.query(Page).count()
    covered_pages = db.query(Page).filter(Page.status.in_(["processed", "empty"])).count()
    ocr_pages = db.query(Page).filter(Page.ocr_used.is_(True)).count()
    ocr_problem_pages = db.query(Page).filter(Page.ocr_status.in_(["error", "empty"])).count()
    indexed_chunks = db.query(Chunk).filter(Chunk.embedding.is_not(None)).count()
    return {
        "email": email,
        "total_documents": total_documents,
        "processed_documents": processed_documents,
        "duplicate_documents": duplicate_documents,
        "failed_documents": failed_documents,
        "total_pages": total_pages,
        "processed_pages": covered_pages,
        "ocr_pages": ocr_pages,
        "ocr_problem_pages": ocr_problem_pages,
        "indexed_chunks": indexed_chunks,
        "coverage": "complete",
    }


def _run_search(payload: SearchRequest, db: Session, eligible_document_ids: set[int]) -> list[dict[str, Any]]:
    comparative_markers = (
        "confronta", "confrontare", "confronto", "differenze", "evoluzione",
        "nel tempo", "come è cambiato", "come e cambiato", "tutti i verbali",
        "tutte le riunioni", "quali date", "quali verbali",
    )
    question_folded = payload.question.casefold()
    comparative = any(marker in question_folded for marker in comparative_markers)
    search_limit = max(payload.limit, 40) if comparative else payload.limit
    try:
        return search_chunks(
            db,
            payload.question,
            eligible_document_ids=eligible_document_ids,
            limit=search_limit,
            date_from=payload.date_from,
            date_to=payload.date_to,
            meeting_number=payload.meeting_number,
            per_document_limit=2 if comparative else 3,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="La domanda supera i limiti del modello; prova a formularla più brevemente.") from exc
    except Exception as exc:
        logger.exception("Ricerca CTS non riuscita (%s).", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Ricerca non disponibile o indice non ancora pronto.") from exc


@app.post("/api/search")
def search_api(
    payload: SearchRequest,
    email: str = Depends(require_user),
    db: Session = Depends(db_dependency),
) -> dict[str, Any]:
    eligible_document_ids = require_consultation_ready(db)
    sources = _run_search(payload, db, eligible_document_ids)
    return {"question": payload.question, "count": len(sources), "results": sources}


@app.post("/api/query")
def query_api(
    payload: SearchRequest,
    email: str = Depends(require_user),
    db: Session = Depends(db_dependency),
) -> dict[str, Any]:
    eligible_document_ids = require_consultation_ready(db)
    sources = _run_search(payload, db, eligible_document_ids)
    try:
        answer = answer_with_sources(payload.question, sources)
    except AnswerServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    coverage_note = "Copertura verificata sull'intero inventario atteso; i passaggi citati sono i più pertinenti e non provano da soli l'esaustività interpretativa. Numero o data non identificati sono indicati come tali, senza inferenze."
    return {
        "question": payload.question,
        "answer": answer,
        "count": len(sources),
        "sources": sources,
        "coverage_note": coverage_note,
    }
