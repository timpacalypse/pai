"""Receipt tracking service — upload, OCR, extract details, query for taxes."""

import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import text

from app.core.database import async_session
from app.services.ollama_service import generate

logger = logging.getLogger("pai.receipts")

UPLOAD_DIR = Path(os.environ.get("PAI_UPLOAD_DIR", "/app/uploads")) / "receipts"
try:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
except PermissionError:
    UPLOAD_DIR = Path("./uploads/receipts")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

TAX_CATEGORIES = [
    "business_expense", "office_supplies", "software_subscriptions",
    "travel", "meals_entertainment", "professional_development",
    "equipment", "home_office", "medical", "charitable",
    "vehicle", "insurance", "utilities", "other",
]


async def ingest_receipt(file_bytes: bytes, filename: str) -> dict:
    """Process an uploaded receipt: save, OCR, extract details, store."""
    # Save file
    safe_name = filename.replace("/", "_").replace("\\", "_")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    stored_name = f"{timestamp}_{safe_name}"
    file_path = UPLOAD_DIR / stored_name
    file_path.write_bytes(file_bytes)

    # Extract text via OCR or text extraction
    ext = file_path.suffix.lower()
    raw_text = ""

    if ext == ".pdf":
        raw_text = _extract_pdf(file_path)
    elif ext in (".jpg", ".jpeg", ".png", ".webp", ".heic"):
        raw_text = _extract_image(file_path)
    elif ext in (".txt", ".csv"):
        raw_text = file_bytes.decode("utf-8", errors="replace")
    else:
        return {"error": f"Unsupported file type: {ext}", "filename": filename}

    if not raw_text.strip():
        return {"error": "Could not extract text from receipt", "filename": filename}

    # Use LLM to extract structured receipt data
    details = await _extract_receipt_details(raw_text)

    # Determine tax year
    receipt_date = details.get("date")
    tax_year = receipt_date.year if receipt_date else date.today().year

    # Store in DB
    async with async_session() as session:
        r = await session.execute(text("""
            INSERT INTO receipts
                (vendor, amount, tax_amount, receipt_date, category, description,
                 payment_method, tax_year, file_path, raw_text, metadata)
            VALUES
                (:vendor, :amount, :tax_amount, :receipt_date, :category, :description,
                 :payment_method, :tax_year, :file_path, :raw_text, CAST(:metadata AS jsonb))
            RETURNING id
        """), {
            "vendor": details.get("vendor", ""),
            "amount": details.get("amount"),
            "tax_amount": details.get("tax_amount"),
            "receipt_date": receipt_date,
            "category": details.get("category", "other"),
            "description": details.get("description", ""),
            "payment_method": details.get("payment_method", ""),
            "tax_year": tax_year,
            "file_path": str(file_path),
            "raw_text": raw_text[:5000],
            "metadata": "{}",
        })
        await session.commit()
        receipt_id = r.scalar()

    return {
        "id": receipt_id,
        "vendor": details.get("vendor", "Unknown"),
        "amount": details.get("amount"),
        "category": details.get("category", "other"),
        "date": str(receipt_date) if receipt_date else "Unknown",
        "tax_year": tax_year,
        "filename": filename,
    }


async def _extract_receipt_details(raw_text: str) -> dict:
    """Use LLM to extract structured data from receipt text."""
    import json

    prompt = f"""Extract the following from this receipt text. Return ONLY valid JSON, no other text.

RECEIPT TEXT:
{raw_text[:3000]}

Return this exact JSON structure (use null for unknown values):
{{
  "vendor": "store/company name",
  "amount": 0.00,
  "tax_amount": 0.00,
  "date": "YYYY-MM-DD",
  "category": "one of: business_expense, office_supplies, software_subscriptions, travel, meals_entertainment, professional_development, equipment, home_office, medical, charitable, vehicle, insurance, utilities, other",
  "description": "brief description of purchase",
  "payment_method": "credit/debit/cash/etc"
}}"""

    result = await generate(
        prompt=prompt,
        system_prompt="You extract structured data from receipts. Return ONLY valid JSON. No markdown, no explanation.",
        model="qwen3:4b",
    )

    # Parse LLM response
    try:
        # Strip markdown fences if present
        cleaned = result.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
        cleaned = cleaned.strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()

        data = json.loads(cleaned)

        # Parse date
        if data.get("date") and data["date"] != "null":
            try:
                data["date"] = date.fromisoformat(data["date"])
            except (ValueError, TypeError):
                data["date"] = None
        else:
            data["date"] = None

        # Validate category
        if data.get("category") not in TAX_CATEGORIES:
            data["category"] = "other"

        # Ensure amount is numeric
        if data.get("amount"):
            try:
                data["amount"] = float(data["amount"])
            except (ValueError, TypeError):
                data["amount"] = None

        if data.get("tax_amount"):
            try:
                data["tax_amount"] = float(data["tax_amount"])
            except (ValueError, TypeError):
                data["tax_amount"] = None

        return data
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning("receipt_parse_failed", extra={"error": str(e), "response": result[:200]})
        return {"vendor": "", "amount": None, "tax_amount": None, "date": None,
                "category": "other", "description": "", "payment_method": ""}


