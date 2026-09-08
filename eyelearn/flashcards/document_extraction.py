"""Extracts plain text from an uploaded source document, for AI flashcard
generation (see flashcards/services.py's SourceDocumentService). Owns
pypdf/pypdfium2 the same way storage.py owns boto3 and each ai_providers/*.py
owns its own SDK -- this is the only file that touches PDF-parsing libraries.

PDF, TXT, and Markdown are handled directly. A PDF page with no (or almost
no) embedded text -- a scanned page -- and any standalone image upload (a
photo of a book page or handwritten notes) are both routed through
ai_providers.describe_image_with_fallback for genuine visual understanding,
not narrow character recognition (see that module's _VISION_PROMPT).

Rendering a PDF page to an image uses pypdfium2 (binds Google's PDFium, the
engine Chrome itself uses) rather than the more commonly recommended
PyMuPDF: PyMuPDF is AGPL-3.0 (or a paid Artifex commercial license), a real
licensing risk for a closed-source product, whereas pypdfium2 is
permissively licensed (Apache-2.0/BSD) and ships prebuilt wheels. Likewise,
pdf2image (which shells out to the Poppler system binary) is ruled out
entirely: this backend runs as a Vercel serverless Python function with no
system package installs (see eyelearn/CLAUDE.md).
"""

import io

import pypdf

from . import ai_providers
from .ai_providers.base import AiGenerationError

ALLOWED_CONTENT_TYPES = {
    'application/pdf', 'text/plain', 'text/markdown', 'text/x-markdown',
    'image/jpeg', 'image/png', 'image/webp',
}
MAX_SIZE_BYTES = {
    'application/pdf': 20 * 1024 * 1024,
    'text/plain': 5 * 1024 * 1024,
    'text/markdown': 5 * 1024 * 1024,
    'text/x-markdown': 5 * 1024 * 1024,
    'image/jpeg': 10 * 1024 * 1024,
    'image/png': 10 * 1024 * 1024,
    'image/webp': 10 * 1024 * 1024,
}
# A page yielding less embedded text than this is treated as "needs OCR" --
# real chars, not just page furniture (a page number, a running header).
MIN_TEXT_LENGTH_TO_SKIP_OCR = 20
# Bounds cost/latency on a heavily-scanned PDF: each OCR'd page is one vision
# API call. Pages beyond this cap are silently skipped (see _extract_pdf),
# not an error -- a partial extraction is still useful.
MAX_OCR_PAGES_PER_DOCUMENT = 20


class DocumentExtractionError(Exception):
    """Corrupt/encrypted file, undecodable text, or a vision-OCR failure the user should see."""


def extract_text(*, content_type, data: bytes) -> str:
    if content_type == 'application/pdf':
        return _extract_pdf(data)
    if content_type in ('text/plain', 'text/markdown', 'text/x-markdown'):
        return _extract_plain_text(data)
    if content_type in ('image/jpeg', 'image/png', 'image/webp'):
        return _extract_image(content_type, data)
    raise DocumentExtractionError(f'{content_type!r} is not a supported document type.')


def _extract_pdf(data):
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
    except Exception:
        raise DocumentExtractionError('This PDF could not be read. It may be corrupted.')
    if reader.is_encrypted:
        raise DocumentExtractionError('Password-protected PDFs are not supported.')

    ocr_budget = MAX_OCR_PAGES_PER_DOCUMENT
    pages = []
    for index, page in enumerate(reader.pages):
        try:
            text = (page.extract_text() or '').strip()
        except Exception:
            text = ''
        if len(text) < MIN_TEXT_LENGTH_TO_SKIP_OCR:
            if ocr_budget <= 0:
                # Cap hit -- skip remaining scanned pages rather than balloon
                # cost/latency further; whatever text was already collected
                # is still returned below.
                continue
            image_bytes = _render_pdf_page_to_png(data, index)
            try:
                text = ai_providers.describe_image_with_fallback(image_bytes=image_bytes, media_type='image/png')
            except AiGenerationError as exc:
                raise DocumentExtractionError(f'Visual analysis of a scanned page failed: {exc}')
            ocr_budget -= 1
        pages.append(text)

    text = '\n\n'.join(page for page in pages if page).strip()
    if not text:
        raise DocumentExtractionError(
            'No extractable text was found in this PDF, and visual analysis of its pages did not find any either.',
        )
    return text


def _render_pdf_page_to_png(data, page_index):
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(data)
    try:
        bitmap = doc[page_index].render(scale=2.0)  # ~144 DPI, enough for handwriting/small print
        pil_image = bitmap.to_pil()
        buffer = io.BytesIO()
        pil_image.save(buffer, format='PNG')
        return buffer.getvalue()
    finally:
        doc.close()


def _extract_plain_text(data):
    for encoding in ('utf-8', 'latin-1'):
        try:
            text = data.decode(encoding).strip()
            break
        except UnicodeDecodeError:
            continue
    else:
        raise DocumentExtractionError('This file could not be decoded as text.')
    if not text:
        raise DocumentExtractionError('This file is empty.')
    return text


def _extract_image(content_type, data):
    try:
        text = ai_providers.describe_image_with_fallback(image_bytes=data, media_type=content_type).strip()
    except AiGenerationError as exc:
        raise DocumentExtractionError(f'Visual analysis of this image failed: {exc}')
    if not text:
        raise DocumentExtractionError('Visual analysis of this image did not find any readable content.')
    return text
