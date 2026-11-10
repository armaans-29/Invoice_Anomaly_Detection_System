<div align="center">

#  Invoice Anomaly Detection System

### Trust, but verify: every invoice, every number, every decision.

**An invoice-verification engine that reads documents with OCR, re-derives the math independently, reconciles against purchase orders and goods receipts, and records every human decision in a tamper-evident audit trail.**

<br>

[![Live Demo](https://img.shields.io/badge/🚀_Live_Demo-Open_the_App-FF4B4B?style=for-the-badge)](https://invoice-anomaly-detection-system.streamlit.app/)


</div>

---

Open the app, upload an invoice (PDF or image), and watch it extract, verify, score and explain, with no installation needed. If the app has been idle, it may take a short moment to wake up.

**Suggested 60-second tour:**
1. Upload `sample_01_vat_invoice_clean.pdf` → **Passed Checks**.
2. Upload `sample_03_gst_invoice_errors.pdf` → **High Risk**, with every arithmetic and tax error pinpointed.
3. Upload `sample_04_duplicate_of_01.pdf` → **Exact duplicate** detected.
4. Approve or reject an invoice, then press **Verify** on the audit log to see the hash chain confirmed.

---

## Table of Contents

1. [The Problem](#1-the-problem)
2. [The Solution](#2-the-solution)
3. [How It Works](#3-how-it-works)
4. [OCR Deep Dive](#4-ocr-deep-dive)
5. [Engineering Decisions](#5-engineering-decisions)
6. [Architecture](#6-architecture)
7. [Quick Start](#7-quick-start)
8. [Demo Dataset](#8-demo-dataset)
9. [Quality Assurance](#9-quality-assurance)
10. [Configuration & Security](#10-configuration--security)
11. [Scope & Limitations](#11-scope--limitations)

---

## 1. The Problem

Accounts-payable teams approve payments based on a document that arrives from *outside* the organisation and is, by default, **trusted**. That trust is where the risk lives.

| Failure mode | Why it is hard to catch by eye |
|---|---|
| **Arithmetic and tax errors** | A wrong line total, a subtotal that doesn't add up, or CGST/SGST components that don't sum to the stated tax are easy to miss in a dense document. |
| **Duplicate submissions** | The same invoice re-sent with a tweaked number, a re-scanned copy, or a different file name slips past exact-match checks. |
| **Over-billing** | Quantities or prices that differ from the purchase order, or billing for goods never received, only show up when three documents are compared side by side. |
| **Unreadable inputs** | Invoices arrive as scans and photos. Machines must *read* them first, and reading errors silently corrupt every number that follows. |
| **Opaque approvals** | Ordinary database records can be edited after the fact, so there is no reliable proof of who approved what, and when. |
| **Black-box automation** | Many tools return a confident answer with no explanation, and guess when they cannot read a document. |

The real requirement is not just to *extract* data from an invoice. It is to **independently verify it, explain every finding, and prove the review process was not altered afterwards.**

---

## 2. The Solution

InvoiceShield AI treats every invoice as a **claim to be audited**, not a fact to be ingested.

### System Overview: The Invoice Verification Tree

One invoice, eight branches. Read the tree from left to right: the **numbers ① – ⑧** show the order in which an invoice travels, and every leaf is a real tool, check or outcome inside the project.

```mermaid
flowchart LR
    ROOT(("🛡️<br/><b>INVOICE<br/>ANOMALY<br/>DETECTION<br/>SYSTEM</b>"))

    %% ───────────── ① INGEST ─────────────
    ROOT ==> B1["<b>① INGEST</b><br/>read any invoice"]
    B1 --> I1["📄 Digital PDF<br/>has a text layer"]
    I1 --> I1a["<b>PyMuPDF</b><br/>exact characters"]
    B1 --> I2["🖼️ Scan or image<br/>no text layer"]
    I2 --> I2a["<b>EasyOCR</b><br/>detect + recognise"]
    I2a --> I2b["<b>Box regrouping</b><br/>boxes → visual rows"]

    %% ───────────── ② EXTRACT ─────────────
    ROOT ==> B2["<b>② EXTRACT</b><br/>turn text into fields"]
    B2 --> E1["🏷️ Header fields"]
    E1 --> E1a["invoice no · dates · supplier<br/>GSTIN / VAT · buyer · PO ref"]
    B2 --> E2["💰 Amounts"]
    E2 --> E2a["subtotal · discount · shipping<br/>CGST · SGST · IGST · total"]
    B2 --> E3["🧾 Line items"]
    E3 --> E3a["<b>Table parser</b><br/>column-order aware"]
    E3 --> E3b["<b>Receipt parser</b><br/>Label: amount"]
    B2 --> E4["🩹 OCR repair"]
    E4 --> E4a["drop stray leading digit<br/>only if invoice reconciles"]
    E4a --> E4b["⚠️ Review Required<br/>repair shown as a finding"]
    B2 --> E5["💱 Currency"]
    E5 --> E5a["GST found, no symbol<br/>→ INR, inference disclosed"]

    %% ───────────── ③ VALIDATE ─────────────
    ROOT ==> B3["<b>③ VALIDATE</b><br/>re-compute the math"]
    B3 --> V1["📏 Line level"]
    V1 --> V1a["qty × rate = line total"]
    B3 --> V2["➕ Totals"]
    V2 --> V2a["Σ lines = subtotal"]
    V2 --> V2b["subtotal − discount<br/>+ shipping + tax = total"]
    B3 --> V3["🏛️ Tax"]
    V3 --> V3a["VAT / GST rate check"]
    V3 --> V3b["CGST + SGST + IGST<br/>= total tax"]
    V3 --> V3c["IGST vs CGST/SGST<br/>conflict"]
    B3 --> V4["📋 Completeness"]
    V4 --> V4a["missing required fields"]

    %% ───────────── ④ DUPLICATES ─────────────
    ROOT ==> B4["<b>④ DUPLICATES</b><br/>have we seen this?"]
    B4 --> D1["🔍 Signals compared"]
    D1 --> D1a["fuzzy invoice no<br/>+ supplier"]
    D1 --> D1b["amount · date<br/>PO reference"]
    D1 --> D1c["file hash"]
    B4 --> D2["⚖️ Verdict"]
    D2 --> D2a["🔴 Exact"]
    D2 --> D2b["🟠 Probable"]
    D2 --> D2c["🟡 Similar"]

    %% ───────────── ⑤ RECONCILE ─────────────
    ROOT ==> B5["<b>⑤ RECONCILE</b><br/>match the paperwork"]
    B5 --> R1["📑 Invoice ↔ Purchase Order"]
    R1 --> R1a["price"]
    R1 --> R1b["quantity"]
    R1 --> R1c["supplier"]
    B5 --> R2["📦 Invoice ↔ Goods Receipt"]
    R2 --> R2a["over-billing<br/>detection"]

    %% ───────────── ⑥ RISK ─────────────
    ROOT ==> B6["<b>⑥ RISK</b><br/>explainable score"]
    B6 --> K1["🎯 Additive score<br/>0 – 100"]
    K1 --> K1a["every point traces to<br/>a named indicator"]
    B6 --> K2["🚦 Outcome"]
    K2 --> K2a["✅ Passed Checks"]
    K2 --> K2b["🟡 Review Required"]
    K2 --> K2c["🔴 High Risk"]
    K2 --> K2d["⚪ Insufficient Evidence"]

    %% ───────────── ⑦ DECIDE & AUDIT ─────────────
    ROOT ==> B7["<b>⑦ DECIDE & AUDIT</b><br/>human in control"]
    B7 --> H1["👤 Reviewer action"]
    H1 --> H1a["Approve"]
    H1 --> H1b["Reject"]
    H1 --> H1c["Request info"]
    H1 --> H1d["+ comment"]
    B7 --> H2["🔗 Hash-chained log"]
    H2 --> H2a["<b>SHA-256</b><br/>(previous hash + event)"]
    H2a --> H2b["✔ Verify → pinpoints<br/>first altered entry"]

    %% ───────────── ⑧ OUTPUT & STORAGE ─────────────
    ROOT ==> B8["<b>⑧ OUTPUT & STORAGE</b><br/>keep it safe"]
    B8 --> O1["📊 Reports"]
    O1 --> O1a["PDF"]
    O1 --> O1b["Excel"]
    B8 --> O2["🗄️ Storage"]
    O2 --> O2a["<b>SQLite</b>"]
    O2 --> O2b["<b>Fernet</b><br/>encryption at rest"]
    B8 --> O3["🖥️ Interface"]
    O3 --> O3a["<b>Streamlit</b><br/>web app"]

    %% ───────────── STYLES ─────────────
    classDef root fill:#0D1B2A,stroke:#00B4D8,stroke-width:4px,color:#FFFFFF,font-size:16px
    classDef b1 fill:#EF6C00,stroke:#BF360C,stroke-width:3px,color:#FFFFFF
    classDef b2 fill:#7B1FA2,stroke:#4A148C,stroke-width:3px,color:#FFFFFF
    classDef b3 fill:#2E7D32,stroke:#1B5E20,stroke-width:3px,color:#FFFFFF
    classDef b4 fill:#C62828,stroke:#7F0000,stroke-width:3px,color:#FFFFFF
    classDef b5 fill:#1565C0,stroke:#0D47A1,stroke-width:3px,color:#FFFFFF
    classDef b6 fill:#F9A825,stroke:#F57F17,stroke-width:3px,color:#212121
    classDef b7 fill:#00838F,stroke:#006064,stroke-width:3px,color:#FFFFFF
    classDef b8 fill:#455A64,stroke:#263238,stroke-width:3px,color:#FFFFFF

    classDef l1 fill:#FFF3E0,stroke:#EF6C00,color:#E65100
    classDef l2 fill:#F3E5F5,stroke:#7B1FA2,color:#4A148C
    classDef l3 fill:#E8F5E9,stroke:#2E7D32,color:#1B5E20
    classDef l4 fill:#FFEBEE,stroke:#C62828,color:#B71C1C
    classDef l5 fill:#E3F2FD,stroke:#1565C0,color:#0D47A1
    classDef l6 fill:#FFFDE7,stroke:#F9A825,color:#8D6E00
    classDef l7 fill:#E0F7FA,stroke:#00838F,color:#006064
    classDef l8 fill:#ECEFF1,stroke:#455A64,color:#263238

    class ROOT root
    class B1 b1
    class B2 b2
    class B3 b3
    class B4 b4
    class B5 b5
    class B6 b6
    class B7 b7
    class B8 b8
    class I1,I1a,I2,I2a,I2b l1
    class E1,E1a,E2,E2a,E3,E3a,E3b,E4,E4a,E4b,E5,E5a l2
    class V1,V1a,V2,V2a,V2b,V3,V3a,V3b,V3c,V4,V4a l3
    class D1,D1a,D1b,D1c,D2,D2a,D2b,D2c l4
    class R1,R1a,R1b,R1c,R2,R2a l5
    class K1,K1a,K2,K2a,K2b,K2c,K2d l6
    class H1,H1a,H1b,H1c,H1d,H2,H2a,H2b l7
    class O1,O1a,O1b,O2,O2a,O2b,O3,O3a l8
```

**How to read it:** the thick **trunk** lines carry an invoice from the root into each stage. The **branches** are the stages, the **twigs** are the capabilities inside each stage, and the **leaves** are the concrete tools, checks and outcomes you can see working in the live app.

**Four principles guide the design:**

1. **Independent verification.** The system never trusts the totals printed on the invoice. It recomputes quantity × rate, line sums, subtotal, discount, shipping, tax and grand total from scratch.
2. **Explainability over magic.** The risk score is additive and transparent. Every point traces back to a named indicator.
3. **Honesty about uncertainty.** When a layout can't be read reliably, the system reports *Insufficient Evidence* instead of inventing errors or guessing values.
4. **Tamper-evident accountability.** Every reviewer action is cryptographically chained, so any later alteration is detectable.

---

## 3. How It Works

| Stage | Capability |
|---|---|
| **Read** | Text-layer extraction from digital PDFs via PyMuPDF. Scanned PDFs and images are read with EasyOCR. |
| **Extract** | Invoice number, dates, supplier, GSTIN / VAT ID, buyer, PO reference, currency, tax regime, subtotal, discount, shipping, CGST / SGST / IGST, total, and line items. Two parsers cover tabular invoices (column-order aware) and receipt-style `Label: amount` documents. |
| **Validate** | Seven independent checks:<br>• qty × rate = line total<br>• Σ lines = subtotal<br>• subtotal − discount + shipping + tax = total<br>• VAT / GST rate sanity check<br>• CGST + SGST + IGST = total tax<br>• IGST vs. CGST/SGST conflict<br>• missing required fields |
| **Detect duplicates** | Fuzzy matching on invoice number and supplier, plus amount, date, PO reference and file hash. Matches are graded **exact**, **probable** or **similar**. |
| **Reconcile** | Per-item invoice ↔ purchase order (price, quantity, supplier) and invoice ↔ goods receipt note (over-billing detection). |
| **Score risk** | A transparent additive score from 0 to 100, shown with the list of indicators behind it. |
| **Decide** | Approve, reject, or request more information, each with a reviewer comment. |
| **Audit** | Every event stores `SHA-256(previous_hash + event)`. A **Verify** button recomputes the chain and pinpoints the first altered entry. |
| **Report** | One-click per-invoice export to PDF and Excel. |

---

## 4. OCR Deep Dive

OCR is the foundation of this project, and also its most fragile layer. Everything downstream (validation, duplicate detection, reconciliation) is only as reliable as the text it receives. This project is built around that fact.

### 4.1 Two reading paths, chosen automatically

| Input | Method | Why |
|---|---|---|
| Digital PDF with a text layer | **PyMuPDF** reads the embedded text directly | Exact characters, no recognition error. Using OCR here would only add noise. |
| Scanned PDF or image | **EasyOCR** detects text regions and recognises the characters | The only option when no text layer exists. |

### 4.2 The hidden problem: OCR returns *boxes*, not *lines*

OCR engines return a flat list of text fragments, each with a bounding box. They do **not** return the document in reading order. On an invoice, a label and its value often come back as separate, unrelated fragments:

```
 Raw OCR output:      "Invoice No:"     "INV-2041"       (two separate boxes)
 After regrouping:    "Invoice No: INV-2041"              (one visual row)
```

Without a fix, field extraction fails, because a pattern like `Invoice No: <value>` never finds its value. InvoiceShield **regroups OCR boxes spatially into visual rows** before any parsing happens, so the text behaves the way a human sees the page. This single step is what makes label-and-value extraction work on scanned documents.

### 4.3 Table parsing that understands column order

Line items are rarely written as sentences. They sit in columns: description, quantity, rate, amount. The table parser is **column-order aware**, and a separate receipt-style parser handles `Label: amount` layouts such as taxi and retail receipts. Having both means the system adapts to the document's structure instead of forcing one layout on everything.

### 4.4 Handling OCR's characteristic mistakes

OCR does not fail randomly. It fails in *patterns*, and the pipeline is designed around them:

| OCR behaviour | Example | How InvoiceShield responds |
|---|---|---|
| Currency symbol read as a digit | `₹394.40` → `2394.40` | **Conservative repair**: on OCR text only, a stray leading digit is removed *only if doing so makes the invoice reconcile*. The invoice is then marked **Review Required** and the repair is displayed as a finding. |
| Currency symbol missing entirely | no `₹` / `$` detected | If GST is present, currency is set to INR and a note **discloses the inference**. |
| Layout too ambiguous to parse | unusual or damaged scan | Reports **Insufficient Evidence** instead of raising false math errors. |

### 4.5 Why the repair is safe

A guess can be dangerous, so the repair rule is deliberately narrow:

* It runs **only on OCR output**, never on a digital PDF's exact text.
* It acts **only if the corrected number makes the arithmetic reconcile**, so the invoice's own internal consistency acts as proof.
* It is **never silent**: the invoice is flagged for human review and the exact change is shown.

The system uses the invoice's own arithmetic as an error-correcting code, which lets it fix a probable OCR mistake without ever hiding it from the reviewer.

### 4.6 OCR quality is tested against real data

The test suite includes a regression built from the **real OCR output of a taxi receipt** (`tests/fixtures/cab_receipt_ocr_boxes.json`). It originally failed in five ways (wrong supplier, missing date, missing tax, missing line items, wrong amount and currency), and it now guards against those failures returning.

---

## 5. Engineering Decisions

### 🔍 "Insufficient Evidence" instead of guessing
If the layout can't be parsed with confidence, the app says so. A system that knows what it doesn't know is more trustworthy than one that always gives an answer.

### 🛠️ Repairs are conservative and never silent
Every automatic correction is justified by the arithmetic and surfaced to a human reviewer.

### 💱 Inferences are disclosed
When the system fills a gap (such as currency), it tells the reviewer that it did so.

### ✅ "Passed Checks" ≠ "Genuine"
The label means that *no configured rule fired*. It makes no claim that the supplier or invoice is real. The interface and documentation are deliberate about not overstating what automated checks can prove.

### ⛓️ Hash-chained audit log
Each entry commits to the one before it. Editing, deleting or reordering any past record breaks every hash after it, and **Verify** reports exactly where the chain was first altered. This is the same principle that underpins append-only ledgers.

### 🎯 PO / GRN scoring is opt-in
Gaps such as "PO not found" affect the risk score only after at least one PO or GRN has been recorded, so users without reconciliation data aren't penalised.

---

## 6. Architecture

```
InvoiceShield.py     Streamlit UI, SQLite persistence, OCR wiring, reports
invoice_core.py      Pure logic: extraction, validation, duplicates, reconciliation, risk
samples/             Sample invoices + generator
tests/               pytest suite and OCR fixture
```

**Separation of concerns:** all decision logic lives in `invoice_core.py` as pure functions with no UI or database dependencies. That makes the core independently testable and reusable behind a different interface (an API, a batch job or a CLI).

---

## 7. Quick Start

**Fastest:** use the hosted app, **[no installation required →](https://invoice-anomaly-detection-system.streamlit.app/)**

**Run locally:**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run InvoiceShield.py
```

---

## 8. Demo Dataset

Ready-made samples in `samples/` exercise each behaviour:

| File | Expected result |
|---|---|
| `sample_01_vat_invoice_clean.pdf` | ✅ Passed Checks |
| `sample_02_gst_invoice_clean.pdf` | ✅ Passed Checks (CGST + SGST, HSN column) |
| `sample_03_gst_invoice_errors.pdf` | 🔴 **High Risk**: line math, subtotal, total and GST component errors |
| `sample_04_duplicate_of_01.pdf` | 🟠 **Exact duplicate** of sample 01 (upload 01 first) |
| `sample_05_cab_receipt.pdf` | ✅ Passed Checks (untick *Require PO reference* in the sidebar) |

Regenerate them at any time: `python samples/generate_samples.py`

---

## 9. Quality Assurance

```bash
pip install -r requirements-dev.txt
pytest -q
```

**31 automated tests** cover extraction, validation, duplicate detection, reconciliation and risk scoring, including the real-OCR regression described in [§4.6](#46-ocr-quality-is-tested-against-real-data).

---

## 10. Configuration & Security

| Variable | Purpose |
|---|---|
| `INVOICE_SHIELD_ENCRYPTION_KEY` | Fernet key. When set, uploaded files and stored OCR text are **encrypted at rest**. |
| `INVOICE_SHIELD_DB_PATH` | SQLite file (default `invoiceshield_v5.db`) |
| `INVOICE_SHIELD_UPLOAD_DIR` | Where originals are kept (default `invoiceshield_uploads/`) |
| `INVOICE_SHIELD_MAX_FILE_MB` | Upload limit (default 25) |

Generate a key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## 11. Scope & Limitations

InvoiceShield AI is a **portfolio prototype** built to demonstrate OCR-based document processing and finance controls. It is not a production accounts-payable system. Stating the boundaries plainly is part of the design philosophy.

* **Rule-based extraction.** Parsing is regex- and heuristic-driven and tuned to common invoice layouts. Unusual layouts return *Partial* or *Insufficient Evidence*, which the UI is designed to show clearly.
* **English-only OCR.** Accuracy depends on scan quality, so scanned amounts should always be checked against the source.
* **No authentication or user management.** It is a single-workspace, single-user app intended for local use. If hosted for others, place it behind your own auth (reverse proxy / SSO).
* **SQLite, single workspace.** The schema is tenant-scoped, but there is no tenant-switching UI.
* **Not a guarantee.** It is neither a fraud-detection system nor a tax-compliance guarantee.

### Natural next steps
* Layout-aware or ML-based field extraction to widen supported invoice formats
* Authentication, roles and multi-tenant switching
* REST API on top of `invoice_core.py`
* Multi-language OCR

---

<div align="center">

**Built to answer one question well: *"Can this invoice be trusted, and can we prove how we decided?"***

</div>
