#!/usr/bin/env python3
"""
Automated responder for HARO and Help a B2B Writer source request emails.

Features
- Gmail API OAuth2 authentication with token.json flow
- Polls Gmail for messages with a specific label
- Parses email content and extracts structured fields
- Uses Google Gemini to generate tailored draft responses
- Sends drafts to Telegram for review with inline keyboard (Approve, Edit, Reject)
- Sends approved replies via Gmail with proper threading headers
- SQLite persistence, robust logging, and retries

Setup
- See README.md for full setup instructions and required environment variables.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import email
import email.policy
import json
import random
import logging
import os
import re
import signal
import sqlite3
import sys
import textwrap
import threading
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from gemini_filter import should_include_query_gemini, USE_GEMINI_FILTERING
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Gmail API
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Google Gemini
import google.generativeai as genai

# Telegram bot (async, v21+)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# HTML parsing
from bs4 import BeautifulSoup


# ------------------------------
# Configuration and Globals
# ------------------------------

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
]

GMAIL_LABEL_NAME = os.getenv("GMAIL_LABEL_NAME", "HARO/HelpAB2BWriter")
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0") or "0")

DB_PATH = os.getenv("DB_PATH", "data/app.db")
LOG_DIR = os.getenv("LOG_DIR", "logs")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "120"))
MAX_TELEGRAM_MESSAGE_CHARS = 3800

# HARO filtering configuration
HARO_INCLUDE_KEYWORDS = {
    kw.strip().lower()
    for kw in (os.getenv("HARO_INCLUDE_KEYWORDS", "").split(",") if os.getenv("HARO_INCLUDE_KEYWORDS") else [])
    if kw.strip()
}
HARO_EXCLUDE_KEYWORDS = {
    kw.strip().lower()
    for kw in (os.getenv("HARO_EXCLUDE_KEYWORDS", "").split(",") if os.getenv("HARO_EXCLUDE_KEYWORDS") else [])
    if kw.strip()
}

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs("templates", exist_ok=True)


# ------------------------------
# Logging Setup
# ------------------------------

def configure_logging() -> None:
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    # Rotating file handler
    from logging.handlers import RotatingFileHandler

    fh = RotatingFileHandler(
        os.path.join(LOG_DIR, "app.log"), maxBytes=5 * 1024 * 1024, backupCount=3
    )
    fh.setLevel(logging.INFO)
    fh.setFormatter(formatter)
    logger.addHandler(fh)


configure_logging()
logger = logging.getLogger("main")


# ------------------------------
# Database Layer
# ------------------------------

_db_lock = threading.Lock()


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _db_lock:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gmail_message_id TEXT UNIQUE,
                    gmail_thread_id TEXT,
                    subject TEXT,
                    sender TEXT,
                    sender_email TEXT,
                    reply_to TEXT,
                    received_at TEXT,
                    deadline TEXT,
                    requirements TEXT,
                    query_text TEXT,
                    status TEXT,
                    original_headers TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id INTEGER,
                    subject TEXT,
                    body TEXT,
                    model TEXT,
                    approved INTEGER DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT,
                    FOREIGN KEY (request_id) REFERENCES requests(id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id INTEGER,
                    chat_id TEXT,
                    message_id INTEGER,
                    created_at TEXT,
                    FOREIGN KEY (request_id) REFERENCES requests(id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS actions_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id INTEGER,
                    action TEXT,
                    details TEXT,
                    created_at TEXT,
                    FOREIGN KEY (request_id) REFERENCES requests(id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_edits (
                    chat_id TEXT,
                    request_id INTEGER,
                    PRIMARY KEY (chat_id, request_id)
                )
                """
            )
            conn.commit()
        finally:
            conn.close()


def db_execute(query: str, params: Tuple[Any, ...] = ()) -> None:
    with _db_lock:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(query, params)
            conn.commit()
        finally:
            conn.close()


def db_query_one(query: str, params: Tuple[Any, ...] = ()) -> Optional[sqlite3.Row]:
    with _db_lock:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(query, params)
            row = cur.fetchone()
            return row
        finally:
            conn.close()


def db_query_all(query: str, params: Tuple[Any, ...] = ()) -> List[sqlite3.Row]:
    with _db_lock:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(query, params)
            rows = cur.fetchall()
            return rows
        finally:
            conn.close()


def log_action(request_id: int, action: str, details: str = "") -> None:
    db_execute(
        "INSERT INTO actions_log (request_id, action, details, created_at) VALUES (?, ?, ?, ?)",
        (request_id, action, details, dt.datetime.utcnow().isoformat()),
    )


# ------------------------------
# Gmail API Layer
# ------------------------------


def get_gmail_service() -> Any:
    creds = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as token:
            token.write(creds.to_json())
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return service


@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(HttpError),
)
def gmail_list_messages(service: Any, label_id: Optional[str], unread_only: bool = True) -> List[str]:
    query = None
    label_ids = []
    if label_id:
        label_ids.append(label_id)
    
    # Build query to get unread messages if specified
    if unread_only:
        query = "is:unread"

    results = service.users().messages().list(
        userId="me",
        labelIds=label_ids or None,
        q=query,
        maxResults=50,
    ).execute()
    messages = results.get("messages", [])
    ids = [m["id"] for m in messages]
    return ids


@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(HttpError),
)
def gmail_get_message(service: Any, message_id: str) -> Dict[str, Any]:
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )
    return msg


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(HttpError),
)
def gmail_mark_as_read(service: Any, message_id: str) -> None:
    """Mark a Gmail message as read by removing the UNREAD label."""
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"removeLabelIds": ["UNREAD"]}
    ).execute()


def gmail_get_label_id(service: Any, label_name: str) -> Optional[str]:
    try:
        lbls = service.users().labels().list(userId="me").execute().get("labels", [])
        for lbl in lbls:
            if lbl.get("name") == label_name:
                return lbl.get("id")
    except Exception as e:
        logger.error("Failed to get label id: %s", e)
    return None


