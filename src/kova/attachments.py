"""Bounded, local-only attachment storage and document extraction."""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
from functools import lru_cache
from pathlib import Path
import uuid
import zipfile
from xml.etree import ElementTree

from starlette.formparsers import MultiPartException

MAX_BYTES = 25 * 1024 * 1024
# Three 25 MiB files plus 1 MiB of multipart/form overhead. Enforced before parsing.
MAX_REQUEST_BYTES = 3 * MAX_BYTES + 1024 * 1024
MAX_CHARS = 32_000
TEXT = set('.txt .md .markdown .json .csv .tsv .py .js .mjs .ts .tsx .jsx .html .css .xml .yaml .yml .toml .ini .cfg .sh .bash .c .h .cpp .hpp .rs .go .java .sql .log .rb .swift .kt .lua .r'.split())
IMAGES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif'}


class AttachmentError(ValueError):
    """A safe, single-sentence error for the client."""


class RequestTooLarge(MultiPartException):
    pass


class AttachmentBodyLimit:
    """Bound bytes before Starlette can spool multipart data to disk."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        oversized = False
        for key, value in scope.get('headers', []):
            if key.lower() == b'content-length':
                try:
                    oversized |= int(value) > MAX_REQUEST_BYTES
                except ValueError:
                    pass
        async def reject():
            from starlette.responses import JSONResponse
            await JSONResponse({'detail': 'Request body is too large.'}, status_code=413)(scope, receive, send)
        if oversized:
            return await reject()
        size = 0
        async def bounded_receive():
            nonlocal size, oversized
            message = await receive()
            size += len(message.get('body', b''))
            if size > MAX_REQUEST_BYTES:
                oversized = True
                raise RequestTooLarge("Request body is too large.")
            return message
        async def bounded_send(message):
            # FastAPI may turn a receive error into a multipart 400. Replace it
            # with 413 as well, without leaking a traceback or sending twice.
            if not oversized:
                await send(message)
        try:
            await self.app(scope, bounded_receive, bounded_send)
        except RequestTooLarge:
            oversized = True
        if oversized:
            await reject()


def bounded_join(chunks):
    result, remaining = [], MAX_CHARS
    for chunk in chunks:
        if result:
            result.append('\n')
            remaining -= 1
        result.append(chunk[:remaining])
        remaining -= len(result[-1])
        if remaining <= 0:
            break
    return ''.join(result)


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
            return bounded_join(f'[page {i}]\n{page.extract_text() or ""}' for i, page in enumerate(reader.pages, 1)), len(reader.pages)
        if ext == '.docx':
            with zipfile.ZipFile(path) as archive:
                info = archive.getinfo('word/document.xml')
                if info.file_size > MAX_BYTES:
                    raise AttachmentError('The expanded document is too large to read.')
                xml = archive.read(info)
                declarations = xml.replace(b'\x00', b'').upper()
                if b'<!DOCTYPE' in declarations or b'<!ENTITY' in declarations:
                    raise AttachmentError('XML declarations are not allowed in DOCX attachments.')
                root = ElementTree.fromstring(xml)
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
            def chunks():
                for i, slide in enumerate(deck.slides, 1):
                    yield f'[slide {i}]'
                    for shape in slide.shapes:
                        if shape.has_text_frame:
                            yield shape.text
            return bounded_join(chunks()), len(deck.slides)
        raise AttachmentError('Unsupported file format; attach text, PDF, DOCX, XLSX, PPTX or an image.')
    except ImportError:
        raise AttachmentError('This format needs an optional reader; install kova-ai[attachments] locally.') from None
    except AttachmentError:
        raise
    except Exception:
        raise AttachmentError('This file could not be read; attach a valid, unlocked copy.') from None


@lru_cache(maxsize=128)
def cached_text(ident: str, sha256: str, path: str) -> str:
    return extract(Path(path))[0][:MAX_CHARS]


def read_text(path: Path, start: int = 1, end: int | None = None, *, row: dict | None = None) -> str:
    text = cached_text(row['id'], row['sha256'], str(path)) if row else extract(path)[0]
    lines = text.splitlines()
    start = max(1, start)
    end = min(len(lines), end if end is not None else len(lines))
    result = '\n'.join(f'{i}: {line}' for i, line in enumerate(lines[start - 1:end], start))
    return (f'lines {start}-{end} of {len(lines)}\n' + result)[:MAX_CHARS]


def save(home: Path, store, chat_id: str, name: str, data: bytes) -> dict:
    if len(data) > MAX_BYTES:
        raise AttachmentError('Each attachment must be 25 MB or smaller.')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', chat_id):
        raise AttachmentError('Invalid chat ID.')
    if '\x00' in name or len(Path(name).suffix) > 32:
        raise AttachmentError('Invalid attachment filename or extension.')
    name = Path(name.replace('\\', '/')).name
    ext = Path(name).suffix.lower()
    ident = uuid.uuid4().hex
    folder = home / 'attachments' / chat_id
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / (ident + ext)
    try:
        with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as stream:
            stream.write(data)
        _, pages = extract(path)
        row = dict(id=ident, chat_id=chat_id, name=name, mime=IMAGES.get(ext, mimetypes.guess_type(name)[0] or 'text/plain'), size=len(data), pages=pages, sha256=hashlib.sha256(data).hexdigest(), path=str(path))
        store.add_attachment(row)
        return {k: v for k, v in row.items() if k != 'path'}
    except (OSError, ValueError) as exc:
        path.unlink(missing_ok=True)
        if isinstance(exc, AttachmentError):
            raise
        raise AttachmentError('Invalid attachment filename or storage error.') from None
    except Exception:
        path.unlink(missing_ok=True)
        raise


def block(row: dict) -> str:
    pages = f'{row["pages"]} pages · ' if row['pages'] is not None else ''
    return f'file: {row["name"]} · {pages}{row["size"] / 1024:.1f} KB\nattachment_id: {row["id"]}\n' + read_text(Path(row['path']), row=row)


def index(rows) -> str:
    """A model-only list of a chat's attachments. Never includes extracted text."""
    return '\n'.join(f'- {row["name"]} (id {row["id"]})' for row in rows)
