"""Bounded, local-only attachment storage and document extraction."""
from __future__ import annotations

import hashlib
import mimetypes
import os
from pathlib import Path
import uuid
import zipfile
from xml.etree import ElementTree

MAX_BYTES = 25 * 1024 * 1024
MAX_CHARS = 32_000
TEXT = set('.txt .md .markdown .json .csv .tsv .py .js .mjs .ts .tsx .jsx .html .css .xml .yaml .yml .toml .ini .cfg .sh .bash .c .h .cpp .hpp .rs .go .java .sql .log .rb .swift .kt .lua .r'.split())
IMAGES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif'}


class AttachmentError(ValueError):
    """A safe, single-sentence error for the client."""


def extract(path: Path) -> tuple[str, int | None]:
    """Extract locally; optional parsers never become required dependencies."""
    ext = path.suffix.lower()
    try:
        if ext in IMAGES:
            return '', None
        if ext in TEXT:
            return path.read_text(encoding='utf-8'), None
        if ext == '.pdf':
            from pypdf import PdfReader
            reader = PdfReader(path)
            if reader.is_encrypted:
                raise AttachmentError('Encrypted PDFs cannot be read; attach an unlocked copy.')
            if len(reader.pages) > 500:
                raise AttachmentError('PDFs are limited to 500 pages.')
            return '\n'.join(f'[page {i}]\n{page.extract_text() or ""}' for i, page in enumerate(reader.pages, 1)), len(reader.pages)
        if ext == '.docx':
            with zipfile.ZipFile(path) as archive:
                info = archive.getinfo('word/document.xml')
                if info.file_size > MAX_BYTES:
                    raise AttachmentError('The expanded document is too large to read.')
                root = ElementTree.fromstring(archive.read(info))
                return '\n'.join(''.join(p.itertext()) for p in root.iter() if p.tag.endswith('}p')), None
        if ext in {'.xlsx', '.pptx'}:
            with zipfile.ZipFile(path) as archive:
                if sum(info.file_size for info in archive.infolist()) > MAX_BYTES:
                    raise AttachmentError('The expanded document is too large to read.')
        if ext == '.xlsx':
            from openpyxl import load_workbook
            book = load_workbook(path, read_only=True, data_only=True)
            try:
                chunks, size = [], 0
                for sheet in book:
                    chunks.append(f'[sheet {sheet.title}]')
                    for row in sheet.values:
                        line = '\t'.join(str(v) if v is not None else '' for v in row)
                        size += len(line)
                        if size > MAX_BYTES:
                            raise AttachmentError('The expanded spreadsheet is too large to read.')
                        chunks.append(line)
                return '\n'.join(chunks), None
            finally:
                book.close()
        if ext == '.pptx':
            from pptx import Presentation
            deck = Presentation(path)
            return '\n'.join(f'[slide {i}]\n' + '\n'.join(s.text for s in slide.shapes if s.has_text_frame) for i, slide in enumerate(deck.slides, 1)), len(deck.slides)
        raise AttachmentError('Unsupported file format; attach text, PDF, DOCX, XLSX, PPTX or an image.')
    except ImportError:
        raise AttachmentError('This format needs an optional reader; install kova-ai[attachments] locally.') from None
    except AttachmentError:
        raise
    except Exception:
        raise AttachmentError('This file could not be read; attach a valid, unlocked copy.') from None


def read_text(path: Path, start: int = 1, end: int | None = None) -> str:
    text, _ = extract(path)
    lines = text.splitlines()
    start = max(1, start)
    end = min(len(lines), end if end is not None else len(lines))
    result = '\n'.join(f'{i}: {line}' for i, line in enumerate(lines[start - 1:end], start))
    return (f'lines {start}-{end} of {len(lines)}\n' + result)[:MAX_CHARS]


def save(home: Path, store, chat_id: str, name: str, data: bytes) -> dict:
    if len(data) > MAX_BYTES:
        raise AttachmentError('Each attachment must be 25 MB or smaller.')
    name = Path(name.replace('\\', '/')).name
    ext = Path(name).suffix.lower()
    ident = uuid.uuid4().hex
    folder = home / 'attachments' / chat_id
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / (ident + ext)
    try:
        with path.open('xb') as stream:
            os.chmod(path, 0o600)
            stream.write(data)
        _, pages = extract(path)
        row = dict(id=ident, chat_id=chat_id, name=name, mime=IMAGES.get(ext, mimetypes.guess_type(name)[0] or 'text/plain'), size=len(data), pages=pages, sha256=hashlib.sha256(data).hexdigest(), path=str(path))
        store.add_attachment(row)
        return {k: v for k, v in row.items() if k != 'path'}
    except Exception:
        path.unlink(missing_ok=True)
        raise


def block(row: dict) -> str:
    pages = f'{row["pages"]} pages · ' if row['pages'] is not None else ''
    return f'file: {row["name"]} · {pages}{row["size"] / 1024:.1f} KB\nattachment_id: {row["id"]}\n' + read_text(Path(row['path']))
