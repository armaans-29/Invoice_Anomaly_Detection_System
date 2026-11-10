"""
invoice_core.py — InvoiceShield AI core logic.

Pure Python (no Streamlit, no database, no OpenCV), so every function here can
be unit-tested. Covers:
  * OCR boxes / PDF words  -> reconstructed text lines
  * field + line-item extraction
  * financial validation
  * duplicate detection
  * PO / GRN reconciliation
  * risk scoring
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24.3
except ImportError:  # older installs
    import fitz  # type: ignore
from dateutil import parser as date_parser
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

TOLERANCE = 0.05

# ---------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------


class LineItem(BaseModel):
    description: Optional[str] = None
    sku: Optional[str] = None
    hsn_sac: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    unit_price: Optional[float] = None
    tax_rate: Optional[float] = None
    line_total: Optional[float] = None
    source_page: Optional[int] = None
    confidence: float = 0.0


class Invoice(BaseModel):
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    due_date: Optional[str] = None
    supplier_name: Optional[str] = None
    supplier_gstin: Optional[str] = None
    buyer_name: Optional[str] = None
    po_reference: Optional[str] = None
    currency: Optional[str] = None
    tax_regime: Optional[str] = None
    subtotal: Optional[float] = None
    discount: float = 0.0
    shipping: float = 0.0
    tax_amount: Optional[float] = None
    tax_rate: Optional[float] = None
    cgst: float = 0.0
    sgst: float = 0.0
    igst: float = 0.0
    final_amount: Optional[float] = None
    items: List[LineItem] = Field(default_factory=list)
    extraction_quality: str = "Insufficient Evidence"
    field_confidence: Dict[str, float] = Field(default_factory=dict)
    notes: List[str] = Field(default_factory=list)  # informational
    corrections: List[str] = Field(default_factory=list)  # OCR repairs: need review


# ---------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def clean_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    text = re.sub(r"[£€$₹]|Rs\.?|INR|USD|EUR|GBP", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[^\d.,\-]", "", text)

    if not text or text in {"-", ".", ","}:
        return None

    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        parts = text.split(",")
        if len(parts[-1]) == 2:
            text = ".".join(parts[:-1]) + "." + parts[-1]
        else:
            text = text.replace(",", "")

    try:
        return float(text)
    except ValueError:
        return None


def money(value: Optional[float], currency: Optional[str] = None) -> str:
    if value is None:
        return "Not detected"
    symbols = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}
    return f"{symbols.get(currency or '', '')}{value:,.2f}"


# ---------------------------------------------------------------------
# Text reconstruction (OCR boxes and PDF words -> lines)
# ---------------------------------------------------------------------

Word = Tuple[float, float, float, float, str]  # x0, y0, x1, y1, text


def tidy_line(line: str) -> str:
    """Repair common OCR splits such as '30 .00' and '1, 250.00'."""
    line = re.sub(r"[ \t]+", " ", line)
    line = re.sub(r"(\d)\s+\.(\d{1,2})\b", r"\1.\2", line)
    line = re.sub(r"(\d),\s+(\d{3})\b", r"\1,\2", line)
    return line.strip()


def words_to_lines(words: Iterable[Word]) -> List[str]:
    """
    Group word/box fragments into visual rows by vertical centre, then order
    each row left-to-right. OCR engines return 'Invoice No:' and its value as
    separate boxes; this puts them back on one line.
    """
    items = [w for w in words if str(w[4]).strip()]
    if not items:
        return []

    heights = sorted(max(w[3] - w[1], 1.0) for w in items)
    median_height = heights[len(heights) // 2]

    ordered = sorted(items, key=lambda w: (w[1] + w[3]) / 2)
    rows: List[Dict[str, Any]] = []

    for word in ordered:
        centre = (word[1] + word[3]) / 2
        if rows and abs(centre - rows[-1]["centre"]) <= 0.55 * median_height:
            row = rows[-1]
            row["words"].append(word)
            row["centre"] = sum((w[1] + w[3]) / 2 for w in row["words"]) / len(row["words"])
        else:
            rows.append({"centre": centre, "words": [word]})

    lines = []
    for row in rows:
        ordered_words = sorted(row["words"], key=lambda w: w[0])
        lines.append(tidy_line(" ".join(str(w[4]).strip() for w in ordered_words)))
    return [line for line in lines if line]


def boxes_to_text(boxes: Iterable[Word]) -> str:
    return "\n".join(words_to_lines(boxes))


def pdf_text_layer(pdf_bytes: bytes) -> Tuple[str, int]:
    """Text-layer extraction, rebuilt into rows. Returns (text, page_count)."""
    pages: List[str] = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
        page_count = document.page_count
        for page in document:
            words = [(w[0], w[1], w[2], w[3], w[4]) for w in page.get_text("words")]
            pages.append("\n".join(words_to_lines(words)))
    return "\n".join(pages).strip(), page_count


# ---------------------------------------------------------------------
# Classification, currency and tax regime
# ---------------------------------------------------------------------


def classify_document(text: str) -> str:
    normalized = normalize(text)

    def score(terms: List[str]) -> int:
        return sum(len(re.findall(rf"\b{term}\b", normalized)) for term in terms)

    scores = {
        "Invoice": score(
            ["invoice", "tax invoice", "bill to", "bill no", "amount payable",
             "grand total", "vat", "gst"]
        ),
        "Purchase Order": score(
            ["purchase order", "po number", "ordered quantity", "po ref"]
        ),
        "Goods Receipt Note": score(
            ["goods receipt", "grn", "received quantity", "delivery note"]
        ),
    }

    best = max(scores, key=lambda key: scores[key])
    return best if scores[best] > 0 else "Unknown"


GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")


def detect_currency_and_tax(text: str) -> Tuple[Optional[str], Optional[str], List[str]]:
    normalized = normalize(text)
    notes: List[str] = []

    currency = None
    if re.search(r"₹|\brs\.?(?=\s|\d|$)|\binr\b", normalized):
        currency = "INR"
    elif "£" in text or re.search(r"\bgbp\b", normalized):
        currency = "GBP"
    elif "€" in text or re.search(r"\beur\b", normalized):
        currency = "EUR"
    elif "$" in text or re.search(r"\busd\b", normalized):
        currency = "USD"

    regime = None
    if re.search(r"\bvat\b", normalized):
        regime = "VAT"
    elif re.search(r"\bgst\b|\bcgst\b|\bsgst\b|\bigst\b", normalized) or GSTIN_RE.search(text):
        regime = "GST"

    # OCR engines frequently drop the rupee symbol entirely.
    if currency is None and regime == "GST":
        currency = "INR"
        notes.append(
            "Currency inferred as INR from GST context (no currency symbol was read)."
        )

    return currency, regime, notes


# ---------------------------------------------------------------------
# Field extraction helpers (all operate on reconstructed lines)
# ---------------------------------------------------------------------

CUR = r"(?:[£€$₹]|Rs\.?|INR|USD|EUR|GBP)"
AMOUNT_RE = re.compile(rf"{CUR}?\s*(-?\d[\d,]*(?:\.\d{{1,2}})?)", re.IGNORECASE)

DATE_RE = re.compile(
    r"(\d{1,2}\s*[A-Za-z]{3,9}\.?,?\s*\d{2,4}"
    r"|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4})"
)


def last_amount(text: str) -> Optional[float]:
    """Last money-like number in text, ignoring percentages."""
    found = None
    for match in AMOUNT_RE.finditer(text):
        after = text[match.end(1): match.end(1) + 3].lstrip()
        if after.startswith("%"):
            continue
        value = clean_number(match.group(1))
        if value is not None:
            found = value
    return found


def find_amount(lines: List[str], labels: List[str]) -> Optional[float]:
    for label in labels:
        pattern = re.compile(r"(?<![A-Za-z])" + label, re.IGNORECASE)
        found = None
        for line in lines:
            match = pattern.search(line)
            if not match:
                continue
            value = last_amount(line[match.end():])
            if value is not None and value > 0:
                found = value
        if found is not None:
            return found
    return None


def find_total_line(lines: List[str]) -> Optional[float]:
    """Plain 'TOTAL 394.40' line, excluding Subtotal / Total Tax / etc."""
    pattern = re.compile(
        r"^\W*total\b(?!\s*(?:tax|gst|vat|cgst|sgst|igst|discount|qty|quantity|items))",
        re.IGNORECASE,
    )
    found = None
    for line in lines:
        if pattern.search(line):
            value = last_amount(re.sub(r"^\W*total", "", line, flags=re.IGNORECASE))
            if value is not None and value > 0:
                found = value
    return found


def find_labeled_value(
    lines: List[str],
    labels: List[str],
    value_pattern: str,
    next_line: bool = False,
) -> Optional[str]:
    for label in labels:
        pattern = re.compile(
            r"(?<![A-Za-z])" + label + r"[ \t]*[:\-#]?[ \t]*" + value_pattern,
            re.IGNORECASE,
        )
        for line in lines:
            match = pattern.search(line)
            if match and match.group(1).strip():
                return match.group(1).strip(" ,.-")

        if next_line:
            bare = re.compile(r"(?<![A-Za-z])" + label + r"[ \t]*[:\-]?[ \t]*$", re.IGNORECASE)
            for index, line in enumerate(lines[:-1]):
                if bare.search(line) and re.search(r"[A-Za-z]{3,}", lines[index + 1]):
                    return lines[index + 1].strip(" ,.-")
    return None


def find_date(lines: List[str], labels: List[str]) -> Optional[str]:
    for label in labels:
        pattern = re.compile(r"(?<![A-Za-z])" + label, re.IGNORECASE)
        for line in lines:
            for match in pattern.finditer(line):
                prefix = line[max(0, match.start() - 9): match.start()].lower()
                if re.search(r"due|deliv|ship|valid|expir", prefix):
                    continue
                found = DATE_RE.search(line[match.end():])
                if not found:
                    continue
                raw = found.group(1).strip()
                try:
                    return date_parser.parse(raw, dayfirst=True).date().isoformat()
                except Exception:
                    return raw
    return None


SUPPLIER_SKIP = re.compile(
    r"invoice|receipt|bill\s*to|\bdate\b|\btotal\b|\bvat\b|\bgst|\bpage\b|"
    r"amount|^from\b|^to\b|^tax\b",
    re.IGNORECASE,
)


def extract_supplier_name(lines: List[str]) -> Optional[str]:
    for line in lines[:20]:
        if len(re.sub(r"[^A-Za-z]", "", line)) < 3:
            continue
        if SUPPLIER_SKIP.search(line):
            continue
        if 3 <= len(line) <= 80 and not re.search(r"\d{4,}", line):
            return line.strip(" -:|")
    return None


# ---------------------------------------------------------------------
# Line-item extraction
# ---------------------------------------------------------------------

SUMMARY_ROW_RE = re.compile(
    r"^\W*(sub\s*total|total|grand|vat|gst|cgst|sgst|igst|tax|discount|"
    r"shipping|freight|round\s*off|amount\s*payable|net\s*payable|balance)\b",
    re.IGNORECASE,
)

HEADER_COLUMN_RE = re.compile(
    r"(?P<hsn>hsn|sac)"
    r"|(?P<qty>qty|quantity|days|hours|nos)"
    r"|(?P<tax>(?:gst|vat|tax)\s*(?:rate|%|amount|amt)?)"
    r"|(?P<rate>unit\s*price|rate|price)"
    r"|(?P<amount>total|amount|value)",
    re.IGNORECASE,
)


def is_header_row(line: str) -> bool:
    text = normalize(line)
    return (
        ("description" in text or "item" in text or "particulars" in text)
        and any(t in text for t in ["qty", "quantity", "days", "hours", "nos"])
        and any(t in text for t in ["rate", "price", "unit"])
        and any(t in text for t in ["total", "amount", "value"])
    )


def header_columns(line: str) -> List[str]:
    columns: List[str] = []
    for match in HEADER_COLUMN_RE.finditer(line):
        kind = match.lastgroup or ""
        if kind and (not columns or columns[-1] != kind):
            columns.append(kind)
    return columns


NUMERIC_TAIL_RE = re.compile(
    rf"^(?P<desc>.*?)(?P<nums>(?:\s+{CUR}?\s?-?\d[\d,]*(?:\.\d+)?\s?%?)+)\s*$",
    re.IGNORECASE,
)
NUMERIC_TOKEN_RE = re.compile(rf"{CUR}?\s?-?\d[\d,]*(?:\.\d+)?\s?%?", re.IGNORECASE)


def extract_table_items(lines: List[str]) -> List[LineItem]:
    """Column-order based table parser: works for OCR text and PDF text alike."""
    header_index = next((i for i, l in enumerate(lines) if is_header_row(l)), None)
    if header_index is None:
        return []

    columns = header_columns(lines[header_index])
    if "amount" not in columns or "qty" not in columns:
        return []

    items: List[LineItem] = []
    current: Optional[LineItem] = None

    for line in lines[header_index + 1:]:
        if SUMMARY_ROW_RE.search(line):
            break

        match = NUMERIC_TAIL_RE.match(line)
        tokens = NUMERIC_TOKEN_RE.findall(match.group("nums")) if match else []

        if not tokens:
            if current and re.search(r"[A-Za-z]{2,}", line):
                current.description = f"{current.description} {line}".strip()
            continue

        if len(tokens) < len(columns):
            continue

        used = tokens[-len(columns):]
        extra = tokens[:-len(columns)]
        description = " ".join([match.group("desc").strip()] + [t.strip() for t in extra]).strip()
        if not description:
            continue

        values: Dict[str, Any] = {}
        for kind, token in zip(columns, used):
            values.setdefault(kind, token.strip())

        sku = re.search(
            r"\b(?:SKU|Item\s*Code|Product\s*ID)\s*[:\-]?\s*([A-Za-z0-9\-]+)",
            description, re.IGNORECASE,
        )
        quantity = clean_number(values.get("qty"))
        unit_price = clean_number(values.get("rate"))
        line_total = clean_number(values.get("amount"))
        tax_token = values.get("tax", "")

        current = LineItem(
            description=description,
            sku=sku.group(1) if sku else None,
            hsn_sac=values.get("hsn"),
            quantity=quantity,
            unit_price=unit_price,
            tax_rate=clean_number(tax_token) if tax_token.endswith("%") else None,
            line_total=line_total,
            confidence=80.0 if None not in (quantity, unit_price, line_total) else 45.0,
        )
        items.append(current)

    return items


BREAKUP_HEADER_RE = re.compile(
    r"^\W*(fare\s*break\s*up|break\s*up|particulars|charges|bill\s*details|fare\s*details)\W*$",
    re.IGNORECASE,
)
LABEL_AMOUNT_RE = re.compile(
    rf"^(?P<desc>[A-Za-z].*?)\s*[:\-]?\s*{CUR}?\s*(?P<amt>-?\d[\d,]*(?:\.\d{{1,2}})?)\s*$",
    re.IGNORECASE,
)
QTY_RATE_RE = re.compile(
    rf"\(\s*(\d[\d.,]*)\s*[A-Za-z]*\s*[x×*]\s*{CUR}?\s*(\d[\d.,]*)\s*\)",
    re.IGNORECASE,
)


def extract_label_amount_items(lines: List[str]) -> List[LineItem]:
    """Receipt-style 'Label: amount' rows under a FARE BREAKUP / CHARGES heading."""
    header_index = next((i for i, l in enumerate(lines) if BREAKUP_HEADER_RE.match(l)), None)
    if header_index is None:
        return []

    items: List[LineItem] = []
    for line in lines[header_index + 1:]:
        if SUMMARY_ROW_RE.search(line):
            break
        match = LABEL_AMOUNT_RE.match(line)
        if not match:
            continue

        amount = clean_number(match.group("amt"))
        description = match.group("desc").strip(" :-")
        if amount is None or not description:
            continue

        quantity, unit_price = 1.0, amount
        qty_rate = QTY_RATE_RE.search(description)
        if qty_rate:
            parsed_qty = clean_number(qty_rate.group(1))
            parsed_rate = clean_number(qty_rate.group(2))
            if parsed_qty and parsed_rate:
                quantity, unit_price = parsed_qty, parsed_rate

        items.append(
            LineItem(
                description=description,
                quantity=quantity,
                unit_price=unit_price,
                line_total=amount,
                confidence=70.0,
            )
        )
    return items


# ---------------------------------------------------------------------
# Invoice extraction
# ---------------------------------------------------------------------


def _strip_leading_digit(value: Optional[float]) -> Optional[float]:
    """OCR often reads the rupee sign as a digit ('₹394.40' -> '2394.40')."""
    if value is None:
        return None
    whole, _, frac = f"{value:.2f}".partition(".")
    if len(whole) < 2:
        return None
    return float(f"{whole[1:]}.{frac}")


def repair_ocr_amounts(invoice: Invoice) -> None:
    """
    Conservative repair, applied only to OCR text and only when the stripped value
    makes the invoice arithmetic reconcile. Every repair is recorded in
    invoice.corrections so a reviewer must confirm it against the source.
    """
    for item in invoice.items:
        if None in (item.quantity, item.unit_price, item.line_total):
            continue
        if abs(item.quantity * item.unit_price - item.line_total) <= TOLERANCE:
            continue
        for field in ("line_total", "unit_price"):
            candidate = _strip_leading_digit(getattr(item, field))
            if candidate is None:
                continue
            total = candidate if field == "line_total" else item.line_total
            price = candidate if field == "unit_price" else item.unit_price
            if abs(item.quantity * price - total) <= TOLERANCE:
                invoice.corrections.append(
                    f"Line '{item.description}': {field} OCR-corrected "
                    f"{getattr(item, field):.2f} -> {candidate:.2f}"
                )
                setattr(item, field, candidate)
                break

    if invoice.items and invoice.subtotal is not None:
        calc = sum(i.line_total or 0 for i in invoice.items)
        if abs(calc - invoice.subtotal) > TOLERANCE:
            candidate = _strip_leading_digit(invoice.subtotal)
            if candidate is not None and abs(calc - candidate) <= TOLERANCE:
                invoice.corrections.append(
                    f"Subtotal OCR-corrected {invoice.subtotal:.2f} -> {candidate:.2f}"
                )
                invoice.subtotal = candidate

    if (
        invoice.final_amount is not None
        and invoice.subtotal is not None
        and invoice.tax_amount is not None
    ):
        expected = invoice.subtotal - invoice.discount + invoice.shipping + invoice.tax_amount
        if abs(expected - invoice.final_amount) > TOLERANCE:
            candidate = _strip_leading_digit(invoice.final_amount)
            if candidate is not None and abs(expected - candidate) <= TOLERANCE:
                invoice.corrections.append(
                    f"Final amount OCR-corrected {invoice.final_amount:.2f} -> {candidate:.2f}"
                )
                invoice.final_amount = candidate


def extract_invoice(
    raw_text: str,
    text_confidence: float = 90.0,
    ocr_used: bool = False,
) -> Invoice:
    lines = [tidy_line(l) for l in raw_text.splitlines() if l.strip()]
    text = "\n".join(lines)

    currency, tax_regime, notes = detect_currency_and_tax(text)

    value_ref = r"((?=[A-Za-z0-9/\-_]*\d)[A-Za-z0-9][A-Za-z0-9/\-_]{1,29})"

    invoice_number = find_labeled_value(
        lines,
        [r"invoice\s*(?:no\.?|number|#)", r"bill\s*(?:no\.?|number|#)",
         r"receipt\s*(?:no\.?|number|#)", r"inv\s*(?:no\.?|#)"],
        value_ref,
    )
    invoice_date = find_date(
        lines,
        [r"invoice\s*date", r"bill\s*date", r"date\s*of\s*issue", r"issue\s*date", r"date"],
    )
    due_date = find_date(lines, [r"due\s*date", r"payment\s*due", r"due\s*by"])
    supplier_name = extract_supplier_name(lines)

    buyer_name = find_labeled_value(
        lines,
        [r"bill\s*to", r"sold\s*to", r"buyer(?:\s*name)?", r"customer\s*name", r"client\s*name"],
        r"([A-Za-z0-9&.,'\-() ]{3,80})",
        next_line=True,
    )

    gstin_match = GSTIN_RE.search(text)
    supplier_gstin = gstin_match.group(0) if gstin_match else find_labeled_value(
        lines,
        [r"gstin", r"gst\s*(?:registration\s*)?no\.?",
         r"vat\s*(?:registration\s*)?(?:no\.?|number)"],
        r"([A-Za-z0-9]{5,20})",
    )

    po_reference = find_labeled_value(
        lines,
        [r"po\s*(?:no\.?|number|#)", r"purchase\s*order\s*(?:no\.?|number|#)",
         r"po\s*ref(?:erence)?"],
        value_ref,
    )

    subtotal = find_amount(
        lines, [r"sub\s*total", r"taxable\s*(?:value|amount)", r"net\s*amount"]
    )
    discount = find_amount(lines, [r"discount", r"less\s*discount"]) or 0.0
    shipping = find_amount(
        lines, [r"shipping", r"freight", r"delivery\s*charge", r"carriage"]
    ) or 0.0

    cgst = find_amount(lines, [r"cgst"]) or 0.0
    sgst = find_amount(lines, [r"sgst"]) or 0.0
    igst = find_amount(lines, [r"igst"]) or 0.0

    tax_amount = find_amount(
        lines,
        [r"total\s*tax", r"tax\s*amount", r"gst\s*amount", r"vat\s*amount",
         r"total\s*(?:gst|vat)", r"tax\s*\(\s*(?:gst|vat)[^)]*\)",
         r"(?:vat|gst)\s*@\s*[\d.]+\s*%", r"tax\s*[:\-]"],
    )
    tax_derived = False
    if tax_amount is None and (cgst + sgst + igst) > 0:
        tax_amount, tax_derived = round(cgst + sgst + igst, 2), True

    rate_match = re.search(
        r"(?<![A-Za-z])(?:vat|gst)\s*@\s*([0-9]+(?:\.[0-9]+)?)\s*%", text, re.IGNORECASE
    )
    tax_rate = clean_number(rate_match.group(1)) if rate_match else None

    final_amount = find_amount(
        lines,
        [r"grand\s*total", r"total\s*amount", r"amount\s*payable", r"net\s*payable",
         r"total\s*payable", r"total\s*due", r"balance\s*due", r"amount\s*due"],
    )
    if final_amount is None:
        final_amount = find_total_line(lines)

    items = extract_table_items(lines) or extract_label_amount_items(lines)

    subtotal_derived = False
    if subtotal is None and items and all(i.line_total is not None for i in items):
        subtotal = round(sum(i.line_total or 0 for i in items), 2)
        subtotal_derived = True
        notes.append("Subtotal derived from line items (not printed on the document).")

    invoice = Invoice(
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        due_date=due_date,
        supplier_name=supplier_name,
        supplier_gstin=supplier_gstin,
        buyer_name=buyer_name,
        po_reference=po_reference,
        currency=currency,
        tax_regime=tax_regime,
        subtotal=subtotal,
        discount=discount,
        shipping=shipping,
        tax_amount=tax_amount,
        tax_rate=tax_rate,
        cgst=cgst,
        sgst=sgst,
        igst=igst,
        final_amount=final_amount,
        items=items,
        notes=notes,
    )

    if ocr_used:
        repair_ocr_amounts(invoice)

    def conf(present: bool, derived: bool = False) -> float:
        if not present:
            return 0.0
        return round(text_confidence * (0.8 if derived else 1.0), 1)

    item_conf = (
        sum(i.confidence for i in items) / len(items) * text_confidence / 100
        if items else 0.0
    )
    invoice.field_confidence = {
        "invoice_number": conf(bool(invoice.invoice_number)),
        "invoice_date": conf(bool(invoice.invoice_date)),
        "supplier_name": conf(bool(invoice.supplier_name), derived=True),
        "subtotal": conf(invoice.subtotal is not None, subtotal_derived),
        "tax_amount": conf(invoice.tax_amount is not None, tax_derived),
        "final_amount": conf(invoice.final_amount is not None),
        "line_items": round(item_conf, 1),
    }

    if invoice.invoice_number and invoice.final_amount is not None and items:
        invoice.extraction_quality = "Good"
    elif invoice.final_amount is not None or invoice.invoice_number or items:
        invoice.extraction_quality = "Partial"
    else:
        invoice.extraction_quality = "Insufficient Evidence"

    return invoice


# ---------------------------------------------------------------------
# Financial validation
# ---------------------------------------------------------------------


def _finding(check, result, severity, expected, actual, explanation, evidence) -> Dict[str, Any]:
    return {
        "check_name": check,
        "result": result,
        "severity": severity,
        "expected_value": expected,
        "actual_value": actual,
        "explanation": explanation,
        "evidence": evidence,
    }


def validate_invoice(invoice: Invoice, require_po: bool = True) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    cur = invoice.currency

    if invoice.extraction_quality == "Insufficient Evidence":
        return [
            _finding(
                "Invoice extraction quality", "Warning", "Extraction failure",
                "Reliable invoice structure", "Incomplete extraction",
                "The invoice layout could not be extracted reliably enough for financial "
                "validation. Review the source document and correct key fields manually.",
                "No invoice number, line items or totals could be identified.",
            )
        ]

    for correction in invoice.corrections:
        findings.append(
            _finding(
                "OCR correction applied", "Warning", "OCR correction",
                "Value as printed", "Value repaired by arithmetic consistency",
                "A likely currency-symbol misread was repaired because the corrected "
                "value makes the invoice reconcile. Confirm against the source document.",
                correction,
            )
        )

    if not invoice.items:
        findings.append(
            _finding(
                "Line items", "Warning", "Missing information",
                "At least one line item", "No line items detected",
                "No invoice line items could be extracted reliably. The invoice may use an "
                "unsupported table layout or be a low-quality scan.",
                "No structured line-item row was detected in the document.",
            )
        )

    calculated_subtotal = 0.0
    for index, item in enumerate(invoice.items, start=1):
        if None not in (item.quantity, item.unit_price, item.line_total):
            expected_total = item.quantity * item.unit_price
            if abs(item.line_total - expected_total) > TOLERANCE:
                findings.append(
                    _finding(
                        f"Line {index}: quantity × unit price", "Fail", "Mathematical error",
                        money(expected_total, cur), money(item.line_total, cur),
                        f"The line total does not match quantity multiplied by unit price. "
                        f"Expected {money(expected_total, cur)}, but the invoice shows "
                        f"{money(item.line_total, cur)}.",
                        f"Item: {item.description or 'Unnamed item'}\n"
                        f"Quantity: {item.quantity}\n"
                        f"Unit price: {money(item.unit_price, cur)}\n"
                        f"Stated line total: {money(item.line_total, cur)}",
                    )
                )
        if item.line_total is not None:
            calculated_subtotal += item.line_total

    if invoice.subtotal is not None and invoice.items:
        if abs(calculated_subtotal - invoice.subtotal) > TOLERANCE:
            findings.append(
                _finding(
                    "Subtotal verification", "Fail", "Mathematical error",
                    money(calculated_subtotal, cur), money(invoice.subtotal, cur),
                    f"The sum of extracted line totals is {money(calculated_subtotal, cur)}, "
                    f"but the invoice states a subtotal of {money(invoice.subtotal, cur)}.",
                    "Line-item totals were added independently and compared with the stated subtotal.",
                )
            )

    if invoice.final_amount is None:
        findings.append(
            _finding(
                "Final invoice amount", "Warning", "Missing information",
                "Final amount", "Not detected",
                "The final payable amount was not confidently extracted, so it cannot be verified.",
                "Final amount was not detected in the source document.",
            )
        )
    elif invoice.final_amount <= 0:
        findings.append(
            _finding(
                "Final invoice amount", "Warning", "Unverified information",
                "Positive payable amount", money(invoice.final_amount, cur),
                "The extracted final amount is zero or negative. Confirm whether this is a "
                "credit note, an extraction error, or a genuine zero-value document.",
                "Final amount must be manually verified.",
            )
        )
    elif invoice.tax_amount is None:
        findings.append(
            _finding(
                "Final invoice amount", "Warning", "Missing information",
                "Tax amount", "Not detected",
                "The final amount was detected, but the tax amount was not, so the final "
                "amount cannot be independently reconciled.",
                "Tax amount was not detected in the source document.",
            )
        )
    else:
        base = (
            (invoice.subtotal if invoice.subtotal is not None else calculated_subtotal)
            - invoice.discount + invoice.shipping
        )
        expected_final = base + invoice.tax_amount
        if abs(expected_final - invoice.final_amount) > TOLERANCE:
            findings.append(
                _finding(
                    "Final invoice amount", "Fail", "Mathematical error",
                    money(expected_final, cur), money(invoice.final_amount, cur),
                    f"The final amount does not reconcile. Expected {money(expected_final, cur)} "
                    f"from subtotal, discount, shipping and tax, but the invoice states "
                    f"{money(invoice.final_amount, cur)}.",
                    "Formula: subtotal - discount + shipping + tax = final amount",
                )
            )

    if invoice.tax_regime in ("VAT", "GST"):
        if None not in (invoice.tax_amount, invoice.subtotal, invoice.tax_rate):
            expected_tax = invoice.subtotal * (invoice.tax_rate / 100)
            if abs(expected_tax - invoice.tax_amount) > TOLERANCE:
                findings.append(
                    _finding(
                        f"{invoice.tax_regime} calculation", "Fail", "Tax inconsistency",
                        money(expected_tax, cur), money(invoice.tax_amount, cur),
                        f"The tax amount does not match the stated rate. Expected "
                        f"{money(expected_tax, cur)} at {invoice.tax_rate}% of "
                        f"{money(invoice.subtotal, cur)}.",
                        f"Rate: {invoice.tax_rate}%\nNet amount: {money(invoice.subtotal, cur)}\n"
                        f"Stated tax: {money(invoice.tax_amount, cur)}",
                    )
                )

    if invoice.tax_regime == "GST":
        components = invoice.cgst + invoice.sgst + invoice.igst
        if components > 0 and invoice.tax_amount is not None:
            if abs(components - invoice.tax_amount) > TOLERANCE:
                findings.append(
                    _finding(
                        "GST component reconciliation", "Fail", "Tax inconsistency",
                        money(invoice.tax_amount, cur), money(components, cur),
                        f"CGST, SGST and IGST together equal {money(components, cur)}, but the "
                        f"stated total tax is {money(invoice.tax_amount, cur)}.",
                        f"CGST: {money(invoice.cgst, cur)}\nSGST: {money(invoice.sgst, cur)}\n"
                        f"IGST: {money(invoice.igst, cur)}",
                    )
                )
        if invoice.igst > 0 and (invoice.cgst > 0 or invoice.sgst > 0):
            findings.append(
                _finding(
                    "GST mode conflict", "Warning", "Tax inconsistency",
                    "Either IGST or CGST + SGST", "IGST and CGST/SGST both detected",
                    "The invoice contains both IGST and CGST/SGST, which normally apply to "
                    "different supply types. Confirm the tax treatment with the supplier.",
                    f"CGST: {money(invoice.cgst, cur)}\nSGST: {money(invoice.sgst, cur)}\n"
                    f"IGST: {money(invoice.igst, cur)}",
                )
            )

    for name, value in {
        "Invoice number": invoice.invoice_number,
        "Invoice date": invoice.invoice_date,
        "Supplier name": invoice.supplier_name,
    }.items():
        if not value:
            findings.append(
                _finding(
                    f"Missing {name.lower()}", "Warning", "Missing information",
                    name, "Not detected",
                    f"{name} was not extracted reliably and should be checked manually.",
                    "This is a required field for a complete invoice review.",
                )
            )

    if require_po and not invoice.po_reference:
        findings.append(
            _finding(
                "Purchase order reference", "Warning", "Unverified information",
                "PO reference", "Not detected",
                "No purchase-order reference was found. Confirm whether this invoice is "
                "supported by an approved PO.",
                "PO reference was not detected in the extracted document text.",
            )
        )

    return findings


# ---------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------


def detect_duplicates(
    current: Dict[str, Any],
    history: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    matches = []

    c_number = normalize(current.get("invoice_number"))
    c_supplier = normalize(current.get("supplier_name"))
    c_amount = current.get("final_amount")
    c_date = normalize(current.get("invoice_date"))
    c_po = normalize(current.get("po_reference"))
    c_hash = current.get("file_hash")

    for prior in history:
        if prior.get("invoice_id") == current.get("invoice_id"):
            continue

        reasons: List[str] = []
        score = 0.0

        p_number = normalize(prior.get("invoice_number"))
        p_supplier = normalize(prior.get("supplier_name"))
        p_amount = prior.get("final_amount")
        p_date = normalize(prior.get("invoice_date"))
        p_po = normalize(prior.get("po_reference"))
        p_hash = prior.get("file_hash")

        if c_number and p_number:
            similarity = fuzz.ratio(c_number, p_number)
            if similarity >= 95:
                reasons.append(f"Invoice number similarity: {similarity:.1f}%")
                score += 40
            elif similarity >= 80:
                reasons.append(f"Similar invoice number: {similarity:.1f}%")
                score += 18

        if c_supplier and p_supplier:
            similarity = fuzz.ratio(c_supplier, p_supplier)
            if similarity >= 90:
                reasons.append(f"Supplier similarity: {similarity:.1f}%")
                score += 15

        if c_amount is not None and p_amount is not None:
            if abs(c_amount - p_amount) <= 0.01:
                reasons.append("Identical invoice amount")
                score += 20
            elif abs(c_amount - p_amount) <= max(1.0, p_amount * 0.01):
                reasons.append("Near-identical invoice amount")
                score += 10

        if c_date and p_date and c_date == p_date:
            reasons.append("Same invoice date")
            score += 10

        if c_po and p_po and c_po == p_po:
            reasons.append("Same purchase-order reference")
            score += 10

        if c_hash and p_hash and c_hash == p_hash:
            reasons.append("Identical source-file hash")
            score += 50

        if score >= 70:
            match_type = "Exact duplicate"
        elif score >= 45:
            match_type = "Probable duplicate"
        elif score >= 25:
            match_type = "Similar invoice — review"
        else:
            continue

        matches.append(
            {
                "matched_invoice_id": prior.get("invoice_id"),
                "matched_invoice_number": prior.get("invoice_number"),
                "match_type": match_type,
                "similarity": round(min(score, 100), 1),
                "reasons": "; ".join(reasons),
            }
        )

    return sorted(matches, key=lambda m: m["similarity"], reverse=True)


# ---------------------------------------------------------------------
# PO / GRN reconciliation (pure function; the app loads rows from SQLite)
# ---------------------------------------------------------------------


def _match_item(item: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    sku = normalize(item.get("sku"))
    if sku:
        for candidate in candidates:
            if normalize(candidate.get("sku")) == sku:
                return candidate

    description = normalize(item.get("description"))
    best, best_score = None, 0.0
    for candidate in candidates:
        score = fuzz.token_set_ratio(description, normalize(candidate.get("description")))
        if score > best_score:
            best, best_score = candidate, score
    return best if best_score >= 80 else None


def reconcile(
    invoice: Dict[str, Any],
    invoice_items: List[Dict[str, Any]],
    po: Optional[Dict[str, Any]],
    po_items: List[Dict[str, Any]],
    grn: Optional[Dict[str, Any]],
    grn_items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    cur = invoice.get("currency")
    results: List[Dict[str, Any]] = []

    def add(kind, result, severity, explanation, evidence):
        results.append(
            {
                "reconciliation_type": kind,
                "result": result,
                "severity": severity,
                "explanation": explanation,
                "evidence": evidence,
            }
        )

    if not invoice.get("po_reference"):
        add("Three-way match", "Warning", "Missing information",
            "No PO reference was found, so invoice-to-PO reconciliation cannot be performed.",
            "PO reference is missing from the invoice extraction.")
        return results

    if not po:
        add("Invoice to PO", "Warning", "Unverified information",
            f"PO reference {invoice['po_reference']} was not found in the PO register.",
            "No matching purchase order has been recorded in the application.")
    else:
        if fuzz.token_set_ratio(normalize(po.get("supplier_name")),
                                normalize(invoice.get("supplier_name"))) < 85:
            add("Supplier match", "Fail", "Supplier mismatch",
                "The invoice supplier does not match the supplier on the purchase order.",
                f"Invoice supplier: {invoice.get('supplier_name')}\n"
                f"PO supplier: {po.get('supplier_name')}")

        po_total = po.get("total_amount")
        if po_total is None and po_items:
            po_total = sum(i.get("line_total") or 0 for i in po_items)

        if po_total is None:
            add("Invoice to PO", "Warning", "Missing information",
                "The PO has no total amount or line items to compare against.",
                f"PO: {po.get('po_number')}")
        else:
            tolerance = max(TOLERANCE, abs(po_total) * 0.001)
            candidates = [v for v in (invoice.get("subtotal"), invoice.get("final_amount")) if v is not None]
            if candidates and not any(abs(po_total - v) <= tolerance for v in candidates):
                add("Invoice to PO", "Fail", "Price or quantity discrepancy",
                    f"PO total {money(po_total, cur)} matches neither the invoice subtotal nor "
                    f"the final amount.",
                    f"PO: {po.get('po_number')}\nPO total: {money(po_total, cur)}\n"
                    f"Invoice subtotal: {money(invoice.get('subtotal'), cur)}\n"
                    f"Invoice final: {money(invoice.get('final_amount'), cur)}")

        for item in invoice_items:
            if not po_items:
                break
            matched = _match_item(item, po_items)
            name = item.get("description") or "Unnamed item"
            if not matched:
                add("Invoice to PO (item)", "Warning", "Unverified information",
                    f"Invoice item '{name}' was not found on the purchase order.",
                    f"Invoice item: {name}")
                continue
            if (item.get("quantity") is not None and matched.get("quantity") is not None
                    and item["quantity"] > matched["quantity"] + 1e-9):
                add("Invoice to PO (item)", "Fail", "Quantity overbilling",
                    f"'{name}' is billed at {item['quantity']} but only {matched['quantity']} were ordered.",
                    f"Invoiced: {item['quantity']}\nOrdered: {matched['quantity']}")
            if (item.get("unit_price") is not None and matched.get("unit_price") is not None
                    and abs(item["unit_price"] - matched["unit_price"]) > TOLERANCE):
                add("Invoice to PO (item)", "Fail", "Price or quantity discrepancy",
                    f"'{name}' unit price differs from the PO.",
                    f"Invoiced: {money(item['unit_price'], cur)}\n"
                    f"PO price: {money(matched['unit_price'], cur)}")

    if not grn:
        add("Invoice to GRN", "Warning", "Unverified information",
            "No goods receipt was found for this PO, so the three-way match is incomplete.",
            f"PO reference: {invoice['po_reference']}")
    elif not grn_items:
        add("Invoice to GRN", "Warning", "Missing information",
            "The goods receipt has no line items, so received quantities cannot be checked.",
            f"GRN: {grn.get('grn_number')}")
    else:
        for item in invoice_items:
            matched = _match_item(item, grn_items)
            name = item.get("description") or "Unnamed item"
            if not matched:
                add("Invoice to GRN (item)", "Warning", "Unverified information",
                    f"Invoice item '{name}' has no matching goods receipt line.",
                    f"GRN: {grn.get('grn_number')}")
                continue
            received = matched.get("quantity_received")
            if (item.get("quantity") is not None and received is not None
                    and item["quantity"] > received + 1e-9):
                add("Invoice to GRN (item)", "Fail", "Quantity overbilling",
                    f"'{name}' is billed at {item['quantity']} but only {received} were received.",
                    f"GRN: {grn.get('grn_number')}\nInvoiced: {item['quantity']}\nReceived: {received}")

    if not results:
        add("Three-way match", "Pass", "No issue",
            "Invoice, purchase order and goods receipt data reconciled successfully.",
            "No reconciliation exception was identified.")
    return results


# ---------------------------------------------------------------------
# Risk assessment
# ---------------------------------------------------------------------

PASSED = "Passed Checks"


def assess_risk(
    invoice: Invoice,
    findings: List[Dict[str, Any]],
    duplicates: List[Dict[str, Any]],
    reconciliation: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[float, str, List[str], str]:
    reconciliation = reconciliation or []

    if invoice.extraction_quality == "Insufficient Evidence":
        return (
            0.0,
            "Insufficient Evidence",
            ["Invoice structure could not be extracted reliably"],
            "Manual review is required. Not enough information could be extracted to "
            "perform financial validation.",
        )

    weights = {
        "Mathematical error": ("Mathematical issue", 20),
        "Tax inconsistency": ("Tax issue", 18),
        "OCR correction": ("OCR correction", 8),
        "Missing information": ("Missing information", 6),
        "Unverified information": ("Unverified information", 4),
    }

    score = 0.0
    indicators: List[str] = []

    for item in findings:
        if item["severity"] in weights:
            label, points = weights[item["severity"]]
            score += points
            indicators.append(f"{label}: {item['check_name']}")

    for dup in duplicates:
        if dup["match_type"] == "Exact duplicate":
            score += 35
            indicators.append("Exact duplicate detected")
        elif dup["match_type"] == "Probable duplicate":
            score += 22
            indicators.append("Probable duplicate detected")
        else:
            score += 8
            indicators.append("Similar historical invoice requires review")

    for rec in reconciliation:
        if rec["result"] == "Fail":
            score += 15
            indicators.append(f"Reconciliation failure: {rec['reconciliation_type']}")
        elif rec["result"] == "Warning":
            score += 3
            indicators.append(f"Reconciliation gap: {rec['reconciliation_type']}")

    score = round(min(score, 100), 1)

    if not indicators:
        classification = PASSED
    elif score >= 51:
        classification = "High Risk"
    else:
        classification = "Review Required"

    recommendation = {
        PASSED: "No configured validation exceptions were identified. Retain the invoice and "
                "supporting evidence for audit. This is an automated arithmetic and consistency "
                "check, not proof that the invoice is genuine.",
        "High Risk": "Escalate for finance review. Verify supplier identity, invoice calculations, "
                     "tax treatment and duplicate status before payment.",
        "Review Required": "Review the flagged items. Correct any extraction errors, obtain missing "
                           "information and compare the invoice with its supporting PO.",
    }[classification]

    return score, classification, indicators, recommendation