# ── Query functions ──────────────────────────────────────────────────────────

async def get_receipts(
    tax_year: int | None = None,
    category: str | None = None,
    vendor: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query receipts with optional filters."""
    conditions = []
    params = {"lim": limit}

    if tax_year:
        conditions.append("tax_year = :year")
        params["year"] = tax_year
    if category:
        conditions.append("category = :cat")
        params["cat"] = category
    if vendor:
        conditions.append("LOWER(vendor) LIKE '%' || LOWER(:vendor) || '%'")
        params["vendor"] = vendor

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    async with async_session() as session:
        r = await session.execute(text(f"""
            SELECT id, vendor, amount, tax_amount, receipt_date, category,
                   description, payment_method, tax_year, created_at
            FROM receipts {where}
            ORDER BY receipt_date DESC LIMIT :lim
        """), params)
        return [dict(row) for row in r.mappings().fetchall()]


async def get_tax_summary(tax_year: int | None = None) -> dict:
    """Get summary of receipts by category for a tax year."""
    if not tax_year:
        tax_year = date.today().year

    async with async_session() as session:
        r = await session.execute(text("""
            SELECT category,
                   COUNT(*) as count,
                   SUM(amount) as total,
                   SUM(tax_amount) as total_tax
            FROM receipts
            WHERE tax_year = :year AND amount IS NOT NULL
            GROUP BY category
            ORDER BY total DESC
        """), {"year": tax_year})
        categories = [dict(row) for row in r.mappings().fetchall()]

        r = await session.execute(text("""
            SELECT COUNT(*) as total_receipts,
                   SUM(amount) as grand_total
            FROM receipts WHERE tax_year = :year
        """), {"year": tax_year})
        totals = dict(r.mappings().fetchone())

    return {
        "tax_year": tax_year,
        "total_receipts": totals.get("total_receipts", 0),
        "grand_total": float(totals["grand_total"]) if totals.get("grand_total") else 0,
        "by_category": categories,
    }


async def delete_receipt(receipt_id: int) -> bool:
    """Delete a receipt by ID."""
    async with async_session() as session:
        r = await session.execute(text(
            "DELETE FROM receipts WHERE id = :id"
        ), {"id": receipt_id})
        await session.commit()
        return r.rowcount > 0


# ── OCR helpers ──────────────────────────────────────────────────────────────

def _extract_image(path: Path) -> str:
    """OCR an image file."""
    try:
        import pytesseract
        from PIL import Image
        img = Image.open(path)
        return pytesseract.image_to_string(img)
    except ImportError:
        logger.error("pytesseract or Pillow not installed")
        return ""
    except Exception as e:
        logger.error("image_ocr_failed: %s", e)
        return ""


def _extract_pdf(path: Path) -> str:
    """Extract text from PDF (text layer or OCR)."""
    try:
        import pdfplumber
        pages = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    pages.append(t)
        if pages:
            return "\n\n".join(pages)
    except Exception as e:
        logger.warning("pdf_text_failed: %s", e)

    # OCR fallback
    try:
        from pdf2image import convert_from_path
        import pytesseract
        images = convert_from_path(str(path), dpi=300)
        pages = []
        for img in images:
            t = pytesseract.image_to_string(img)
            if t and t.strip():
                pages.append(t.strip())
        return "\n\n".join(pages)
    except Exception as e:
        logger.error("pdf_ocr_failed: %s", e)
        return ""
