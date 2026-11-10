#  Invoice Detection System

A Streamlit prototype that reads invoices (PDF or image), extracts the key fields and
line items, **independently re-checks the arithmetic and tax**, flags duplicates, reconciles
against purchase orders and goods receipts, and records every reviewer action in a
**hash-chained audit log**.

> **Scope:** this is a portfolio prototype built to demonstrate OCR-based document
> processing and finance controls. It is not a production accounts-payable system
> (see [Limitations](#limitations)).

## What it does

| Stage | Details |
|---|---|
| **Read** | PDF text layer via PyMuPDF; scanned PDFs and images via EasyOCR. OCR boxes are regrouped into visual rows so `Invoice No:` and its value end up on one line. |
| **Extract** | Invoice no., dates, supplier, GSTIN/VAT id, buyer, PO ref, currency, tax regime, subtotal, discount, shipping, CGST/SGST/IGST, total, and line items (column-order table parser + receipt-style `Label: amount` parser). |
| **Validate** | qty × rate = line total · Σ lines = subtotal · subtotal − discount + shipping + tax = total · VAT/GST rate check · CGST+SGST+IGST = total tax · IGST/CGST conflict · missing required fields. |
| **Duplicates** | Fuzzy invoice-number / supplier match, amount, date, PO and file hash. Scored as exact / probable / similar. |
| **Reconcile** | Per-item invoice ↔ PO (price, quantity, supplier) and invoice ↔ GRN (over-billing). |
| **Risk** | Transparent additive score (0–100) with the list of indicators behind it. |
| **Approve** | Approve / reject / request info, with reviewer comment. |
| **Audit** | Each event stores `SHA-256(previous_hash + event)`. A **Verify** button recomputes the chain and reports the first altered entry. |
| **Reports** | Per-invoice PDF and Excel. |

### Design choices worth knowing
* **"Insufficient Evidence" instead of guessing.** If the layout can't be read, the app says so rather than raising false math errors.
* **OCR repair is conservative and never silent.** OCR often reads `₹394.40` as `2394.40`. On OCR text only, a stray leading digit is removed *only if* that makes the invoice reconcile, and the invoice is then marked **Review Required** with the repair shown as a finding.
* **Currency inference is disclosed.** If no currency symbol is read but GST is present, currency is set to INR and a note says so.
* **"Passed Checks" ≠ "genuine".** It means no configured rule fired. It does not verify that the supplier or invoice is real.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run InvoiceShield.py
```

Try the files in `samples/`:

| File | Expected result |
|---|---|
| `sample_01_vat_invoice_clean.pdf` | Passed Checks |
| `sample_02_gst_invoice_clean.pdf` | Passed Checks (CGST + SGST, HSN column) |
| `sample_03_gst_invoice_errors.pdf` | **High Risk** — line math, subtotal, total and GST component errors |
| `sample_04_duplicate_of_01.pdf` | **Exact duplicate** of sample 01 (upload 01 first) |
| `sample_05_cab_receipt.pdf` | Passed Checks (untick *Require PO reference* in the sidebar) |

Regenerate them with `python samples/generate_samples.py`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

31 tests cover extraction, validation, duplicates, reconciliation and risk scoring. They
include a regression test built from **real OCR output of a taxi receipt**
(`tests/fixtures/cab_receipt_ocr_boxes.json`) that originally failed (wrong supplier,
missing date/tax/line items, wrong amount and currency).

## Project layout

```
InvoiceShield.py     Streamlit UI, SQLite persistence, OCR wiring, reports
invoice_core.py      Pure logic: extraction, validation, duplicates, reconciliation, risk
samples/             Sample invoices + generator
tests/               pytest suite and OCR fixture
```

## Configuration

| Variable | Purpose |
|---|---|
| `INVOICE_SHIELD_ENCRYPTION_KEY` | Fernet key. When set, uploaded files and stored OCR text are encrypted at rest. |
| `INVOICE_SHIELD_DB_PATH` | SQLite file (default `invoiceshield_v5.db`) |
| `INVOICE_SHIELD_UPLOAD_DIR` | Where originals are kept (default `invoiceshield_uploads/`) |
| `INVOICE_SHIELD_MAX_FILE_MB` | Upload limit (default 25) |

Generate a key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`

## Limitations

* **Rule-based extraction.** Parsing is regex/heuristic and tuned to common invoice layouts. Unusual layouts will return *Partial* or *Insufficient Evidence*, which the UI is designed to show clearly.
* **English-only OCR**, and accuracy depends on scan quality. Always check amounts against the source for scanned documents.
* **No authentication or user management.** It is a single-workspace, single-user app intended for local use. If you host it, put it behind your own auth (reverse proxy / SSO).
* **SQLite**, single workspace (the schema is tenant-scoped, but there is no tenant switching UI).
* **PO/GRN risk scoring is opt-in:** gaps (e.g. "PO not found") only affect the risk score once you have recorded at least one PO or GRN.
* Not a fraud-detection or tax-compliance guarantee.