def decode_email_body(payload: Dict[str, Any]) -> Tuple[str, str]:
    """Return (text/plain, text/html) from Gmail payload."""
    def decode_part(body_obj: Dict[str, Any]) -> str:
        data = body_obj.get("data")
        if not data:
            return ""
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")

    text_part = ""
    html_part = ""

    if payload.get("mimeType", "").startswith("multipart"):
        for part in payload.get("parts", []):
            mime = part.get("mimeType", "")
            if mime == "text/plain":
                text_part += decode_part(part.get("body", {}))
            elif mime == "text/html":
                html_part += decode_part(part.get("body", {}))
            elif mime.startswith("multipart"):
                # Nested multiparts
                nested_text, nested_html = decode_email_body(part)
                text_part += nested_text
                html_part += nested_html
    else:
        mime = payload.get("mimeType", "")
        if mime == "text/plain":
            text_part = decode_part(payload.get("body", {}))
        elif mime == "text/html":
            html_part = decode_part(payload.get("body", {}))

    return text_part, html_part


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    # Remove scripts and styles
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text("\n")
    # Normalize whitespace
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def extract_header(headers: List[Dict[str, str]], name: str) -> Optional[str]:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value")
    return None


def parse_address(addr: str) -> Tuple[str, str]:
    """Return (display_name, email_address)."""
    if not addr:
        return "", ""
    from email.utils import parseaddr

    name, email_addr = parseaddr(addr)
    return name or email_addr, email_addr


# ------------------------------
# Request Parsing
# ------------------------------


@dataclass
class ParsedRequest:
    subject: str
    sender: str
    sender_email: str
    reply_to: str
    received_at: str
    deadline: Optional[str]
    requirements: Optional[str]
    query_text: str
    original_headers: Dict[str, Any]
    gmail_message_id: str
    gmail_thread_id: str
    # Optional structured fields
    summary: Optional[str] = None
    category: Optional[str] = None
    media_outlet: Optional[str] = None
    provider: Optional[str] = None  # "HARO" | "HELP_A_B2B_WRITER" | None
    query_index: Optional[int] = None  # For HARO multi-query digests
    requester_name: Optional[str] = None  # Reporter/requester name from HARO/B2B


