"""Document ingestion service — ingest PDFs, text files, and URLs into semantic memory."""

import logging
import os
from pathlib import Path

import httpx
import trafilatura

from app.memory.semantic import store_semantic

logger = logging.getLogger("pai.services.doc_ingest")

# Where uploaded files are stored — container uses /app/uploads, local uses ./uploads
UPLOAD_DIR = Path(os.environ.get("PAI_UPLOAD_DIR", "/app/uploads"))
try:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
except PermissionError:
    UPLOAD_DIR = Path("./uploads")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Max chunk size for embedding (tokens ≈ words × 1.3, keep under model context)
CHUNK_SIZE = 800  # words per chunk
CHUNK_OVERLAP = 100  # overlap in words


def _chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping word-level chunks."""
    words = text.split()
    if len(words) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(words):
        end = start + chunk_size
        chunk = " ".join(words[start:end])
        chunks.append(chunk)
        start += chunk_size - overlap
    return chunks


async def ingest_url(url: str, http_client: httpx.AsyncClient | None = None) -> dict:
    """Fetch a URL, extract text, chunk it, and store each chunk in semantic memory."""
    try:
        if http_client:
            resp = await http_client.get(url, follow_redirects=True, timeout=20.0)
            html = resp.text
        else:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(url, follow_redirects=True)
                html = resp.text

        text = trafilatura.extract(html, include_comments=False, include_tables=True) or ""
        if not text.strip():
            return {"url": url, "chunks": 0, "error": "No extractable text found"}
    except Exception as e:
        return {"url": url, "chunks": 0, "error": str(e)}

    return await _store_chunks(text, source=url, doc_type="url", http_client=http_client)


async def ingest_text(
    text: str,
    title: str = "",
    source: str = "",
    doc_type: str = "text",
    http_client: httpx.AsyncClient | None = None,
) -> dict:
    """Chunk raw text and store in semantic memory."""
    return await _store_chunks(
        text, source=source or title, doc_type=doc_type, title=title, http_client=http_client
    )


async def ingest_file(
    file_bytes: bytes,
    filename: str,
    http_client: httpx.AsyncClient | None = None,
) -> dict:
    """Save an uploaded file, extract text, and ingest into semantic memory."""
    # Save to disk
    safe_name = filename.replace("/", "_").replace("\\", "_")
    file_path = UPLOAD_DIR / safe_name
    file_path.write_bytes(file_bytes)

    ext = file_path.suffix.lower()
    text = ""

    if ext == ".pdf":
        text = _extract_pdf(file_path)
    elif ext in (".txt", ".md", ".csv"):
        text = file_bytes.decode("utf-8", errors="replace")
    elif ext in (".html", ".htm"):
        text = trafilatura.extract(file_bytes.decode("utf-8", errors="replace")) or ""
    else:
        return {"filename": filename, "chunks": 0, "error": f"Unsupported file type: {ext}"}

    if not text.strip():
        return {"filename": filename, "chunks": 0, "error": "No extractable text found"}

    result = await _store_chunks(
        text, source=f"file:{filename}", doc_type="file", title=filename, http_client=http_client
    )
    result["filename"] = filename
    result["file_path"] = str(file_path)
    return result


def _text_quality_score(text: str) -> float:
    """Estimate text quality. Returns 0.0-1.0 based on readable word ratio."""
    import re
    words = text.split()
    if not words:
        return 0.0
    # Count words that look like real English (letters, common punctuation)
    readable = sum(1 for w in words if re.match(r'^[A-Za-z\'\-,.;:!?()]+$', w.strip('.,;:!?()"')))
    return readable / len(words)


def _extract_pdf(path: Path) -> str:
    """Extract text from a PDF file using pdfplumber, with OCR fallback for scanned docs."""
    pdfplumber_text = ""

    # Try pdfplumber first (fast, works for text-layer PDFs)
    try:
        import pdfplumber
        pages = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    pages.append(page_text)
        if pages:
            pdfplumber_text = "\n\n".join(pages)
            quality = _text_quality_score(pdfplumber_text)
            logger.info(
                "pdf_text_layer pages=%d quality=%.2f path=%s",
                len(pages), quality, path.name,
            )
            # Only skip OCR if text layer quality is very high (native digital PDF)
            if quality >= 0.85:
                return pdfplumber_text
            # Otherwise fall through to OCR and compare
            logger.info("pdf_text_layer_moderate quality=%.2f, will compare with OCR path=%s", quality, path.name)
    except ImportError:
        logger.warning("pdfplumber not installed — trying OCR directly")
    except Exception as e:
        logger.error("pdf_pdfplumber_failed: %s", e)

    # Fallback: OCR for scanned/image PDFs or low-quality text layers
    try:
        from pdf2image import convert_from_path
        import pytesseract

        logger.info("pdf_ocr_starting path=%s", path.name)
        images = convert_from_path(str(path), dpi=300)
        pages = []
        for i, img in enumerate(images):
            text = pytesseract.image_to_string(img)
            if text and text.strip():
                pages.append(text.strip())
        if pages:
            ocr_text = "\n\n".join(pages)
            ocr_quality = _text_quality_score(ocr_text)
            logger.info(
                "pdf_ocr_extracted pages=%d quality=%.2f path=%s",
                len(pages), ocr_quality, path.name,
            )
            # If we also have pdfplumber text, pick the better one
            if pdfplumber_text:
                plumber_quality = _text_quality_score(pdfplumber_text)
                if plumber_quality >= ocr_quality:
                    logger.info("pdf_using_text_layer (%.2f >= %.2f) path=%s", plumber_quality, ocr_quality, path.name)
                    return pdfplumber_text
                logger.info("pdf_using_ocr (%.2f > %.2f) path=%s", ocr_quality, plumber_quality, path.name)
            return ocr_text
        logger.warning("pdf_ocr_no_text path=%s", path.name)
    except ImportError:
        logger.warning("pytesseract/pdf2image not installed — OCR unavailable")
    except Exception as e:
        logger.error("pdf_ocr_failed: %s", e)

    # Last resort: return whatever pdfplumber got, even if low quality
    if pdfplumber_text:
        logger.info("pdf_fallback_to_text_layer path=%s", path.name)
        return pdfplumber_text
    return ""


async def _store_chunks(
    text: str,
    source: str = "",
    doc_type: str = "text",
    title: str = "",
    http_client: httpx.AsyncClient | None = None,
) -> dict:
    """Chunk text and store each chunk in semantic memory."""
    chunks = _chunk_text(text)
    stored = 0
    for i, chunk in enumerate(chunks):
        prefix = f"[{title}] " if title else ""
        content = f"{prefix}(Part {i+1}/{len(chunks)})\n{chunk}"
        row_id = await store_semantic(
            content=content,
            source=source,
            metadata={
                "type": "ingested_document",
                "doc_type": doc_type,
                "title": title,
                "chunk": i + 1,
                "total_chunks": len(chunks),
            },
            http_client=http_client,
        )
        if row_id > 0:
            stored += 1

    logger.info("document_ingested", extra={
        "source": source, "chunks": len(chunks), "stored": stored, "doc_type": doc_type,
    })
    return {
        "source": source,
        "title": title,
        "doc_type": doc_type,
        "total_chars": len(text),
        "chunks": len(chunks),
        "stored": stored,
    }
