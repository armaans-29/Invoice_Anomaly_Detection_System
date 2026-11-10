# =====================================================================
# InvoiceShield AI — Prototype
# Invoice extraction, arithmetic/tax validation, duplicate detection,
# PO/GRN reconciliation, approval workflow and a hash-chained audit log.
#
# Run:
#   pip install -r requirements.txt
#   streamlit run InvoiceShield.py
#
# Optional environment variables:
#   INVOICE_SHIELD_ENCRYPTION_KEY  Fernet key; encrypts stored files and OCR text
#   INVOICE_SHIELD_DB_PATH         Custom SQLite path
#   INVOICE_SHIELD_UPLOAD_DIR      Where original uploads are kept
#   INVOICE_SHIELD_MAX_FILE_MB     Upload size limit (default 25)
#
# Generate an encryption key:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# =====================================================================

import hashlib
import html
import io
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape as xml_escape

import cv2
import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import invoice_core as core
from invoice_core import Invoice, LineItem, money, normalize, sha256

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

st.set_page_config(
    page_title="InvoiceShield AI",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

APP_VERSION = "5.0.0"
SCHEMA_VERSION = 5
MAX_FILE_MB = int(os.getenv("INVOICE_SHIELD_MAX_FILE_MB", "25"))
MAX_OCR_SIDE = int(os.getenv("INVOICE_SHIELD_MAX_OCR_SIDE", "3000"))
ALLOWED_TYPES = ["pdf", "png", "jpg", "jpeg"]

DB_PATH = os.getenv("INVOICE_SHIELD_DB_PATH", "invoiceshield_v5.db")
UPLOAD_DIR = Path(os.getenv("INVOICE_SHIELD_UPLOAD_DIR", "invoiceshield_uploads"))
ENCRYPTION_KEY = os.getenv("INVOICE_SHIELD_ENCRYPTION_KEY")

GREEN, AMBER, RED, BLUE = "#22C55E", "#F59E0B", "#EF4444", "#3B82F6"
PASSED = core.PASSED

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');
html, body, .stApp { background:#0B1220 !important; color:#FFFFFF !important; font-family:'Inter',sans-serif !important; }
section[data-testid="stSidebar"] { background:linear-gradient(180deg,#0A101B 0%,#101B2C 100%) !important; border-right:1px solid #243449 !important; }
section[data-testid="stSidebar"] * { color:#E8EEF5 !important; }
.block-container { padding-top:1.5rem; padding-bottom:3rem; max-width:1450px; }
h1,h2,h3,h4,h5,h6,p,span,label,div { color:#FFFFFF !important; }
.app-hero { background:radial-gradient(900px 300px at 8% -10%, rgba(0,194,168,.20), transparent), linear-gradient(120deg,#0B1220 0%,#12233A 60%,#0E2F33 100%); border:1px solid #243449; border-radius:20px; padding:1.7rem 1.8rem; box-shadow:0 18px 45px rgba(0,0,0,.35); }
.app-hero h1 { font-size:2.1rem; margin:0; }
.app-hero p { color:#C8D3DE !important; margin:.45rem 0 0; font-size:.95rem; }
.hero-badge { display:inline-flex; align-items:center; gap:.45rem; background:rgba(0,194,168,.12); border:1px solid rgba(0,194,168,.35); color:#00C2A8 !important; padding:.28rem .72rem; border-radius:999px; font-size:.74rem; font-weight:700; letter-spacing:.05em; text-transform:uppercase; }
.card { background:#111C2E; border:1px solid #243449; border-radius:16px; padding:1.1rem 1.2rem; box-shadow:0 10px 25px rgba(0,0,0,.18); }
.card-title { font-size:1rem; font-weight:750; margin-bottom:.25rem; }
.card-help { color:#A9B7C6 !important; font-size:.84rem; margin-bottom:.85rem; }
.kpi { background:linear-gradient(145deg,#111C2E 0%,#16233A 100%); border:1px solid #243449; border-radius:16px; padding:1rem 1.1rem; }
.kpi-label { color:#A9B7C6 !important; font-size:.72rem; font-weight:750; letter-spacing:.08em; text-transform:uppercase; }
.kpi-value { font-size:1.65rem; font-weight:800; margin-top:.25rem; }
.kpi-sub { color:#A9B7C6 !important; font-size:.76rem; margin-top:.2rem; }
.pill { display:inline-block; padding:.27rem .72rem; border-radius:999px; font-size:.74rem; font-weight:750; }
.pill-verified { background:rgba(34,197,94,.14); color:#4ADE80 !important; border:1px solid rgba(34,197,94,.35); }
.pill-review { background:rgba(245,158,11,.14); color:#FBBF24 !important; border:1px solid rgba(245,158,11,.35); }
.pill-high { background:rgba(239,68,68,.14); color:#F87171 !important; border:1px solid rgba(239,68,68,.35); }
.pill-info { background:rgba(59,130,246,.14); color:#93C5FD !important; border:1px solid rgba(59,130,246,.35); }
.finding { border-left:4px solid #00C2A8; background:rgba(255,255,255,.035); border:1px solid #243449; padding:.8rem .95rem; border-radius:10px; margin-bottom:.65rem; }
.finding-warning { border-left-color:#F59E0B; } .finding-error { border-left-color:#EF4444; }
.finding-success { border-left-color:#22C55E; } .finding-info { border-left-color:#3B82F6; }
.finding-title { font-weight:750; margin-bottom:.2rem; }
.finding-text { color:#D6E0EA !important; font-size:.87rem; }
.evidence { background:#0A1420; border:1px solid #243449; color:#CBD5E1 !important; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.76rem; padding:.6rem .75rem; border-radius:8px; white-space:pre-wrap; }
.step { display:flex; gap:.75rem; align-items:flex-start; padding:.65rem 0; border-bottom:1px solid rgba(255,255,255,.06); }
.step:last-child { border-bottom:none; }
.step-number { min-width:26px; height:26px; display:flex; align-items:center; justify-content:center; border-radius:50%; background:rgba(0,194,168,.15); color:#00C2A8 !important; font-weight:800; font-size:.76rem; }
.step-text { color:#D6E0EA !important; font-size:.87rem; }
.stButton > button { border-radius:10px !important; font-weight:650 !important; border:1px solid #243449 !important; }
div[data-testid="stDataFrame"] { border:1px solid #243449 !important; border-radius:12px !important; overflow:hidden; }
div[data-testid="stExpander"] { background:#111C2E; border:1px solid #243449; border-radius:12px; }
.small-muted { color:#A9B7C6 !important; font-size:.78rem; }
</style>
"""

st.markdown(CSS, unsafe_allow_html=True)

# ---------------------------------------------------------------------
# Encryption (files and OCR text, only when a key is configured)
# ---------------------------------------------------------------------


def _fernet():
    from cryptography.fernet import Fernet

    return Fernet(ENCRYPTION_KEY.encode())


if ENCRYPTION_KEY:
    try:
        _fernet()
    except Exception:
        st.error(
            "INVOICE_SHIELD_ENCRYPTION_KEY is not a valid Fernet key. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
        st.stop()


def encrypt_bytes(data: bytes) -> bytes:
    return _fernet().encrypt(data) if ENCRYPTION_KEY else data


def decrypt_bytes(data: bytes) -> bytes:
    return _fernet().decrypt(data) if ENCRYPTION_KEY else data


def protect_text(text: str) -> str:
    return encrypt_bytes(text.encode("utf-8")).decode("ascii") if ENCRYPTION_KEY else text


def reveal_text(stored: Optional[str]) -> str:
    if not stored:
        return ""
    if not ENCRYPTION_KEY:
        return stored
    try:
        return decrypt_bytes(stored.encode("ascii")).decode("utf-8")
    except Exception:
        return stored  # legacy plaintext row


# ---------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def conn_ctx():
    """Connection that commits on success, rolls back on error, always closes."""
    connection = db()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_database():
    with conn_ctx() as c:
        c.executescript(
            """
            PRAGMA journal_mode = WAL;

            CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);

            CREATE TABLE IF NOT EXISTS tenants (
                id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
                role TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY (tenant_id) REFERENCES tenants(id)
            );

            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, filename TEXT NOT NULL,
                file_hash TEXT NOT NULL, document_type TEXT, uploaded_at TEXT NOT NULL,
                page_count INTEGER, ocr_used INTEGER, raw_text TEXT,
                extraction_confidence REAL, currency TEXT, tax_regime TEXT, status TEXT,
                encrypted INTEGER, stored_path TEXT,
                UNIQUE(tenant_id, file_hash),
                FOREIGN KEY (tenant_id) REFERENCES tenants(id)
            );

            CREATE TABLE IF NOT EXISTS invoices (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, document_id TEXT NOT NULL,
                invoice_number TEXT, invoice_date TEXT, due_date TEXT, supplier_name TEXT,
                supplier_gstin TEXT, buyer_name TEXT, po_reference TEXT, currency TEXT,
                tax_regime TEXT, subtotal REAL, discount REAL, shipping REAL,
                tax_amount REAL, tax_rate REAL, cgst REAL, sgst REAL, igst REAL,
                final_amount REAL, extraction_confidence REAL, extraction_quality TEXT,
                notes TEXT, risk_score REAL, risk_level TEXT, approval_status TEXT,
                reviewed_by TEXT, reviewed_at TEXT, created_at TEXT NOT NULL,
                FOREIGN KEY (tenant_id) REFERENCES tenants(id),
                FOREIGN KEY (document_id) REFERENCES documents(id)
            );

            CREATE TABLE IF NOT EXISTS invoice_items (
                id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL, description TEXT, sku TEXT,
                hsn_sac TEXT, quantity REAL, unit TEXT, unit_price REAL, tax_rate REAL,
                line_total REAL, source_page INTEGER,
                FOREIGN KEY (invoice_id) REFERENCES invoices(id)
            );

            CREATE TABLE IF NOT EXISTS purchase_orders (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, po_number TEXT NOT NULL,
                supplier_name TEXT, currency TEXT, total_amount REAL, status TEXT,
                created_at TEXT NOT NULL, UNIQUE(tenant_id, po_number),
                FOREIGN KEY (tenant_id) REFERENCES tenants(id)
            );

            CREATE TABLE IF NOT EXISTS purchase_order_items (
                id TEXT PRIMARY KEY, po_id TEXT NOT NULL, description TEXT, sku TEXT,
                quantity REAL, unit_price REAL, tax_rate REAL, line_total REAL,
                FOREIGN KEY (po_id) REFERENCES purchase_orders(id)
            );

            CREATE TABLE IF NOT EXISTS goods_receipts (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, grn_number TEXT NOT NULL,
                po_number TEXT, supplier_name TEXT, receipt_date TEXT, created_at TEXT NOT NULL,
                UNIQUE(tenant_id, grn_number),
                FOREIGN KEY (tenant_id) REFERENCES tenants(id)
            );

            CREATE TABLE IF NOT EXISTS goods_receipt_items (
                id TEXT PRIMARY KEY, grn_id TEXT NOT NULL, description TEXT, sku TEXT,
                quantity_received REAL, unit_price REAL,
                FOREIGN KEY (grn_id) REFERENCES goods_receipts(id)
            );

            CREATE TABLE IF NOT EXISTS validation_results (
                id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL, check_name TEXT, result TEXT,
                severity TEXT, expected_value TEXT, actual_value TEXT, explanation TEXT,
                evidence TEXT, created_at TEXT NOT NULL,
                FOREIGN KEY (invoice_id) REFERENCES invoices(id)
            );

            CREATE TABLE IF NOT EXISTS duplicate_matches (
                id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL, matched_invoice_id TEXT,
                match_type TEXT, similarity REAL, reasons TEXT, created_at TEXT NOT NULL,
                FOREIGN KEY (invoice_id) REFERENCES invoices(id)
            );

            CREATE TABLE IF NOT EXISTS reconciliation_results (
                id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL, po_id TEXT, grn_id TEXT,
                reconciliation_type TEXT, result TEXT, severity TEXT, explanation TEXT,
                evidence TEXT, created_at TEXT NOT NULL,
                FOREIGN KEY (invoice_id) REFERENCES invoices(id)
            );

            CREATE TABLE IF NOT EXISTS risk_assessments (
                id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL, score REAL, classification TEXT,
                indicators TEXT, recommendation TEXT, created_at TEXT NOT NULL,
                FOREIGN KEY (invoice_id) REFERENCES invoices(id)
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id TEXT PRIMARY KEY, tenant_id TEXT, created_at TEXT NOT NULL, action TEXT,
                entity_type TEXT, entity_id TEXT, details TEXT, previous_hash TEXT, entry_hash TEXT
            );
            """
        )

        if not c.execute("SELECT version FROM schema_version LIMIT 1").fetchone():
            c.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            tenant_id = str(uuid.uuid4())
            c.execute(
                "INSERT INTO tenants (id, name, created_at) VALUES (?, ?, ?)",
                (tenant_id, "Default Organization", now_iso()),
            )
            c.execute(
                "INSERT INTO users (id, tenant_id, email, role, created_at) VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), tenant_id, "finance@invoiceshield.local",
                 "Finance Reviewer", now_iso()),
            )


def audit(action: str, entity_type: str, entity_id: str, details: Any, tenant_id: str):
    """Append a hash-chained audit entry: hash = SHA256(previous_hash + payload)."""
    with conn_ctx() as c:
        previous = c.execute(
            "SELECT entry_hash FROM audit_logs WHERE tenant_id = ? ORDER BY rowid DESC LIMIT 1",
            (tenant_id,),
        ).fetchone()
        previous_hash = previous["entry_hash"] if previous else "GENESIS"

        created_at = now_iso()
        payload = json.dumps(
            {
                "tenant_id": tenant_id,
                "created_at": created_at,
                "action": action,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "details": details,
            },
            default=str, sort_keys=True, ensure_ascii=False,
        )
        entry_hash = hashlib.sha256((previous_hash + payload).encode("utf-8")).hexdigest()

        c.execute(
            """
            INSERT INTO audit_logs
            (id, tenant_id, created_at, action, entity_type, entity_id, details,
             previous_hash, entry_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), tenant_id, created_at, action, entity_type,
             entity_id, payload, previous_hash, entry_hash),
        )


def verify_audit_chain(tenant_id: str) -> Tuple[bool, int, Optional[int]]:
    """Recompute every hash. Returns (intact, entries_checked, first_broken_entry_number)."""
    with conn_ctx() as c:
        rows = c.execute(
            "SELECT details, previous_hash, entry_hash FROM audit_logs "
            "WHERE tenant_id = ? ORDER BY rowid ASC",
            (tenant_id,),
        ).fetchall()

    expected_previous = "GENESIS"
    for number, row in enumerate(rows, start=1):
        recomputed = hashlib.sha256((row["previous_hash"] + row["details"]).encode("utf-8")).hexdigest()
        if row["previous_hash"] != expected_previous or recomputed != row["entry_hash"]:
            return False, len(rows), number
        expected_previous = row["entry_hash"]
    return True, len(rows), None


init_database()


def get_default_tenant_id() -> str:
    with conn_ctx() as c:
        tenant = c.execute("SELECT id FROM tenants ORDER BY created_at LIMIT 1").fetchone()
    return tenant["id"]


if "tenant_id" not in st.session_state:
    st.session_state.tenant_id = get_default_tenant_id()
if "user_email" not in st.session_state:
    st.session_state.user_email = "finance@invoiceshield.local"
if "require_po" not in st.session_state:
    st.session_state.require_po = True

# ---------------------------------------------------------------------
# UI helpers (everything dynamic is HTML-escaped: OCR text is untrusted)
# ---------------------------------------------------------------------


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def risk_pill(level: str) -> str:
    classes = {
        PASSED: "pill-verified",
        "Review Required": "pill-review",
        "High Risk": "pill-high",
        "Insufficient Evidence": "pill-info",
    }
    return f'<span class="pill {classes.get(level, "pill-info")}">{esc(level)}</span>'


def page_header(title: str, instruction: str):
    st.markdown(
        f'<div class="card" style="margin-bottom:1.1rem;"><div class="card-title">{esc(title)}</div>'
        f'<div class="card-help">{esc(instruction)}</div></div>',
        unsafe_allow_html=True,
    )


def kpi(label: str, value: Any, subtitle: str = ""):
    st.markdown(
        f'<div class="kpi"><div class="kpi-label">{esc(label)}</div>'
        f'<div class="kpi-value">{esc(value)}</div><div class="kpi-sub">{esc(subtitle)}</div></div>',
        unsafe_allow_html=True,
    )


FINDING_CLASS = {
    "Mathematical error": "finding-error",
    "Quantity overbilling": "finding-error",
    "Price or quantity discrepancy": "finding-error",
    "Supplier mismatch": "finding-error",
    "Tax inconsistency": "finding-warning",
    "OCR correction": "finding-warning",
    "Missing information": "finding-warning",
    "Unverified information": "finding-warning",
    "Extraction failure": "finding-info",
    "No issue": "finding-success",
}


def finding(title, severity, explanation, evidence="", expected=None, actual=None):
    comparison = ""
    if expected is not None and actual is not None:
        comparison = (
            f'<div class="small-muted" style="margin-top:.35rem;">'
            f"Expected: {esc(expected)} &nbsp;•&nbsp; Detected: {esc(actual)}</div>"
        )
    evidence_html = (
        f'<div class="evidence" style="margin-top:.55rem;">{esc(evidence)}</div>' if evidence else ""
    )
    st.markdown(
        f'<div class="finding {FINDING_CLASS.get(severity, "finding-warning")}">'
        f'<div class="finding-title">{esc(title)}</div>'
        f'<div class="finding-text">{esc(explanation)}</div>{comparison}{evidence_html}</div>',
        unsafe_allow_html=True,
    )


def select_invoice(label: str = "Select invoice") -> Optional[str]:
    with conn_ctx() as c:
        rows = c.execute(
            "SELECT id, invoice_number, supplier_name FROM invoices "
            "WHERE tenant_id = ? ORDER BY created_at DESC",
            (st.session_state.tenant_id,),
        ).fetchall()
    if not rows:
        return None

    options = {
        f"{r['invoice_number'] or 'Unnamed invoice'} — {r['supplier_name'] or 'Unknown supplier'} "
        f"[{r['id'][:6]}]": r["id"]
        for r in rows
    }
    return options[st.selectbox(label, list(options.keys()), key=f"select_{label}")]


# ---------------------------------------------------------------------
# OCR and text extraction
# ---------------------------------------------------------------------


@st.cache_resource(show_spinner=False)
def get_ocr_reader():
    try:
        import easyocr

        return easyocr.Reader(["en"], gpu=False, verbose=False)
    except Exception:
        return None


def preprocess_image(image_bytes: bytes) -> np.ndarray:
    """
    Light preprocessing. Heavy binarisation (adaptive threshold) hurts clean digital
    receipts, so we only grey-scale, bound the size and boost contrast when needed.
    """
    image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("The image could not be read.")

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape
    longest, shortest = max(height, width), min(height, width)

    if longest > MAX_OCR_SIDE:
        scale = MAX_OCR_SIDE / longest
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    elif shortest < 900:
        gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)

    if float(gray.std()) < 40:  # low-contrast photo/scan
        gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)

    return gray


def ocr_image(image_bytes: bytes) -> Tuple[str, float]:
    reader = get_ocr_reader()
    if reader is None:
        raise RuntimeError("EasyOCR could not be loaded. Install it with: pip install easyocr")

    results = reader.readtext(preprocess_image(image_bytes), detail=1, paragraph=False)

    boxes, confidences = [], []
    for bbox, text, confidence in results:
        text = str(text).strip()
        if not text:
            continue
        xs = [point[0] for point in bbox]
        ys = [point[1] for point in bbox]
        boxes.append((min(xs), min(ys), max(xs), max(ys), text))
        confidences.append(float(confidence))

    # Rebuild visual rows so "Invoice No:" and its value land on the same line.
    return core.boxes_to_text(boxes), (float(np.mean(confidences)) * 100 if confidences else 0.0)


def extract_document_text(file_bytes: bytes, extension: str) -> Tuple[str, int, bool, float]:
    """Returns (text, page_count, ocr_used, confidence)."""
    if extension != "pdf":
        text, confidence = ocr_image(file_bytes)
        return text, 1, True, confidence

    import pymupdf as fitz  # noqa: WPS433  (import here keeps module import cheap)

    text, page_count = core.pdf_text_layer(file_bytes)
    if len(text) >= 100:
        return text, page_count, False, 96.0

    if get_ocr_reader() is None:
        return text, page_count, False, 35.0

    pages, confidences = [], []
    with fitz.open(stream=file_bytes, filetype="pdf") as document:
        for page in document:
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            page_text, page_confidence = ocr_image(pixmap.tobytes("png"))
            pages.append(page_text)
            confidences.append(page_confidence)

    ocr_text = "\n".join(pages).strip()
    if len(ocr_text) > len(text):
        return ocr_text, page_count, True, float(np.mean(confidences)) if confidences else 0.0
    return text, page_count, False, 55.0


# ---------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------


def store_file(file_hash: str, extension: str, file_bytes: bytes) -> str:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOAD_DIR / f"{file_hash}.{extension}{'.enc' if ENCRYPTION_KEY else ''}"
    path.write_bytes(encrypt_bytes(file_bytes))
    return str(path)


def get_document_by_hash(tenant_id: str, file_hash: str):
    with conn_ctx() as c:
        return c.execute(
            "SELECT id FROM documents WHERE tenant_id = ? AND file_hash = ?",
            (tenant_id, file_hash),
        ).fetchone()


def save_document(tenant_id, filename, file_hash, document_type, page_count, ocr_used,
                  raw_text, confidence, currency, tax_regime, stored_path) -> str:
    document_id = str(uuid.uuid4())
    with conn_ctx() as c:
        c.execute(
            """
            INSERT INTO documents
            (id, tenant_id, filename, file_hash, document_type, uploaded_at, page_count,
             ocr_used, raw_text, extraction_confidence, currency, tax_regime, status,
             encrypted, stored_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (document_id, tenant_id, filename, file_hash, document_type, now_iso(),
             page_count, int(ocr_used), protect_text(raw_text), confidence, currency,
             tax_regime, "Processed", int(bool(ENCRYPTION_KEY)), stored_path),
        )
    return document_id


def save_invoice(tenant_id: str, document_id: str, invoice: Invoice) -> str:
    invoice_id = str(uuid.uuid4())
    confidence = sum(invoice.field_confidence.values()) / max(len(invoice.field_confidence), 1)

    with conn_ctx() as c:
        c.execute(
            """
            INSERT INTO invoices
            (id, tenant_id, document_id, invoice_number, invoice_date, due_date,
             supplier_name, supplier_gstin, buyer_name, po_reference, currency,
             tax_regime, subtotal, discount, shipping, tax_amount, tax_rate, cgst, sgst,
             igst, final_amount, extraction_confidence, extraction_quality, notes,
             risk_score, risk_level, approval_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (invoice_id, tenant_id, document_id, invoice.invoice_number, invoice.invoice_date,
             invoice.due_date, invoice.supplier_name, invoice.supplier_gstin, invoice.buyer_name,
             invoice.po_reference, invoice.currency, invoice.tax_regime, invoice.subtotal,
             invoice.discount, invoice.shipping, invoice.tax_amount, invoice.tax_rate,
             invoice.cgst, invoice.sgst, invoice.igst, invoice.final_amount, confidence,
             invoice.extraction_quality, json.dumps(invoice.notes + invoice.corrections),
             0.0, "Insufficient Evidence", "Needs Review", now_iso()),
        )

        for item in invoice.items:
            c.execute(
                """
                INSERT INTO invoice_items
                (id, invoice_id, description, sku, hsn_sac, quantity, unit, unit_price,
                 tax_rate, line_total, source_page)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), invoice_id, item.description, item.sku, item.hsn_sac,
                 item.quantity, item.unit, item.unit_price, item.tax_rate, item.line_total,
                 item.source_page),
            )
    return invoice_id


def save_validation_results(invoice_id: str, findings: List[Dict[str, Any]]):
    with conn_ctx() as c:
        c.execute("DELETE FROM validation_results WHERE invoice_id = ?", (invoice_id,))
        for item in findings:
            c.execute(
                """
                INSERT INTO validation_results
                (id, invoice_id, check_name, result, severity, expected_value,
                 actual_value, explanation, evidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), invoice_id, item["check_name"], item["result"],
                 item["severity"], str(item["expected_value"]), str(item["actual_value"]),
                 item["explanation"], item["evidence"], now_iso()),
            )


def save_duplicates(invoice_id: str, matches: List[Dict[str, Any]]):
    with conn_ctx() as c:
        c.execute("DELETE FROM duplicate_matches WHERE invoice_id = ?", (invoice_id,))
        for match in matches:
            c.execute(
                """
                INSERT INTO duplicate_matches
                (id, invoice_id, matched_invoice_id, match_type, similarity, reasons, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), invoice_id, match["matched_invoice_id"], match["match_type"],
                 match["similarity"], match["reasons"], now_iso()),
            )


def save_reconciliation(invoice_id: str, results: List[Dict[str, Any]]):
    with conn_ctx() as c:
        c.execute("DELETE FROM reconciliation_results WHERE invoice_id = ?", (invoice_id,))
        for result in results:
            c.execute(
                """
                INSERT INTO reconciliation_results
                (id, invoice_id, reconciliation_type, result, severity, explanation,
                 evidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), invoice_id, result["reconciliation_type"], result["result"],
                 result["severity"], result["explanation"], result["evidence"], now_iso()),
            )


def get_invoice_history(tenant_id: str, exclude_id: Optional[str] = None) -> List[Dict[str, Any]]:
    with conn_ctx() as c:
        rows = c.execute(
            """
            SELECT i.id AS invoice_id, i.invoice_number, i.supplier_name, i.invoice_date,
                   i.final_amount, i.po_reference, d.file_hash
            FROM invoices i LEFT JOIN documents d ON d.id = i.document_id
            WHERE i.tenant_id = ?
            """,
            (tenant_id,),
        ).fetchall()
    history = [dict(r) for r in rows]
    return [h for h in history if h["invoice_id"] != exclude_id] if exclude_id else history


def invoice_from_rows(row, item_rows) -> Invoice:
    keys = row.keys()
    fields = [
        "invoice_number", "invoice_date", "due_date", "supplier_name", "supplier_gstin",
        "buyer_name", "po_reference", "currency", "tax_regime", "subtotal", "tax_amount",
        "tax_rate", "final_amount", "extraction_quality",
    ]
    data = {f: row[f] for f in fields if f in keys}
    for numeric in ("discount", "shipping", "cgst", "sgst", "igst"):
        data[numeric] = row[numeric] or 0.0
    data["items"] = [
        LineItem(**{k: item[k] for k in LineItem.model_fields if k in item.keys()})
        for item in item_rows
    ]
    return Invoice(**data)


def tenant_has_po_data(connection, tenant_id: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM purchase_orders WHERE tenant_id = ? LIMIT 1", (tenant_id,)
        ).fetchone()
        or connection.execute(
            "SELECT 1 FROM goods_receipts WHERE tenant_id = ? LIMIT 1", (tenant_id,)
        ).fetchone()
    )


def reconcile_for_invoice(invoice_id: str, tenant_id: str) -> List[Dict[str, Any]]:
    with conn_ctx() as c:
        invoice = c.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        items = [dict(r) for r in c.execute(
            "SELECT * FROM invoice_items WHERE invoice_id = ?", (invoice_id,))]
        po = c.execute(
            "SELECT * FROM purchase_orders WHERE tenant_id = ? AND po_number = ?",
            (tenant_id, invoice["po_reference"]),
        ).fetchone()
        po_items = [dict(r) for r in c.execute(
            "SELECT * FROM purchase_order_items WHERE po_id = ?", (po["id"],))] if po else []
        grn = c.execute(
            "SELECT * FROM goods_receipts WHERE tenant_id = ? AND po_number = ?",
            (tenant_id, invoice["po_reference"]),
        ).fetchone()
        grn_items = [dict(r) for r in c.execute(
            "SELECT * FROM goods_receipt_items WHERE grn_id = ?", (grn["id"],))] if grn else []

    return core.reconcile(
        dict(invoice), items, dict(po) if po else None, po_items,
        dict(grn) if grn else None, grn_items,
    )


def refresh_risk(invoice_id: str, tenant_id: str) -> Dict[str, Any]:
    """Recompute and persist the risk score from everything currently stored."""
    with conn_ctx() as c:
        row = c.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        item_rows = c.execute("SELECT * FROM invoice_items WHERE invoice_id = ?", (invoice_id,)).fetchall()
        findings = [dict(r) for r in c.execute(
            "SELECT * FROM validation_results WHERE invoice_id = ?", (invoice_id,))]
        duplicates = [dict(r) for r in c.execute(
            "SELECT * FROM duplicate_matches WHERE invoice_id = ?", (invoice_id,))]
        reconciliation = [dict(r) for r in c.execute(
            "SELECT * FROM reconciliation_results WHERE invoice_id = ?", (invoice_id,))]
        has_po_data = tenant_has_po_data(c, tenant_id)

    # Without any PO/GRN records the register is simply not in use: only hard failures count.
    if not has_po_data:
        reconciliation = [r for r in reconciliation if r["result"] == "Fail"]

    invoice = invoice_from_rows(row, item_rows)
    score, level, indicators, recommendation = core.assess_risk(
        invoice, findings, duplicates, reconciliation
    )

    with conn_ctx() as c:
        c.execute("UPDATE invoices SET risk_score = ?, risk_level = ? WHERE id = ?",
                  (score, level, invoice_id))
        c.execute("DELETE FROM risk_assessments WHERE invoice_id = ?", (invoice_id,))
        c.execute(
            """
            INSERT INTO risk_assessments
            (id, invoice_id, score, classification, indicators, recommendation, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), invoice_id, score, level, json.dumps(indicators),
             recommendation, now_iso()),
        )

    return {"score": score, "classification": level, "indicators": indicators,
            "recommendation": recommendation}


# ---------------------------------------------------------------------
# Upload pipeline
# ---------------------------------------------------------------------


class ProcessingError(Exception):
    pass


def validate_file(filename: str, size: int, data: bytes) -> None:
    extension = Path(filename).suffix.lower().lstrip(".")
    if extension not in ALLOWED_TYPES:
        raise ProcessingError("Unsupported file type.")
    if size > MAX_FILE_MB * 1024 * 1024:
        raise ProcessingError(f"File exceeds {MAX_FILE_MB} MB.")

    header = data[:8]
    if extension == "pdf" and not header.startswith(b"%PDF"):
        raise ProcessingError("File is not a valid PDF.")
    if extension == "png" and not header.startswith(b"\x89PNG"):
        raise ProcessingError("File is not a valid PNG image.")
    if extension in {"jpg", "jpeg"} and not header.startswith(b"\xff\xd8\xff"):
        raise ProcessingError("File is not a valid JPEG image.")


def process_document(filename: str, file_bytes: bytes, tenant_id: str,
                     require_po: bool = True) -> Dict[str, Any]:
    validate_file(filename, len(file_bytes), file_bytes)
    extension = Path(filename).suffix.lower().lstrip(".")
    file_hash = sha256(file_bytes)

    if get_document_by_hash(tenant_id, file_hash):
        raise ProcessingError("this exact file has already been processed.")

    raw_text, page_count, ocr_used, confidence = extract_document_text(file_bytes, extension)

    document_type = core.classify_document(raw_text)
    if document_type != "Invoice":
        raise ProcessingError(f"identified as {document_type}, not an invoice.")

    invoice = core.extract_invoice(raw_text, confidence, ocr_used)

    document_id = save_document(
        tenant_id, filename, file_hash, document_type, page_count, ocr_used, raw_text,
        confidence, invoice.currency, invoice.tax_regime,
        store_file(file_hash, extension, file_bytes),
    )

    duplicates = core.detect_duplicates(
        {
            "invoice_id": None,
            "invoice_number": invoice.invoice_number,
            "supplier_name": invoice.supplier_name,
            "invoice_date": invoice.invoice_date,
            "final_amount": invoice.final_amount,
            "po_reference": invoice.po_reference,
            "file_hash": file_hash,
        },
        get_invoice_history(tenant_id),
    )
    findings = core.validate_invoice(invoice, require_po=require_po)

    invoice_id = save_invoice(tenant_id, document_id, invoice)
    save_validation_results(invoice_id, findings)
    save_duplicates(invoice_id, duplicates)
    save_reconciliation(invoice_id, reconcile_for_invoice(invoice_id, tenant_id))
    risk = refresh_risk(invoice_id, tenant_id)

    audit(
        "invoice_processed", "invoice", invoice_id,
        {"filename": filename, "ocr_used": ocr_used, "risk_score": risk["score"],
         "risk_level": risk["classification"]},
        tenant_id,
    )

    return {
        "File": filename,
        "Invoice": invoice.invoice_number or "Not detected",
        "Supplier": invoice.supplier_name or "Not detected",
        "Amount": money(invoice.final_amount, invoice.currency),
        "Items": len(invoice.items),
        "Risk": risk["classification"],
        "Score": risk["score"],
    }


# ---------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------


def create_excel_report(invoice: Invoice, findings: List[Dict[str, Any]], risk: Dict[str, Any]) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame([{
            "Invoice number": invoice.invoice_number, "Invoice date": invoice.invoice_date,
            "Supplier": invoice.supplier_name, "Tax registration": invoice.supplier_gstin,
            "Buyer": invoice.buyer_name, "PO reference": invoice.po_reference,
            "Currency": invoice.currency, "Tax regime": invoice.tax_regime,
            "Subtotal": invoice.subtotal, "Discount": invoice.discount,
            "Shipping": invoice.shipping, "Tax": invoice.tax_amount,
            "Final amount": invoice.final_amount, "Extraction quality": invoice.extraction_quality,
            "Risk score": risk["score"], "Classification": risk["classification"],
        }]).to_excel(writer, sheet_name="Invoice Summary", index=False)

        pd.DataFrame([i.model_dump() for i in invoice.items]).to_excel(
            writer, sheet_name="Line Items", index=False)
        pd.DataFrame(findings).to_excel(writer, sheet_name="Validation Findings", index=False)
        pd.DataFrame({"Risk indicators": risk["indicators"]}).to_excel(
            writer, sheet_name="Risk Indicators", index=False)
    return output.getvalue()


def create_pdf_report(invoice: Invoice, findings, risk, duplicates) -> bytes:
    buffer = io.BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=36, leftMargin=36,
                                 topMargin=42, bottomMargin=36)
    styles = getSampleStyleSheet()
    safe = lambda value: xml_escape(str(value))  # noqa: E731  OCR text may contain < & >
    story = [
        Paragraph("<b>InvoiceShield AI — Verification Report</b>", styles["Title"]),
        Paragraph(f"Generated: {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC", styles["Normal"]),
        Spacer(1, 16),
    ]

    summary = [
        ["Invoice number", invoice.invoice_number or "Not detected"],
        ["Supplier", invoice.supplier_name or "Not detected"],
        ["Invoice date", invoice.invoice_date or "Not detected"],
        ["Currency", invoice.currency or "Not detected"],
        ["Tax regime", invoice.tax_regime or "Not detected"],
        ["Final amount", money(invoice.final_amount, invoice.currency)],
        ["Extraction quality", invoice.extraction_quality],
        ["Risk score", f"{risk['score']}/100"],
        ["Classification", risk["classification"]],
    ]
    table = Table([[Paragraph(safe(a), styles["Normal"]), Paragraph(safe(b), styles["Normal"])]
                   for a, b in summary], colWidths=[160, 320])
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, colors.HexColor("#F1F5F9")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story += [table, Spacer(1, 18), Paragraph("<b>Validation findings</b>", styles["Heading2"])]

    if findings:
        for item in findings:
            story += [
                Paragraph(
                    f"<b>{safe(item['check_name'])}</b> — {safe(item['severity'])}<br/>"
                    f"{safe(item['explanation'])}<br/>"
                    f"<i>Evidence: {safe(item['evidence']).replace(chr(10), '<br/>')}</i>",
                    styles["Normal"]),
                Spacer(1, 8),
            ]
    else:
        story.append(Paragraph("No validation exceptions were identified.", styles["Normal"]))

    story += [Spacer(1, 14), Paragraph("<b>Duplicate analysis</b>", styles["Heading2"])]
    if duplicates:
        for dup in duplicates:
            story += [
                Paragraph(
                    f"{safe(dup.get('match_type', 'Duplicate match'))} — "
                    f"similarity {safe(dup.get('similarity', 0))}%<br/>"
                    f"Matched invoice: {safe(dup.get('matched_invoice_number') or 'Not available')}<br/>"
                    f"Reasons: {safe(dup.get('reasons', 'No reason recorded'))}",
                    styles["Normal"]),
                Spacer(1, 6),
            ]
    else:
        story.append(Paragraph("No duplicate indicators were identified.", styles["Normal"]))

    story += [Spacer(1, 14), Paragraph("<b>Risk assessment</b>", styles["Heading2"]),
              Paragraph(safe(risk["recommendation"]), styles["Normal"])]
    if risk["indicators"]:
        story += [Spacer(1, 8), Paragraph("<b>Risk indicators</b>", styles["Heading3"])]
        story += [Paragraph(f"• {safe(i)}", styles["Normal"]) for i in risk["indicators"]]

    document.build(story)
    return buffer.getvalue()


# ---------------------------------------------------------------------
# UI: Home
# ---------------------------------------------------------------------


def render_home():
    st.markdown(
        """
        <div class="app-hero">
          <div class="hero-badge">Invoice verification workspace</div>
          <h1>🛡️ InvoiceShield AI</h1>
          <p>Upload an invoice, extract its details, validate calculations, check duplicates, reconcile with PO and GRN, then approve or escalate.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    tenant_id = st.session_state.tenant_id
    with conn_ctx() as c:
        count = c.execute("SELECT COUNT(*) FROM invoices WHERE tenant_id = ?", (tenant_id,)).fetchone()[0]
        value = c.execute("SELECT COALESCE(SUM(final_amount), 0) FROM invoices WHERE tenant_id = ?",
                          (tenant_id,)).fetchone()[0]
        high = c.execute("SELECT COUNT(*) FROM invoices WHERE tenant_id = ? AND risk_level = 'High Risk'",
                         (tenant_id,)).fetchone()[0]
        pending = c.execute("SELECT COUNT(*) FROM invoices WHERE tenant_id = ? AND approval_status = 'Needs Review'",
                            (tenant_id,)).fetchone()[0]
        dupes = c.execute(
            "SELECT COUNT(*) FROM duplicate_matches dm JOIN invoices i ON i.id = dm.invoice_id "
            "WHERE i.tenant_id = ?", (tenant_id,)).fetchone()[0]

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        kpi("Invoices processed", count, "Stored in local SQLite")
    with col2:
        kpi("Invoice value", f"{value:,.2f}", "Sum of extracted amounts (mixed currencies)")
    with col3:
        kpi("High risk", high, "Priority finance review")
    with col4:
        kpi("Needs review", pending, "Awaiting reviewer action")

    if count == 0:
        st.info("**No invoices processed yet.** Go to **Upload & Analyze**, upload a PDF or image "
                "invoice (try the files in `samples/`), then click **Process invoices**.")

    st.markdown(
        """
        <div class="card" style="margin-top:1.1rem;">
        <div class="card-title">How InvoiceShield works</div>
        <div class="step"><div class="step-number">1</div><div class="step-text"><b>Upload</b> — Add invoice PDFs or images.</div></div>
        <div class="step"><div class="step-number">2</div><div class="step-text"><b>Extract</b> — Text layer or OCR, rebuilt into rows, then parsed into fields and line items.</div></div>
        <div class="step"><div class="step-number">3</div><div class="step-text"><b>Validate</b> — Independent arithmetic and tax-consistency checks.</div></div>
        <div class="step"><div class="step-number">4</div><div class="step-text"><b>Reconcile</b> — Compare invoice, PO and GRN records.</div></div>
        <div class="step"><div class="step-number">5</div><div class="step-text"><b>Approve</b> — Review exceptions, approve, reject or request more information.</div></div>
        </div>
        <div class="card" style="margin-top:1.1rem;">
        <div class="card-title">Duplicate alerts</div>
        <div class="card-help">"""
        + esc(f"{dupes} duplicate or similar-invoice alert(s) are currently stored.")
        + "</div></div>",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------
# UI: Upload
# ---------------------------------------------------------------------


def render_upload():
    page_header(
        "📤 Upload and analyze invoices",
        "Upload invoice PDFs or images. The app identifies the document type, extracts key "
        "details and saves a structured record.",
    )

    uploaded_files = st.file_uploader(
        "Choose invoice files", type=ALLOWED_TYPES, accept_multiple_files=True,
        help=f"Supported formats: PDF, PNG, JPG and JPEG. Maximum {MAX_FILE_MB} MB per file.",
    )

    if not uploaded_files:
        st.info("**How to use:** Click **Browse files**, select one or more invoices, then press "
                "**Process invoices**.")
        return

    st.dataframe(
        pd.DataFrame([{"File name": f.name, "Size": f"{f.size / 1024:.1f} KB",
                       "Type": Path(f.name).suffix.lower().lstrip(".")} for f in uploaded_files]),
        width="stretch", hide_index=True,
    )

    if not st.button("Process invoices", type="primary", width="stretch"):
        return

    progress = st.progress(0, text="Preparing document processing...")
    processed, errors = [], []

    for index, uploaded in enumerate(uploaded_files, start=1):
        try:
            processed.append(process_document(
                uploaded.name, uploaded.getvalue(), st.session_state.tenant_id,
                st.session_state.require_po))
        except ProcessingError as error:
            errors.append(f"{uploaded.name}: {error}")
        except Exception as error:  # keep the batch going
            errors.append(f"{uploaded.name}: unexpected error — {error}")

        progress.progress(int(index / len(uploaded_files) * 100),
                          text=f"Processed {index} of {len(uploaded_files)} file(s)")

    if processed:
        st.success(f"Successfully processed {len(processed)} invoice(s).")
        st.dataframe(pd.DataFrame(processed), width="stretch", hide_index=True)
    if errors:
        st.warning("Some files could not be processed.")
        for error in errors:
            st.error(error)


# ---------------------------------------------------------------------
# UI: Invoice analyzer
# ---------------------------------------------------------------------


def render_analyzer():
    page_header("🧾 Invoice analyzer",
                "Select an invoice to inspect the extracted details, line items, source text and risk classification.")

    invoice_id = select_invoice()
    if not invoice_id:
        st.info("No invoices are available yet. Upload an invoice first.")
        return

    with conn_ctx() as c:
        invoice = c.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        items = c.execute("SELECT * FROM invoice_items WHERE invoice_id = ?", (invoice_id,)).fetchall()
        document = c.execute("SELECT * FROM documents WHERE id = ?", (invoice["document_id"],)).fetchone()

    cur = invoice["currency"]
    left, right = st.columns([1.15, 1])

    with left:
        st.markdown('<div class="card-title">Extracted invoice details</div>', unsafe_allow_html=True)
        st.markdown(risk_pill(invoice["risk_level"]), unsafe_allow_html=True)

        details = pd.DataFrame([
            ["Invoice number", invoice["invoice_number"] or "Not detected"],
            ["Invoice date", invoice["invoice_date"] or "Not detected"],
            ["Due date", invoice["due_date"] or "Not detected"],
            ["Supplier", invoice["supplier_name"] or "Not detected"],
            ["Tax registration", invoice["supplier_gstin"] or "Not detected"],
            ["Buyer", invoice["buyer_name"] or "Not detected"],
            ["PO reference", invoice["po_reference"] or "Not detected"],
            ["Currency", cur or "Not detected"],
            ["Tax regime", invoice["tax_regime"] or "Not detected"],
            ["Subtotal", money(invoice["subtotal"], cur)],
            ["Discount", money(invoice["discount"], cur)],
            ["Shipping", money(invoice["shipping"], cur)],
            ["Tax amount", money(invoice["tax_amount"], cur)],
            ["Final amount", money(invoice["final_amount"], cur)],
            ["Extraction confidence", f"{invoice['extraction_confidence'] or 0:.1f}%"],
            ["Risk score", f"{invoice['risk_score'] or 0}/100"],
            ["Approval status", invoice["approval_status"]],
        ], columns=["Field", "Extracted value"])
        st.dataframe(details, width="stretch", hide_index=True)

        for note in json.loads(invoice["notes"] or "[]"):
            st.caption(f"ℹ️ {note}")

        st.markdown('<div class="card-title" style="margin-top:1rem;">Line items</div>', unsafe_allow_html=True)
        if items:
            frame = pd.DataFrame([dict(i) for i in items])[
                ["description", "sku", "hsn_sac", "quantity", "unit", "unit_price", "tax_rate", "line_total"]]
            frame.columns = ["Description", "SKU", "HSN/SAC", "Quantity", "Unit",
                             "Unit price", "Tax rate", "Line total"]
            st.dataframe(frame, width="stretch", hide_index=True)
        else:
            st.warning("No line items were extracted. This may occur with an unusual table layout "
                       "or a low-quality scan.")

    with right:
        st.markdown('<div class="card-title">Source document</div>', unsafe_allow_html=True)
        if document:
            st.markdown(f"**File name:** {document['filename']}")
            st.markdown(f"**Document type:** {document['document_type']}")
            st.markdown(f"**Pages:** {document['page_count']}")
            st.markdown(f"**Text method:** {'OCR' if document['ocr_used'] else 'Embedded PDF text'}")
            st.markdown(f"**OCR / text confidence:** {document['extraction_confidence'] or 0:.1f}%")
            st.markdown(f"**Document hash:** `{document['file_hash'][:18]}...`")

            path = Path(document["stored_path"] or "")
            if path.is_file():
                try:
                    st.download_button("Download original file", decrypt_bytes(path.read_bytes()),
                                       file_name=document["filename"])
                except Exception:
                    st.caption("Original file could not be decrypted with the current key.")

        with st.expander("View extracted text (as reconstructed from the document)"):
            st.text_area("Extracted text", reveal_text(document["raw_text"]) if document else "",
                         height=420, disabled=True)


# ---------------------------------------------------------------------
# UI: Validation
# ---------------------------------------------------------------------


def render_validation():
    page_header("✅ Financial validation",
                "Select an invoice to see independent checks on line-item calculations, subtotal, "
                "final amount and tax consistency.")

    invoice_id = select_invoice()
    if not invoice_id:
        st.info("No invoices are available for validation. Upload an invoice first.")
        return

    with conn_ctx() as c:
        results = c.execute(
            "SELECT * FROM validation_results WHERE invoice_id = ? ORDER BY rowid", (invoice_id,)
        ).fetchall()
        invoice = c.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()

    failed = sum(1 for r in results if r["result"] == "Fail")
    warnings = sum(1 for r in results if r["result"] == "Warning")

    col1, col2, col3 = st.columns(3)
    with col1:
        kpi("Findings", len(results), "Exceptions raised by the rules")
    with col2:
        kpi("Failed checks", failed, "Calculation or tax issue")
    with col3:
        kpi("Warnings", warnings, "Manual verification advised")

    st.markdown('<div class="card-title" style="margin-top:1rem;">Validation results</div>', unsafe_allow_html=True)

    if not results:
        finding("All configured checks passed", "No issue",
                "No calculation, tax or missing-information issue was identified.",
                "All invoice values available to the validation engine reconciled successfully.")
    for r in results:
        finding(r["check_name"], r["severity"], r["explanation"], r["evidence"],
                r["expected_value"], r["actual_value"])

    st.markdown('<div class="card-title" style="margin-top:1rem;">Correct extraction values</div>', unsafe_allow_html=True)
    st.caption("If OCR misread an amount, correct it here. The invoice is re-validated and re-scored, and the correction is audit-logged.")

    def as_text(value):
        return "" if value is None else str(value)

    new_subtotal = st.text_input("Correct subtotal", value=as_text(invoice["subtotal"]))
    new_tax = st.text_input("Correct tax amount", value=as_text(invoice["tax_amount"]))
    new_final = st.text_input("Correct final amount", value=as_text(invoice["final_amount"]))

    if st.button("Save corrections and revalidate", type="primary"):
        try:
            subtotal, tax, final = (clean_or_none(v) for v in (new_subtotal, new_tax, new_final))

            with conn_ctx() as c:
                item_count = c.execute("SELECT COUNT(*) FROM invoice_items WHERE invoice_id = ?",
                                       (invoice_id,)).fetchone()[0]
                quality = invoice["extraction_quality"]
                if final is not None and invoice["invoice_number"] and item_count:
                    quality = "Good"
                elif final is not None or item_count:
                    quality = "Partial"

                c.execute(
                    "UPDATE invoices SET subtotal = ?, tax_amount = ?, final_amount = ?, "
                    "extraction_quality = ? WHERE id = ?",
                    (subtotal, tax, final, quality, invoice_id),
                )
                updated = c.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
                item_rows = c.execute("SELECT * FROM invoice_items WHERE invoice_id = ?",
                                      (invoice_id,)).fetchall()

            rebuilt = invoice_from_rows(updated, item_rows)
            save_validation_results(
                invoice_id, core.validate_invoice(rebuilt, require_po=st.session_state.require_po))
            refresh_risk(invoice_id, st.session_state.tenant_id)

            audit("manual_correction", "invoice", invoice_id,
                  {"subtotal": new_subtotal, "tax_amount": new_tax, "final_amount": new_final,
                   "by": st.session_state.user_email},
                  st.session_state.tenant_id)
            st.success("Corrections saved, validation rerun and risk re-scored.")
            st.rerun()
        except Exception as error:
            st.error(f"Could not save corrections: {error}")


def clean_or_none(value: str) -> Optional[float]:
    return core.clean_number(value)


# ---------------------------------------------------------------------
# UI: Duplicates
# ---------------------------------------------------------------------


def render_duplicates():
    page_header("🔁 Duplicate detection",
                "Select an invoice and run a duplicate check against every invoice already stored.")

    invoice_id = select_invoice()
    if not invoice_id:
        st.info("No invoices are available for duplicate analysis. Upload an invoice first.")
        return

    tenant_id = st.session_state.tenant_id

    if st.button("Run duplicate check", type="primary"):
        with conn_ctx() as c:
            invoice = c.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
            document = c.execute("SELECT * FROM documents WHERE id = ?", (invoice["document_id"],)).fetchone()

        matches = core.detect_duplicates(
            {
                "invoice_id": invoice_id,
                "invoice_number": invoice["invoice_number"],
                "supplier_name": invoice["supplier_name"],
                "invoice_date": invoice["invoice_date"],
                "final_amount": invoice["final_amount"],
                "po_reference": invoice["po_reference"],
                "file_hash": document["file_hash"] if document else None,
            },
            get_invoice_history(tenant_id, exclude_id=invoice_id),
        )
        save_duplicates(invoice_id, matches)
        refresh_risk(invoice_id, tenant_id)
        audit("duplicate_check", "invoice", invoice_id, {"matches_found": len(matches)}, tenant_id)
        st.success(f"Duplicate check complete. {len(matches)} potential match(es) found.")
        st.rerun()

    with conn_ctx() as c:
        matches_df = pd.read_sql_query(
            """
            SELECT dm.match_type AS "Match type", dm.similarity AS "Similarity",
                   matched.invoice_number AS "Matched invoice", dm.reasons AS "Reasons"
            FROM duplicate_matches dm
            LEFT JOIN invoices matched ON matched.id = dm.matched_invoice_id
            WHERE dm.invoice_id = ?
            ORDER BY dm.similarity DESC
            """,
            c, params=(invoice_id,),
        )

    st.markdown('<div class="card-title" style="margin-top:1rem;">Duplicate alerts for this invoice</div>', unsafe_allow_html=True)
    st.markdown('<div class="card-help">A match is a reason to review the invoice. It is not proof of fraud.</div>', unsafe_allow_html=True)

    if matches_df.empty:
        st.info("No duplicate alerts are recorded for this invoice.")
    else:
        st.dataframe(matches_df, width="stretch", hide_index=True)


# ---------------------------------------------------------------------
# UI: Reconciliation
# ---------------------------------------------------------------------


def items_editor(key: str, quantity_label: str) -> pd.DataFrame:
    template = pd.DataFrame({"description": [""], "sku": [""], quantity_label: [0.0], "unit_price": [0.0]})
    edited = st.data_editor(template, num_rows="dynamic", key=key, width="stretch")
    return edited[edited["description"].astype(str).str.strip() != ""]


def render_reconciliation():
    page_header("🧩 PO / GRN reconciliation",
                "Record purchase orders and goods receipts (with line items), then reconcile invoices "
                "against them item by item.")

    tenant_id = st.session_state.tenant_id
    tab1, tab2, tab3 = st.tabs(["Create PO", "Create GRN", "Run reconciliation"])

    with tab1:
        po_number = st.text_input("PO number", key="po_number_input")
        supplier = st.text_input("Supplier name", key="po_supplier_name_input")
        currency = st.selectbox("Currency", ["INR", "USD", "EUR", "GBP"], key="po_currency_input")
        total_text = st.text_input("PO total amount (optional if line items are entered)", key="po_total_amount_input")
        st.caption("PO line items")
        po_items = items_editor("po_items_editor", "quantity")

        if st.button("Save purchase order", type="primary"):
            if not (po_number and supplier):
                st.warning("PO number and supplier name are required.")
            else:
                try:
                    po_id = str(uuid.uuid4())
                    computed = float((po_items["quantity"] * po_items["unit_price"]).sum()) if len(po_items) else None
                    total = core.clean_number(total_text) if total_text.strip() else computed
                    with conn_ctx() as c:
                        c.execute(
                            "INSERT INTO purchase_orders (id, tenant_id, po_number, supplier_name, currency, "
                            "total_amount, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (po_id, tenant_id, po_number.strip(), supplier.strip(), currency, total,
                             "Approved", now_iso()))
                        for _, row in po_items.iterrows():
                            c.execute(
                                "INSERT INTO purchase_order_items (id, po_id, description, sku, quantity, "
                                "unit_price, line_total) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (str(uuid.uuid4()), po_id, row["description"], row["sku"] or None,
                                 float(row["quantity"]), float(row["unit_price"]),
                                 float(row["quantity"]) * float(row["unit_price"])))
                    audit("po_created", "purchase_order", po_id, {"po_number": po_number}, tenant_id)
                    st.success("Purchase order saved.")
                except sqlite3.IntegrityError:
                    st.error(f"PO {po_number} already exists.")
                except Exception as error:
                    st.error(f"Could not save PO: {error}")

        with conn_ctx() as c:
            existing = pd.read_sql_query(
                "SELECT po_number AS 'PO', supplier_name AS 'Supplier', currency AS 'Currency', "
                "total_amount AS 'Total' FROM purchase_orders WHERE tenant_id = ?", c, params=(tenant_id,))
        if not existing.empty:
            st.dataframe(existing, width="stretch", hide_index=True)

    with tab2:
        grn_number = st.text_input("GRN number", key="grn_number_input")
        grn_po = st.text_input("Related PO number", key="grn_po_number_input")
        grn_supplier = st.text_input("Supplier name", key="grn_supplier_name_input")
        receipt_date = st.date_input("Receipt date", key="grn_receipt_date_input")
        st.caption("Received items")
        grn_items = items_editor("grn_items_editor", "quantity_received")

        if st.button("Save goods receipt", type="primary"):
            if not (grn_number and grn_po):
                st.warning("GRN number and PO number are required.")
            else:
                try:
                    grn_id = str(uuid.uuid4())
                    with conn_ctx() as c:
                        c.execute(
                            "INSERT INTO goods_receipts (id, tenant_id, grn_number, po_number, supplier_name, "
                            "receipt_date, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (grn_id, tenant_id, grn_number.strip(), grn_po.strip(), grn_supplier.strip(),
                             receipt_date.isoformat(), now_iso()))
                        for _, row in grn_items.iterrows():
                            c.execute(
                                "INSERT INTO goods_receipt_items (id, grn_id, description, sku, "
                                "quantity_received, unit_price) VALUES (?, ?, ?, ?, ?, ?)",
                                (str(uuid.uuid4()), grn_id, row["description"], row["sku"] or None,
                                 float(row["quantity_received"]), float(row["unit_price"])))
                    audit("grn_created", "goods_receipt", grn_id, {"grn_number": grn_number}, tenant_id)
                    st.success("Goods receipt saved.")
                except sqlite3.IntegrityError:
                    st.error(f"GRN {grn_number} already exists.")
                except Exception as error:
                    st.error(f"Could not save GRN: {error}")

    with tab3:
        invoice_id = select_invoice()
        if not invoice_id:
            st.info("No invoices are available for reconciliation.")
            return

        if st.button("Run three-way reconciliation", type="primary"):
            results = reconcile_for_invoice(invoice_id, tenant_id)
            save_reconciliation(invoice_id, results)
            refresh_risk(invoice_id, tenant_id)
            audit("reconciliation_run", "invoice", invoice_id, {"results": len(results)}, tenant_id)
            st.success("Reconciliation complete and risk re-scored.")
            st.rerun()

        with conn_ctx() as c:
            rows = c.execute("SELECT * FROM reconciliation_results WHERE invoice_id = ? ORDER BY rowid",
                             (invoice_id,)).fetchall()
        if not rows:
            st.info("No reconciliation results available yet.")
        for r in rows:
            finding(r["reconciliation_type"], r["severity"], r["explanation"], r["evidence"])


# ---------------------------------------------------------------------
# UI: Risk and approval
# ---------------------------------------------------------------------


def render_risk():
    page_header("🚨 Risk review and approval",
                "Review invoices, approve clean records, reject invalid invoices or request additional information.")

    tenant_id = st.session_state.tenant_id
    with conn_ctx() as c:
        assessments = pd.read_sql_query(
            "SELECT id AS invoice_id, invoice_number, supplier_name, final_amount, risk_score AS score, "
            "risk_level AS classification, approval_status FROM invoices WHERE tenant_id = ? "
            "ORDER BY risk_score DESC", c, params=(tenant_id,))

    if assessments.empty:
        st.info("No invoices have been processed yet.")
        return

    counts = assessments["classification"].value_counts().reset_index()
    counts.columns = ["Classification", "Count"]
    fig = px.pie(counts, names="Classification", values="Count", hole=0.62,
                 color="Classification",
                 color_discrete_map={PASSED: GREEN, "Review Required": AMBER,
                                     "High Risk": RED, "Insufficient Evidence": BLUE})
    fig.update_layout(title="Invoice risk classification", height=380,
                      margin=dict(t=50, b=10, l=10, r=10), paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", font_color="#FFFFFF")
    st.plotly_chart(fig, width="stretch")

    st.markdown('<div class="card-title">Invoice risk register</div>', unsafe_allow_html=True)
    display = assessments[["invoice_number", "supplier_name", "final_amount", "score",
                           "classification", "approval_status"]].copy()
    display.columns = ["Invoice", "Supplier", "Amount", "Risk score", "Classification", "Approval status"]
    st.dataframe(display, width="stretch", hide_index=True)

    st.markdown('<div class="card-title" style="margin-top:1rem;">Approval action</div>', unsafe_allow_html=True)
    invoice_id = select_invoice("Select invoice to action")
    if not invoice_id:
        return

    with conn_ctx() as c:
        selected = c.execute("SELECT risk_level FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        stored_risk = c.execute(
            "SELECT indicators, recommendation FROM risk_assessments WHERE invoice_id = ?", (invoice_id,)
        ).fetchone()

    if stored_risk:
        st.markdown(f"**Recommendation:** {stored_risk['recommendation']}")
        for indicator in json.loads(stored_risk["indicators"] or "[]"):
            st.caption(f"• {indicator}")

    action = st.selectbox("Action", ["Approve invoice", "Reject invoice", "Request more information"])
    comment = st.text_input("Reviewer comment (optional)")
    acknowledged = True
    if action == "Approve invoice" and selected["risk_level"] in ("High Risk", "Review Required", "Insufficient Evidence"):
        acknowledged = st.checkbox("I have reviewed the flagged items and accept responsibility for approving.")

    if st.button("Apply approval action", type="primary", disabled=not acknowledged):
        status = {"Approve invoice": "Approved", "Reject invoice": "Rejected",
                  "Request more information": "Info Requested"}[action]
        with conn_ctx() as c:
            c.execute("UPDATE invoices SET approval_status = ?, reviewed_by = ?, reviewed_at = ? WHERE id = ?",
                      (status, st.session_state.user_email, now_iso(), invoice_id))
        audit("approval_action", "invoice", invoice_id,
              {"action": action, "comment": comment, "by": st.session_state.user_email}, tenant_id)
        st.success(f"Invoice status updated to {status}.")
        st.rerun()


# ---------------------------------------------------------------------
# UI: Reports
# ---------------------------------------------------------------------


def render_reports():
    page_header("📄 Reports and audit trail",
                "Select an invoice to download a verification report. The audit trail is hash-linked "
                "so tampering is detectable.")

    tenant_id = st.session_state.tenant_id
    invoice_id = select_invoice()
    if not invoice_id:
        st.info("No invoices are available for report generation.")
        return

    with conn_ctx() as c:
        row = c.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        item_rows = c.execute("SELECT * FROM invoice_items WHERE invoice_id = ?", (invoice_id,)).fetchall()
        findings = [dict(r) for r in c.execute(
            "SELECT * FROM validation_results WHERE invoice_id = ? ORDER BY rowid", (invoice_id,))]
        duplicates = [dict(r) for r in c.execute(
            "SELECT dm.*, m.invoice_number AS matched_invoice_number FROM duplicate_matches dm "
            "LEFT JOIN invoices m ON m.id = dm.matched_invoice_id WHERE dm.invoice_id = ?", (invoice_id,))]
        stored_risk = c.execute("SELECT * FROM risk_assessments WHERE invoice_id = ?", (invoice_id,)).fetchone()
        audit_df = pd.read_sql_query(
            "SELECT created_at, action, entity_type, entity_id FROM audit_logs "
            "WHERE tenant_id = ? ORDER BY rowid DESC LIMIT 100", c, params=(tenant_id,))

    invoice = invoice_from_rows(row, item_rows)
    risk = {
        "score": row["risk_score"] or 0,
        "classification": row["risk_level"] or "Insufficient Evidence",
        "indicators": json.loads(stored_risk["indicators"]) if stored_risk else [],
        "recommendation": stored_risk["recommendation"] if stored_risk else "",
    }

    base = invoice.invoice_number or "report"
    col1, col2 = st.columns(2)
    with col1:
        st.download_button("Download Excel report", create_excel_report(invoice, findings, risk),
                           file_name=f"invoiceshield_{base}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           width="stretch")
    with col2:
        st.download_button("Download PDF report", create_pdf_report(invoice, findings, risk, duplicates),
                           file_name=f"invoiceshield_{base}.pdf", mime="application/pdf",
                           width="stretch")

    st.markdown('<div class="card-title" style="margin-top:1rem;">Audit trail</div>', unsafe_allow_html=True)
    st.markdown('<div class="card-help">Each event stores SHA-256(previous hash + event). '
                'Use the verifier to recompute the whole chain.</div>', unsafe_allow_html=True)

    if st.button("Verify audit chain integrity"):
        intact, checked, broken_at = verify_audit_chain(tenant_id)
        if intact:
            st.success(f"Audit chain intact — {checked} entr{'y' if checked == 1 else 'ies'} verified.")
        else:
            st.error(f"Audit chain BROKEN at entry #{broken_at} of {checked}. The log was altered.")

    if audit_df.empty:
        st.info("No audit events have been recorded yet.")
    else:
        st.dataframe(audit_df, width="stretch", hide_index=True)


# ---------------------------------------------------------------------
# Main navigation
# ---------------------------------------------------------------------


def main():
    with st.sidebar:
        st.markdown(
            '<div style="padding:.2rem 0 1.2rem;">'
            '<div style="font-size:1.25rem;font-weight:800;">🛡️ InvoiceShield AI</div>'
            '<div style="font-size:.74rem;color:#A9B7C6 !important;">Invoice verification prototype</div></div>',
            unsafe_allow_html=True,
        )

        page = st.radio(
            "Workflow",
            ["Home", "Upload & Analyze", "Invoice Analyzer", "Financial Validation",
             "Duplicate Detection", "PO / GRN Reconciliation", "Risk & Approval", "Reports & Audit"],
            label_visibility="collapsed",
        )

        st.markdown("---")
        st.session_state.require_po = st.checkbox(
            "Require PO reference",
            value=st.session_state.require_po,
            help="Untick for receipts and expense bills that are not raised against a purchase order.",
        )

        st.markdown("---")
        st.markdown(
            f"""
            <div class="small-muted">
              <b>Workspace</b><br/>Default Organization<br/><br/>
              <b>Version</b> {esc(APP_VERSION)}<br/>
              <b>Storage</b> Local SQLite<br/>
              <b>OCR</b> PyMuPDF text layer + EasyOCR<br/>
              <b>Encryption</b> {'Enabled' if ENCRYPTION_KEY else 'Disabled'}
            </div>
            """,
            unsafe_allow_html=True,
        )

    {
        "Home": render_home,
        "Upload & Analyze": render_upload,
        "Invoice Analyzer": render_analyzer,
        "Financial Validation": render_validation,
        "Duplicate Detection": render_duplicates,
        "PO / GRN Reconciliation": render_reconciliation,
        "Risk & Approval": render_risk,
        "Reports & Audit": render_reports,
    }[page]()


if __name__ == "__main__":
    main()