DEADLINE_PATTERNS = [
    re.compile(r"^\s*deadline\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE),
]

REQUIREMENTS_PATTERNS = [
    re.compile(r"^\s*requirements?\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*what we need\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE),
]

QUERY_PATTERNS = [
    re.compile(r"^\s*query\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*summary\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*topic\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE),
]


def extract_first(patterns: List[re.Pattern], text: str) -> Optional[str]:
    for p in patterns:
        m = p.search(text)
        if m:
            return m.group(1).strip()
    return None


def _detect_provider(subject: str, headers: List[Dict[str, str]], body_text: str) -> Optional[str]:
    from_header = extract_header(headers, "From") or ""
    list_id = extract_header(headers, "List-Id") or ""
    if "helpareporter.com" in from_header.lower() or "haro" in subject.lower() or "helpareporter" in list_id.lower():
        return "HARO"
    if "helpab2bwriter.com" in body_text.lower() or "Help a B2B Writer".lower() in body_text.lower() or "help a b2b writer" in subject.lower():
        return "HELP_A_B2B_WRITER"
    return None


def _should_include_haro_query(blob: str) -> bool:
    text = blob.lower()
    if HARO_INCLUDE_KEYWORDS:
        if not any(kw in text for kw in HARO_INCLUDE_KEYWORDS):
            return False
    if HARO_EXCLUDE_KEYWORDS:
        if any(kw in text for kw in HARO_EXCLUDE_KEYWORDS):
            return False
    return True


def _parse_haro_queries(body_text: str) -> List[Dict[str, Optional[str]]]:
    """Parse HARO digest email into individual query dicts.
    Returns list of dicts with keys: summary, name, category, email, media_outlet, deadline, query.
    """
    def clean_haro_text(text: str) -> str:
        # Remove zero-width and BOM chars
        text = re.sub(r"[\u200B-\u200D\uFEFF]", "", text)
        # Normalize multiple blank lines
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return text

    # Clean the body before parsing
    text = clean_haro_text(body_text)

    # Regex to capture sections (more permissive newlines, case-insensitive)
    pattern = re.compile(
        r"^\s*(?P<idx>\d+)\)\s*Summary:\s*(?P<summary>.*?)\n+"
        r"Name:\s*(?P<name>.*?)\n+"
        r"Category:\s*(?P<category>.*?)\n+"
        r"Email:\s*(?P<email>.*?)\n+"
        r"(?:Muck Rack URL:.*?\n+)?"
        r"Media Outlet:\s*(?P<media>.*?)\n+"
        r"Deadline:\s*(?P<deadline>.*?)\n+"
        r"Query:\s*\n+(?P<query>.*?)(?:\n+Back to Top|$)",
        re.DOTALL | re.MULTILINE | re.IGNORECASE,
    )

    # Cleaner for encoded noise inside query bodies (hex/base64 blobs pasted by HARO)
    def clean_query_blob(q: str) -> str:
        q = re.sub(r"[\u200B-\u200D\uFEFF]", "", q)
        # Remove very long base64/hex-like runs that are not human content
        q = re.sub(r"\b[0-9A-Fa-f]{40,}\b", "", q)  # long hex
        q = re.sub(r"\b[A-Za-z0-9+/=]{60,}\b", "", q)  # long base64-like
        # Collapse excessive whitespace
        q = re.sub(r"\n\s*\n+", "\n\n", q).strip()
        return q

    items: List[Dict[str, Optional[str]]] = []
    for m in pattern.finditer(text):
        items.append(
            {
                "idx": m.group("idx"),
                "summary": (m.group("summary") or "").strip(),
                "name": (m.group("name") or "").strip(),
                "category": (m.group("category") or "").strip(),
                "email": (m.group("email") or "").strip(),
                "media_outlet": (m.group("media") or "").strip(),
                "deadline": (m.group("deadline") or "").strip(),
                "query": clean_query_blob((m.group("query") or "").strip()),
            }
        )

    # Fallback: if parser failed to find any items, try a simpler split and parse
    if not items:
        blocks = re.split(r"^\s*(?=\d+\)\s*Summary:)", text, flags=re.MULTILINE)
        for blk in blocks:
            if not blk.strip().startswith("1)") and not re.match(r"^\d+\)", blk.strip()):
                continue
            def find(label: str) -> Optional[str]:
                m = re.search(rf"{label}\s*:\s*(.+?)\n+", blk, re.IGNORECASE | re.DOTALL)
                return m.group(1).strip() if m else None
            q_match = re.search(r"Query\s*:\s*\n+(.*?)(?:\n+Back to Top|$)", blk, re.IGNORECASE | re.DOTALL)
            q_text = clean_query_blob(q_match.group(1).strip()) if q_match else None
            if q_text:
                items.append(
                    {
                        "idx": re.match(r"^\s*(\d+)", blk).group(1) if re.match(r"^\s*(\d+)", blk) else None,
                        "summary": find("Summary") or "",
                        "name": find("Name") or "",
                        "category": find("Category") or "",
                        "email": find("Email") or "",
                        "media_outlet": find("Media Outlet") or "",
                        "deadline": find("Deadline") or "",
                        "query": q_text,
                    }
                )

    return items


def _parse_help_b2b_writer(body_text: str) -> Dict[str, Optional[str]]:
    def find_one(label: str) -> Optional[str]:
        m = re.search(rf"^\s*{re.escape(label)}\s*:\s*(.+)$", body_text, re.IGNORECASE | re.MULTILINE)
        return m.group(1).strip() if m else None

    title = find_one("Title")
    writer = find_one("Writer")
    publication = find_one("Publication")
    deadline = find_one("Deadline")
    # Writer's Request block
    req_match = re.search(r"Writer's Request:\s*(.+?)(?:\n\nDeadline:|\Z)", body_text, re.IGNORECASE | re.DOTALL)
    request_text = req_match.group(1).strip() if req_match else body_text
    # Reply email
    em = re.search(r"email the writer\s*:\s*(\S+@helpab2bwriter\.com)", body_text, re.IGNORECASE)
    reply_email = em.group(1).strip() if em else ""
    return {
        "summary": title or "",
        "name": writer or "",
        "category": find_one("Industries") or "",
        "email": reply_email,
        "media_outlet": publication or "",
        "deadline": deadline or "",
        "query": request_text,
    }


def parse_email_to_requests(msg: Dict[str, Any]) -> List[ParsedRequest]:
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])

    subject = extract_header(headers, "Subject") or "(no subject)"
    from_header = extract_header(headers, "From") or ""
    reply_to_header = extract_header(headers, "Reply-To") or from_header
    date_header = extract_header(headers, "Date")

    sender_name, sender_email = parse_address(from_header)
    _, reply_to_email = parse_address(reply_to_header)

    received_at = ""
    try:
        if date_header:
            received_dt = email.utils.parsedate_to_datetime(date_header)
            received_at = received_dt.isoformat()
    except Exception:
        received_at = dt.datetime.utcnow().isoformat()

    text_body, html_body = decode_email_body(payload)
    body_text = text_body.strip() or html_to_text(html_body)

    provider = _detect_provider(subject, headers, body_text)

    requests: List[ParsedRequest] = []
    if provider == "HARO":
        # Parse with regex; if Gemini filtering is enabled, annotate items with analysis
        items = _parse_haro_queries(body_text)
        if USE_GEMINI_FILTERING:
            enriched_items: List[Dict[str, Optional[str]]] = []
            for it in items:
                query_text = it.get("query") or ""
                summary_text = it.get("summary") or ""
                category_text = it.get("category") or ""
                is_relevant, analysis = should_include_query_gemini(query_text, summary_text, category_text)
                it["gemini_analysis"] = analysis
                it["gemini_relevant"] = is_relevant
                enriched_items.append(it)
            items = enriched_items
        for i, it in enumerate(items, start=1):
            # Apply keyword filters
            blob = " ".join(
                [
                    it.get("summary") or "",
                    it.get("category") or "",
                    it.get("media_outlet") or "",
                    it.get("query") or "",
                ]
            )
            # Prefer Gemini decision when available; otherwise fallback to keyword/Gemini blob analysis
            if it.get("gemini_relevant") is True:
                pass
            elif it.get("gemini_relevant") is False:
                continue
            elif not _should_include_haro_query(blob):
                continue

            # Build ParsedRequest for each query, set reply_to to HARO per-query email
            deadline = it.get("deadline") or extract_first(DEADLINE_PATTERNS, body_text)
            pr = ParsedRequest(
                subject=f"HARO: {it.get('summary') or subject}",
                sender=sender_name,
                sender_email=sender_email,
                reply_to=(it.get("email") or reply_to_email or sender_email),
                received_at=received_at,
                deadline=deadline,
                requirements=None,
                query_text=it.get("query") or body_text,
                original_headers={h.get("name"): h.get("value") for h in headers},
                gmail_message_id=f"{msg.get('id','')}::q{i}",
                gmail_thread_id=msg.get("threadId", ""),
                summary=it.get("summary") or None,
                category=it.get("category") or None,
                media_outlet=it.get("media_outlet") or None,
                provider="HARO",
                query_index=i,
                requester_name=it.get("name") or None,
            )
            # Attach Gemini analysis to the request object for display in Telegram
            if it.get("gemini_analysis"):
                setattr(pr, "gemini_analysis", it.get("gemini_analysis"))
            requests.append(pr)
        if not requests:
            logger.info("HARO digest contained no queries matching filters; skipping")
        return requests

    if provider == "HELP_A_B2B_WRITER":
        it = _parse_help_b2b_writer(body_text)
        pr2 = ParsedRequest(
            subject=f"Help A B2B Writer: {it.get('summary') or subject}",
            sender=sender_name,
            sender_email=sender_email,
            reply_to=(it.get("email") or reply_to_email or sender_email),
            received_at=received_at,
            deadline=it.get("deadline"),
            requirements=None,
            query_text=it.get("query") or body_text,
            original_headers={h.get("name"): h.get("value") for h in headers},
            gmail_message_id=msg.get("id", ""),
            gmail_thread_id=msg.get("threadId", ""),
            summary=it.get("summary") or None,
            category=it.get("category") or None,
            media_outlet=it.get("media_outlet") or None,
            provider="HELP_A_B2B_WRITER",
            query_index=1,
        )
        return [pr2]

    # Default: treat whole email as single request
    deadline = extract_first(DEADLINE_PATTERNS, body_text)
    requirements = extract_first(REQUIREMENTS_PATTERNS, body_text)
    query_text = extract_first(QUERY_PATTERNS, body_text)
    if not query_text:
        query_text = body_text[:400].strip()
    parsed = ParsedRequest(
        subject=subject,
        sender=sender_name,
        sender_email=sender_email,
        reply_to=reply_to_email,
        received_at=received_at,
        deadline=deadline,
        requirements=requirements,
        query_text=body_text,
        original_headers={h.get("name"): h.get("value") for h in headers},
        gmail_message_id=msg.get("id", ""),
        gmail_thread_id=msg.get("threadId", ""),
        provider=None,
    )
    return [parsed]


def upsert_request(parsed: ParsedRequest) -> int:
    existing = db_query_one(
        "SELECT id FROM requests WHERE gmail_message_id = ?", (parsed.gmail_message_id,)
    )
    now = dt.datetime.utcnow().isoformat()
    if existing:
        request_id = int(existing["id"])
        db_execute(
            """
            UPDATE requests SET subject=?, sender=?, sender_email=?, reply_to=?, received_at=?, deadline=?,
                requirements=?, query_text=?, status=?, original_headers=?, gmail_thread_id=?, updated_at=?
            WHERE id=?
            """,
            (
                parsed.subject,
                parsed.sender,
                parsed.sender_email,
                parsed.reply_to,
                parsed.received_at,
                parsed.deadline,
                parsed.requirements,
                parsed.query_text,
                "new",
                json.dumps(parsed.original_headers),
                parsed.gmail_thread_id,
                now,
                request_id,
            ),
        )
        return request_id
    else:
        db_execute(
            """
            INSERT INTO requests (
                gmail_message_id, gmail_thread_id, subject, sender, sender_email, reply_to, received_at,
                deadline, requirements, query_text, status, original_headers, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                parsed.gmail_message_id,
                parsed.gmail_thread_id,
                parsed.subject,
                parsed.sender,
                parsed.sender_email,
                parsed.reply_to,
                parsed.received_at,
                parsed.deadline,
                parsed.requirements,
                parsed.query_text,
                "new",
                json.dumps(parsed.original_headers),
                now,
                now,
            ),
        )
        row = db_query_one("SELECT last_insert_rowid() AS id")
        return int(row["id"])  # type: ignore[index]


# ------------------------------
# Gemini Draft Generation
# ------------------------------


def load_prompt_template() -> str:
    template_path = os.getenv(
        "GEMINI_PROMPT_TEMPLATE_PATH", "templates/gemini_prompt_template.md"
    )
    if os.path.exists(template_path):
        with open(template_path, "r", encoding="utf-8") as f:
            return f.read()
    # Fallback default
    return textwrap.dedent(
        """
        You are Bezal John Benny, Founder of Mavericks Edge — a consulting firm based in Edmonton, Alberta, founded in 2017. You respond to media/source requests with grounded, experience-led insight.

        Company context (for credibility and examples when relevant):
        - Mavericks Edge helps solopreneurs, SMBs, nonprofits, and early-stage organizations thrive by blending human-centered consulting with cutting-edge AI and automation.
        - We build custom web applications, immersive 3D websites, and ecommerce platforms that drive measurable results in sales and engagement.
        - Full-service digital marketing includes SEO, PPC, and social — focused on visibility, trust, and conversions.
        - AI is woven into delivery: intelligent chatbots and workflow automation that cut costs and free teams to focus on what matters.
        - We create adaptive digital ecosystems that learn, optimize, and grow alongside the business, from concept to launch to long-term support.

        About Bezal (use briefly when it bolsters relevance):
        - BSc in Music Technology (Birmingham City University) and MSc (University of Victoria).
        - 10+ years bridging creativity and technology across large-scale technical installs and AI-driven web, marketing, and automation.
        - Philosophy: technology should amplify human potential; design solutions that feel authentic, purposeful, and effective.

        Input:
        - Request subject: {{subject}}
        - Request sender: {{sender}} <{{sender_email}}>
        - Deadline (if any): {{deadline}}
        - Requirements (if any): {{requirements}}
        - Full request text:
        ---
        {{query_text}}
        ---

        Task:
        - Draft a concise, credible response that demonstrates expertise and relevance.
        - Include a compelling subject line tailored to the query.
        - Use a polite, professional tone with quick skimmable structure (short paragraphs; no bullets or bold).
        - Provide 2-4 specific, insightful points tied to the query.
        - Proof: include one proof point (metric, brief case note) tied to Mavericks Edge/Bezal when relevant.
        - Plain text: no attachments; max one link only if essential.
        - Close with a direct follow-up invitation (email only).
        - Keep to 150-250 words in the body unless complexity requires more. No more than 2 paragraphs.
        - Keep JSON schema strict: subject, body (no extra keys).
        - Stay within anti-AI style rules (already defined below).

        Hard constraints (do not violate):
        - Do NOT include a salutation or sign-off/signature; those are inserted by the system.
        - Do NOT use markdown formatting (no **bold**, lists, or headers). Plain text only.

        Style constraints (avoid AI telltales):
        - Vary sentence length; include at least one short punchy line.
        - Limit em dashes — prefer commas or parentheses; no more than one em dash total.
        - No formulaic openers (e.g., "In today's fast-paced world", "It's no secret that").
        - Minimize hedging: avoid phrases like "it's important to note", "in many ways", "often" at sentence starts.
        - Use natural transitions; avoid "Additionally", "Moreover", "On the other hand" at sentence starts.
        - Keep bullets uneven (2–4 items max) and concise; no subheadings.
        - Prefer contractions (it's, we're, don't) where natural.
        - Avoid predictable closers (no "In conclusion"/"Ultimately"). End plainly.
        - Avoid over-enthusiastic adjectives (e.g., incredible, transformative, exciting) unless directly quoted.
        - Use specific, non-generic examples; skip default big-tech examples unless the query mentions them.
        - Allow a light, opinionated stance when appropriate (e.g., "this trade-off hurts small teams").
        - Avoid repeating the same idea in different words; remove restatements.

        Output JSON exactly with keys: subject, body
        """
    ).strip()


def interpolate_template(template: str, variables: Dict[str, str]) -> str:
    result = template
    for key, value in variables.items():
        result = result.replace("{{" + key + "}}", value or "")
    return result


@retry(reraise=True, stop=stop_after_attempt(4), wait=wait_exponential(min=2, max=20))
def generate_draft_with_gemini(parsed: ParsedRequest) -> Tuple[str, str]:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set")
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)

    template = load_prompt_template()
    prompt = interpolate_template(
        template,
        {
            "subject": parsed.subject,
            "sender": parsed.sender,
            "sender_email": parsed.sender_email,
            "deadline": parsed.deadline or "",
            "requirements": parsed.requirements or "",
            "query_text": parsed.query_text,
        },
    )

    logger.info("Generating draft with Gemini model=%s", GEMINI_MODEL)
    resp = model.generate_content(prompt)
    text = resp.text or ""

    # Expect JSON with subject/body; but handle plain text fallback
    subj = "Re: " + parsed.subject
    body = text.strip()
    try:
        # Clean fenced code blocks if present
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        data = json.loads(cleaned)
        subj = data.get("subject") or subj
        body = data.get("body") or body
    except Exception:
        pass

    # Ensure greeting personalization and required signature
    first_name = (parsed.requester_name or "").split()[0] if (parsed.requester_name or "").strip() else None
    if first_name:
        greeting_options = [f"Hello {first_name}!", f"Hi {first_name},"]
        greeting = random.choice(greeting_options)
    else:
        greeting_options = ["Hello!", "Hi there!"]
        greeting = random.choice(greeting_options)

    # Post-process to humanize style and avoid AI telltales
    def _humanize(text: str) -> str:
        original = text
        # Replace formulaic openers and stock phrases
        replacements = {
            r"\bIn today's (?:fast-paced|ever[- ]changing) world\b": "",
            r"\bIt's no secret that\b": "",
            r"\bAt the end of the day\b": "",
            r"\bUltimately,\b": "",
            r"\bIn conclusion,\b": "",
            r"\bAdditionally,\b": "",
            r"\bMoreover,\b": "",
            r"\bOn the other hand,\b": "",
            r"\bIt is important to note that\b": "",
        }
        for pat, rep in replacements.items():
            text = re.sub(pat, rep, text, flags=re.IGNORECASE)

        # Tone down over-enthusiastic adjectives unless part of a quote
        exuberant = [
            "incredible", "transformative", "exciting", "revolutionary", "game-changing",
            "unprecedented", "amazing", "remarkable", "cutting-edge",
        ]
        for w in exuberant:
            text = re.sub(rf"(?<![\"'])\b{w}\b(?![\"'])", "strong", text, flags=re.IGNORECASE)

        # Reduce brand-name clichés if not present in the query
        big_brands = ["Tesla", "Apple", "Google", "Amazon", "Microsoft"]
        for b in big_brands:
            if b.lower() not in (parsed.query_text or "").lower():
                text = re.sub(rf"\b{re.escape(b)}\b", "a well-known player", text)

        # Remove near-duplicate consecutive sentences to fight restating
        sentences = re.split(r"(?<=[.!?])\s+", text)
        dedup: list[str] = []
        seen = set()
        for s in sentences:
            key = re.sub(r"\W+", " ", s.strip().lower())
            key = " ".join(key.split())
            if len(key) > 0 and key not in seen:
                dedup.append(s)
                seen.add(key)
        if len(dedup) >= 2:
            text = " ".join(dedup)

        # Limit em dashes: replace excessive — with commas/parentheses
        if text.count("—") > 1:
            text = text.replace("—", "—", 1)
            text = text.replace("—", ",")

        # Vary bullets: trim to 2-4 uneven bullets and vary lengths
        lines = text.splitlines()
        in_bullets = False
        bullets: list[str] = []
        start_idx = -1
        for i, ln in enumerate(lines):
            if re.match(r"^\s*[-*] ", ln):
                if not in_bullets:
                    in_bullets = True
                    start_idx = i
                bullets.append(ln)
            else:
                if in_bullets:
                    # process block
                    processed = bullets[:]
                    if len(processed) > 4:
                        processed = processed[:3]
                    # jitter: append ellipsis on one, shorten another
                    if processed:
                        processed[0] = re.sub(r"\.?$", ".", processed[0])
                    if len(processed) >= 2:
                        processed[1] = re.sub(r"\.?$", "…", processed[1])
                    lines[start_idx:i] = processed
                    # reset
                    bullets = []
                    in_bullets = False
            
        if in_bullets:
            processed = bullets[:]
            if len(processed) > 4:
                processed = processed[:3]
            if processed:
                processed[0] = re.sub(r"\.?$", ".", processed[0])
            if len(processed) >= 2:
                processed[1] = re.sub(r"\.?$", "…", processed[1])
            lines[start_idx:] = processed

        text = "\n".join(lines)

        # Encourage contractions
        text = re.sub(r"\bdo not\b", "don't", text, flags=re.IGNORECASE)
        text = re.sub(r"\bis not\b", "isn't", text, flags=re.IGNORECASE)
        text = re.sub(r"\bwe are\b", "we're", text, flags=re.IGNORECASE)
        text = re.sub(r"\bit is\b", "it's", text, flags=re.IGNORECASE)

        # Insert one short punchy sentence near the top if too uniform
        sentences = re.split(r"(?<=[.!?])\s+", text)
        if 2 <= len(sentences) <= 8:
            avg = sum(len(s) for s in sentences) / max(1, len(sentences))
            if all(10 < len(s) < 220 for s in sentences) and avg > 80:
                sentences.insert(1, "Quick take: here’s the gist.")
                text = " ".join(s.strip() for s in sentences)

        # Remove overly polished endings
        text = re.sub(r"\n*(In conclusion|Ultimately)[^\n]*$", "", text, flags=re.IGNORECASE)
        return text.strip() or original

    body = _humanize(body)

    # Normalize: strip LLM greeting/signature, enforce plain text 1–2 paragraphs, add closing and fixed signature
    def _strip_llm_greeting(text: str) -> str:
        t = text.lstrip()
        # Remove up to two leading greeting lines like "Hi ...,"/"Hello ..."/"Dear ..."
        for _ in range(2):
            m = re.match(r"^(?:hi|hello|hey|dear|greetings)[^\n]*\n+", t, flags=re.IGNORECASE)
            if not m:
                break
            t = t[m.end():]
        return t.lstrip()

    def _strip_llm_signature(text: str) -> str:
        t = text.rstrip()
        # Remove common sign-off blocks starting with regards/sincerely/etc to end
        signoff = re.search(r"\n\s*(best regards|regards|sincerely|thanks|thank you)[^\n]*$", t, flags=re.IGNORECASE)
        if signoff:
            t = t[: signoff.start()] .rstrip()
        # Heuristic: drop trailing block that looks like a signature (name/title/email/phone)
        tail = t.splitlines()
        drop_idx = None
        for i in range(len(tail) - 1, max(-1, len(tail) - 6), -1):
            ln = tail[i]
            if re.search(r"@|\+\d|mavericksedge|founder|bezal", ln, flags=re.IGNORECASE):
                drop_idx = i
        if drop_idx is not None:
            t = "\n".join(tail[:drop_idx]).rstrip()
        return t

    def _remove_markdown_and_bullets(text: str) -> str:
        # Remove bold/italics markers
        t = re.sub(r"(\*\*|__)(.*?)\\1", r"\\2", text)
        t = re.sub(r"(\*|_)(.*?)\\1", r"\\2", t)
        # Convert bullets to plain sentences (strip markers)
        lines = []
        for ln in t.splitlines():
            ln2 = re.sub(r"^\s*([\-*•]|\d+\.)\s+", "", ln)
            lines.append(ln2)
        t = "\n".join(lines)
        # Collapse multiple blank lines
        t = re.sub(r"\n\s*\n+", "\n\n", t)
        return t.strip()

    def _limit_to_two_paragraphs(text: str) -> str:
        paras = [p.strip() for p in re.split(r"\n\n+", text) if p.strip()]
        if not paras:
            return text.strip()
        if len(paras) <= 2:
            return "\n\n".join(paras)
        # Keep first two; squash the rest into the second
        merged_second = paras[1] + " " + " ".join(paras[2:])
        return paras[0] + "\n\n" + re.sub(r"\s+", " ", merged_second).strip()

    body = _strip_llm_greeting(body)
    body = _strip_llm_signature(body)
    body = _remove_markdown_and_bullets(body)
    body = _limit_to_two_paragraphs(body)

    # Prepend our greeting unconditionally
    body = f"{greeting}\n\n{body}".strip()

    # Ensure proper closing on its own line
    if not re.search(r"\n\s*Best regards,\s*$", body, flags=re.IGNORECASE):
        body = body.rstrip() + "\n\nBest regards,"

    # Append standardized signature with URL next to brand (no brackets)
    signature = (
        "\n\nBezal John Benny\n"
        "Founder | Mavericks Edge — https://mavericksedge.ca/\n"
        "bezal.benny@mavericksedge.ca\n"
        "C: +1 (250) 883-8849"
    )
    if signature.strip() not in body:
        body = body.rstrip() + signature

    return subj.strip(), body.strip()


def save_draft(request_id: int, subject: str, body: str) -> int:
    now = dt.datetime.utcnow().isoformat()
    db_execute(
        """
        INSERT INTO drafts (request_id, subject, body, model, approved, created_at, updated_at)
        VALUES (?, ?, ?, ?, 0, ?, ?)
        """,
        (request_id, subject, body, GEMINI_MODEL, now, now),
    )
    row = db_query_one("SELECT last_insert_rowid() AS id")
    draft_id = int(row["id"])  # type: ignore[index]
    db_execute(
        "UPDATE requests SET status=?, updated_at=? WHERE id=?",
        ("drafted", now, request_id),
    )
    log_action(request_id, "draft_created", json.dumps({"draft_id": draft_id}))
    return draft_id


# ------------------------------
# Telegram Integration
# ------------------------------


def build_review_message_text(parsed: ParsedRequest, subject: str, body: str) -> str:
    # Enhanced header with more context
    provider_info = f"Provider: {parsed.provider}\n" if parsed.provider else ""
    name_info = f"Name: {parsed.requester_name}\n" if parsed.requester_name else ""
    category_info = f"Category: {parsed.category}\n" if parsed.category else ""
    media_info = f"Media Outlet: {parsed.media_outlet}\n" if parsed.media_outlet else ""
    reply_to_info = f"Reply-to: {parsed.reply_to}\n" if parsed.reply_to else ""
    header = (
        f"🤖 AI-Powered Source Request\n"
        f"{provider_info}{name_info}{category_info}{media_info}From: {parsed.sender} <{parsed.sender_email}>\n"
        f"{reply_to_info}Deadline: {parsed.deadline or 'n/a'}\n\n"
    )

    # Show full query text (not just summary), as requested
    query_section = f"Query:\n{parsed.query_text}\n\n"

    # Add Gemini analysis info if available (trim to 2 sentences)
    gemini_info = ""
    if USE_GEMINI_FILTERING and hasattr(parsed, 'gemini_analysis'):
        analysis = parsed.gemini_analysis
        reasoning = analysis['reasoning']
        # Keep only first two sentences
        parts = re.split(r"(?<=[.!?])\s+", reasoning)
        trimmed = " ".join(parts[:2]).strip()
        gemini_info = (
            f"🧠 AI Analysis: {trimmed}\n"
            f"📊 Relevance Score: {analysis['relevance_score']:.2f}\n"
            f"🎯 Topics: {', '.join(analysis['matching_topics'])}\n\n"
        )

    draft = f"Proposed Subject:\n{subject}\n\nProposed Body:\n{body}"
    text = header + query_section + gemini_info + draft

    # Telegram max length constraint handling
    if len(text) <= MAX_TELEGRAM_MESSAGE_CHARS:
        return text
    truncated = text[: MAX_TELEGRAM_MESSAGE_CHARS - 100] + "\n\n…[truncated]"
    return truncated


def review_keyboard(request_id: int) -> InlineKeyboardMarkup:
    approve = InlineKeyboardButton("✅ Approve & Send", callback_data=f"approve:{request_id}")
    edit = InlineKeyboardButton("✏️ Edit Draft", callback_data=f"edit:{request_id}")
    reject = InlineKeyboardButton("❌ Reject", callback_data=f"reject:{request_id}")
    return InlineKeyboardMarkup([[approve], [edit], [reject]])


async def telegram_send_review(
    app, parsed: ParsedRequest, request_id: int, subject: str, body: str
) -> Optional[int]:
    try:
        text = build_review_message_text(parsed, subject, body)
        sent = await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            reply_markup=review_keyboard(request_id),
        )
        db_execute(
            "INSERT INTO telegram_messages (request_id, chat_id, message_id, created_at) VALUES (?, ?, ?, ?)",
            (
                request_id,
                str(TELEGRAM_CHAT_ID),
                int(sent.message_id),
                dt.datetime.utcnow().isoformat(),
            ),
        )
        db_execute(
            "UPDATE requests SET status=?, updated_at=? WHERE id=?",
            ("pending_review", dt.datetime.utcnow().isoformat(), request_id),
        )
        log_action(request_id, "telegram_review_sent", json.dumps({"message_id": sent.message_id}))
        return int(sent.message_id)
    except Exception as e:
        logger.exception("Failed to send review to Telegram: %s", e)
        return None


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query:
        return
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    try:
        action, request_id_str = data.split(":", 1)
    except ValueError:
        return
    request_id = int(request_id_str)

    if action == "approve":
        await handle_approve(update, context, request_id)
    elif action == "reject":
        await handle_reject(update, context, request_id)
    elif action == "edit":
        await handle_edit(update, context, request_id)


async def handle_approve(update: Update, context: ContextTypes.DEFAULT_TYPE, request_id: int) -> None:
    req = db_query_one("SELECT * FROM requests WHERE id=?", (request_id,))
    draft = db_query_one(
        "SELECT * FROM drafts WHERE request_id=? ORDER BY id DESC LIMIT 1", (request_id,)
    )
    if not req or not draft:
        await update.effective_message.reply_text("Draft not found.")
        return

    try:
        service = await asyncio.to_thread(get_gmail_service)
        await asyncio.to_thread(
            send_email_reply,
            service,
            req,
            draft["subject"],
            draft["body"],
        )
        now = dt.datetime.utcnow().isoformat()
        db_execute("UPDATE drafts SET approved=1, updated_at=? WHERE id=?", (now, int(draft["id"])))
        db_execute("UPDATE requests SET status=?, updated_at=? WHERE id=?", ("sent", now, request_id))
        log_action(request_id, "approved_and_sent", "")
        await update.effective_message.reply_text("✅ Sent reply via Gmail.")
    except Exception as e:
        logger.exception("Failed to send email: %s", e)
        log_action(request_id, "send_failed", str(e))
        await update.effective_message.reply_text(f"❌ Failed to send: {e}")


async def handle_reject(update: Update, context: ContextTypes.DEFAULT_TYPE, request_id: int) -> None:
    db_execute(
        "UPDATE requests SET status=?, updated_at=? WHERE id=?",
        ("rejected", dt.datetime.utcnow().isoformat(), request_id),
    )
    log_action(request_id, "rejected", "")
    await update.effective_message.reply_text("❌ Rejected. No reply will be sent.")


async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, request_id: int) -> None:
    # Mark pending edit state
    db_execute(
        "INSERT OR IGNORE INTO pending_edits (chat_id, request_id) VALUES (?, ?)",
        (str(update.effective_chat.id), request_id),
    )
    await update.effective_message.reply_text(
        textwrap.dedent(
            """
            ✏️ Send the updated draft as a single message in this format:
            Subject: <your subject>

            Body:
            <your body>

            Tip: You can reply to the original draft message to keep context.
            """
        ).strip()
    )


def parse_subject_body_from_text(text: str) -> Tuple[Optional[str], Optional[str]]:
    subj_match = re.search(r"^\s*Subject\s*:\s*(.+)$", text, re.IGNORECASE | re.MULTILINE)
    body_match = re.search(r"^\s*Body\s*:\s*(.+)$", text, re.IGNORECASE | re.MULTILINE | re.DOTALL)
    subject = subj_match.group(1).strip() if subj_match else None
    body = body_match.group(1).strip() if body_match else None
    return subject, body


async def handle_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = str(update.effective_chat.id)
    row = db_query_one("SELECT request_id FROM pending_edits WHERE chat_id=?", (chat_id,))
    if not row:
        return  # Not in edit flow
    request_id = int(row["request_id"])  # type: ignore[index]
    text = update.message.text or ""
    subject, body = parse_subject_body_from_text(text)
    if not subject or not body:
        await update.message.reply_text("Please include both 'Subject:' and 'Body:' sections.")
        return

    db_execute(
        "UPDATE drafts SET subject=?, body=?, updated_at=? WHERE request_id=?",
        (subject, body, dt.datetime.utcnow().isoformat(), request_id),
    )
    db_execute("DELETE FROM pending_edits WHERE chat_id=? AND request_id=?", (chat_id, request_id))
    req = db_query_one("SELECT * FROM requests WHERE id=?", (request_id,))
    if req:
        parsed = ParsedRequest(
            subject=req["subject"],
            sender=req["sender"],
            sender_email=req["sender_email"],
            reply_to=req["reply_to"],
            received_at=req["received_at"],
            deadline=req["deadline"],
            requirements=req["requirements"],
            query_text=req["query_text"],
            original_headers=json.loads(req["original_headers"]) if req["original_headers"] else {},
            gmail_message_id=req["gmail_message_id"],
            gmail_thread_id=req["gmail_thread_id"],
        )
        # Update the last Telegram message to reflect the new draft
        last_msg = db_query_one(
            "SELECT message_id FROM telegram_messages WHERE request_id=? ORDER BY id DESC LIMIT 1",
            (request_id,),
        )
        if last_msg:
            try:
                await context.bot.edit_message_text(
                    chat_id=TELEGRAM_CHAT_ID,
                    message_id=int(last_msg["message_id"]),
                    text=build_review_message_text(parsed, subject, body),
                    reply_markup=review_keyboard(request_id),
                )
            except Exception:
                # If edit fails (too old), send a new message
                await telegram_send_review(context.application, parsed, request_id, subject, body)
    await update.message.reply_text("✅ Draft updated.")


# ------------------------------
# Gmail Reply Sending
# ------------------------------


def build_reply_message(
    to_addr: str,
    from_addr: str,
    subject: str,
    body: str,
    in_reply_to: Optional[str],
    references: Optional[str],
) -> EmailMessage:
    msg = EmailMessage(policy=email.policy.default)
    msg["To"] = to_addr
    msg["From"] = from_addr
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)
    return msg


@retry(reraise=True, stop=stop_after_attempt(5), wait=wait_exponential(min=2, max=30))
def send_email_reply(
    service: Any,
    req_row: sqlite3.Row,
    subject: str,
    body: str,
) -> None:
    headers = json.loads(req_row["original_headers"]) if req_row["original_headers"] else {}
    to_addr = headers.get("Reply-To") or headers.get("From")
    if not to_addr:
        raise RuntimeError("No reply address found")

    in_reply_to = headers.get("Message-Id") or headers.get("Message-ID")
    references = in_reply_to

    msg = build_reply_message(
        to_addr=to_addr,
        from_addr="me",
        subject=(subject if subject.lower().startswith("re:") else f"Re: {req_row['subject']}") or req_row["subject"],
        body=body,
        in_reply_to=in_reply_to,
        references=references,
    )

    encoded = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

    send_body: Dict[str, Any] = {"raw": encoded}
    if req_row["gmail_thread_id"]:
        send_body["threadId"] = req_row["gmail_thread_id"]

    sent = (
        service.users()
        .messages()
        .send(userId="me", body=send_body)
        .execute()
    )
    logger.info("Sent Gmail reply, id=%s", sent.get("id"))


# ------------------------------
# Polling Logic
# ------------------------------


async def poll_gmail_and_process(app) -> None:
    try:
        logger.info("Polling Gmail for unread emails with label '%s'", GMAIL_LABEL_NAME)
        service = await asyncio.to_thread(get_gmail_service)
        label_id = await asyncio.to_thread(gmail_get_label_id, service, GMAIL_LABEL_NAME)
        ids = await asyncio.to_thread(gmail_list_messages, service, label_id, unread_only=True)
        
        if not ids:
            logger.info("No unread emails found with label '%s'", GMAIL_LABEL_NAME)
            return
            
        logger.info("Found %d unread emails to process", len(ids))
        for mid in ids:
            logger.info("Processing email ID: %s", mid)
            exists = db_query_one("SELECT id FROM requests WHERE gmail_message_id=?", (mid,))
            if exists:
                logger.info("Email %s already exists in database, but still unread in Gmail. Marking as read...", mid)
                await asyncio.to_thread(gmail_mark_as_read, service, mid)
                logger.info("Marked existing email %s as read", mid)
                continue
            try:
                msg = await asyncio.to_thread(gmail_get_message, service, mid)
                parsed_list = parse_email_to_requests(msg)
                
                if not parsed_list:
                    # Mark as read even if no relevant queries found
                    await asyncio.to_thread(gmail_mark_as_read, service, mid)
                    logger.info("Marked email %s as read (no relevant queries found)", mid)
                    continue
                    
                for parsed in parsed_list:
                    request_id = upsert_request(parsed)
                    log_action(request_id, "request_parsed", parsed.subject)
                    # Generate draft
                    subject, body = await asyncio.to_thread(generate_draft_with_gemini, parsed)
                    save_draft(request_id, subject, body)
                    # Send to Telegram for review
                    await telegram_send_review(app, parsed, request_id, subject, body)
                
                # Mark email as read after processing all queries in it
                await asyncio.to_thread(gmail_mark_as_read, service, mid)
                logger.info("Marked email %s as read after processing %d queries", mid, len(parsed_list))
            except Exception as e:
                logger.exception("Failed processing message %s: %s", mid, e)
                row = db_query_one("SELECT id FROM requests WHERE gmail_message_id=?", (mid,))
                if row:
                    db_execute(
                        "UPDATE requests SET status=?, updated_at=? WHERE id=?",
                        ("error", dt.datetime.utcnow().isoformat(), int(row["id"])),
                    )
    except Exception as e:
        logger.exception("Polling failed: %s", e)


# ------------------------------
# Telegram Bot App
# ------------------------------


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Bot running. Use inline buttons to review drafts.")


def run_bot() -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in environment")
        sys.exit(1)

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_text))

    # Job: poll Gmail periodically
    async def poll_job(context: ContextTypes.DEFAULT_TYPE):
        await poll_gmail_and_process(app)

    app.job_queue.run_repeating(poll_job, interval=POLL_INTERVAL_SECONDS, first=3)

    logger.info("Starting Telegram bot polling…")
    app.run_polling(close_loop=False)


# ------------------------------
# Entry Point
# ------------------------------


def main() -> None:
    init_db()
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
    else:
        logger.warning("GEMINI_API_KEY not set; draft generation will fail until set.")

    # Graceful shutdown on SIGINT/SIGTERM
    def _handle_sig(signum, frame):  # type: ignore[no-untyped-def]
        logger.info("Signal %s received, exiting…", signum)
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    run_bot()


if __name__ == "__main__":
    main()



# Import Gemini filtering
from gemini_filter import should_include_query_gemini, USE_GEMINI_FILTERING

# Update the filtering function
def _should_include_haro_query(blob: str) -> bool:
    """Enhanced HARO query filtering with Gemini AI analysis."""
    text = blob.lower()
    
    # If Gemini filtering is enabled, use AI analysis
    if USE_GEMINI_FILTERING:
        # Extract summary and category from the blob for better analysis
        summary = ""
        category = ""
        
        # Try to extract summary from common patterns
        summary_match = re.search(r"Summary:\s*(.+?)(?:\n|$)", blob, re.IGNORECASE)
        if summary_match:
            summary = summary_match.group(1).strip()
        
        # Try to extract category
        category_match = re.search(r"Category:\s*(.+?)(?:\n|$)", blob, re.IGNORECASE)
        if category_match:
            category = category_match.group(1).strip()
        
        # Use Gemini for intelligent analysis
        is_relevant, analysis = should_include_query_gemini(blob, summary, category)
        
        logger.info(f"Gemini analysis result: {analysis['reasoning']}")
        logger.info(f"Relevance score: {analysis['relevance_score']:.2f}, Confidence: {analysis['confidence']:.2f}")
        
        return is_relevant
    
    # Fallback to traditional keyword matching
    if HARO_INCLUDE_KEYWORDS:
        if not any(kw in text for kw in HARO_INCLUDE_KEYWORDS):
            return False
    if HARO_EXCLUDE_KEYWORDS:
        if any(kw in text for kw in HARO_EXCLUDE_KEYWORDS):
            return False
    return True

def build_review_message_text(parsed: ParsedRequest, subject: str, body: str) -> str:
    """Enhanced review message with Gemini analysis results."""
    # Enhanced header with more context
    provider_info = f"Provider: {parsed.provider}\n" if parsed.provider else ""
    category_info = f"Category: {parsed.category}\n" if parsed.category else ""
    media_info = f"Media Outlet: {parsed.media_outlet}\n" if parsed.media_outlet else ""
    
    header = f"🤖 AI-Powered Source Request\n{provider_info}{category_info}{media_info}From: {parsed.sender} <{parsed.sender_email}>\nDeadline: {parsed.deadline or 'n/a'}\n\n"
    
    # Include summary if available
    summary_info = f"Summary: {parsed.summary}\n\n" if parsed.summary else ""
    
    # Add Gemini analysis info if available
    gemini_info = ""
    if USE_GEMINI_FILTERING and hasattr(parsed, 'gemini_analysis'):
        analysis = parsed.gemini_analysis
        gemini_info = f"🧠 AI Analysis: {analysis['reasoning']}\n📊 Relevance Score: {analysis['relevance_score']:.2f}\n🎯 Topics: {', '.join(analysis['matching_topics'])}\n\n"
    
    draft = f"{summary_info}{gemini_info}Proposed Subject:\n{subject}\n\nProposed Body:\n{body}"
    text = header + draft
    
    # Telegram max length constraint handling
    if len(text) <= MAX_TELEGRAM_MESSAGE_CHARS:
        return text
    truncated = text[: MAX_TELEGRAM_MESSAGE_CHARS - 100] + "\n\n…[truncated]"
    return truncated

# Update the HARO parsing to include Gemini analysis
def _parse_haro_queries_with_gemini(body_text: str) -> List[Dict[str, Optional[str]]]:
    """Parse HARO digest email with Gemini analysis for each query."""
    items = _parse_haro_queries(body_text)
    
    if USE_GEMINI_FILTERING:
        for item in items:
            # Analyze each query with Gemini
            query_text = item.get("query", "")
            summary = item.get("summary", "")
            category = item.get("category", "")
            
            is_relevant, analysis = should_include_query_gemini(query_text, summary, category)
            item["gemini_analysis"] = analysis
            item["gemini_relevant"] = is_relevant
    
    return items
