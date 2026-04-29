# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Single-file Python service (`pdf_hybrid_extractor.py`) that extracts text from PDFs and DOCX. PDFs use a hybrid strategy: native text extraction via PyMuPDF, falling back to Gemini Vision for image-heavy/scanned pages. DOCX goes through `mammoth.extract_raw_text` (no Vision). Runs as Flask app (gunicorn in prod) or CLI. Deployed on Dokploy at port 5050.

## Commands

```bash
# Install
pip install -r requirements.txt

# CLI: extract from URL or local path (PDF only — DOCX dispatch lives in the Flask route)
python3 pdf_hybrid_extractor.py <pdf_url_or_path>

# Dev server
PDF_EXTRACTOR_TOKEN=dev python3 pdf_hybrid_extractor.py --serve --port 5050

# Production (matches Dockerfile)
gunicorn --bind 0.0.0.0:5050 --workers 1 --threads 4 --worker-class gthread --timeout 480 'pdf_hybrid_extractor:create_app()'

# Docker
docker build -t pdf-hybrid-extractor . && docker run -p 5050:5050 -e GEMINI_API_KEY=... -e PDF_EXTRACTOR_TOKEN=... pdf-hybrid-extractor
```

No test suite, linter, or formatter is configured.

## Required env vars

- `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) — Gemini Vision
- `PDF_EXTRACTOR_TOKEN` — bearer token for `/extract`. **Required**; `create_app()` raises if missing. No default — service won't start without it.
- Optional Minio: `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, `MINIO_SECURE`, `MINIO_BUCKET`
- Optional tuning: `VISION_MODEL` (default `gemini-flash-latest`), `VISION_MODEL_FALLBACK` (default `gemini-2.5-flash`), `MAX_DOWNLOAD_BYTES` (default 50 MB), `RATE_LIMIT_DEFAULT` (default `60 per minute`), `RATE_LIMIT_EXTRACT` (default `30 per minute`)

## Architecture

Pipeline in `process_pdf()`:

1. **Open document** — `fitz.open(stream=pdf_bytes, filetype="pdf")` (kept open for the whole pipeline so rendering can be lazy).
2. **Pass 1: text extraction** — for each page, `page.get_text()`. If `len(text) < MIN_TEXT_THRESHOLD` (50 chars), the page is marked for Vision. **No rendering happens here** — only metadata is collected.
3. **Pass 2: Vision (parallel + lazy render)** — pages flagged for Vision are capped at `MAX_VISION_PAGES`; the cap'd subset is sent through a `ThreadPoolExecutor(max_workers=VISION_PARALLEL)`. Each worker renders its page (2x zoom PNG) only when picked up, calls `analyze_image_with_vision`, and the bytes go out of scope on return. Pages over the cap stay with their native text and are tagged "ignorada".
4. **Failure handling** — `analyze_image_with_vision` returns `None` on exception OR empty/blocked response (safety filter). Failed pages fall back to native text and are listed in `failed_pages` in the response.
5. **Combine** — page outputs joined with `--- Página N ---`, `--- Página N (Vision AI) ---`, `--- Página N (Vision AI falhou ...) ---`, or `--- Página N (Vision AI ignorada ...) ---` markers.

Vision model: `gemini-2.5-flash` (set at `VISION_MODEL`). When changing models, sanity-check the SDK still accepts `types.Part.from_bytes(..., mime_type="image/png")` and that `response.text` shape is unchanged — newer models sometimes shift candidate handling.

DOCX path (Flask route only, not `process_pdf`): `_detect_type()` peeks magic bytes (`%PDF` vs `PK\x03\x04`); DOCX bytes go straight to `extract_text_docx` (mammoth). Caller can override with `type: "pdf"|"docx"` in the body.

Optional: original PDF persisted to Minio at `<bucket>/<telefone>/<timestamp>.pdf` when `save_to_minio=True`. DOCX uploads to Minio not yet implemented.

### Key constants (top of file)

`MIN_TEXT_THRESHOLD`, `MAX_VISION_PAGES` (15), `GEMINI_TIMEOUT` (60s, passed as ms to the SDK's `http_options`), `VISION_PARALLEL` (3), `VISION_MODEL`, `VISION_PROMPT`, `TELEFONE_RE`. Tune these before adding new knobs.

### Gunicorn config rationale

`workers=1 threads=4 gthread timeout=480`. Single worker keeps memory bounded; gthread serves multiple HTTP requests concurrently. Each request *also* spins up its own `ThreadPoolExecutor(max_workers=VISION_PARALLEL=3)` for parallel Gemini calls — those are independent of gunicorn's thread pool. Worst-case Vision time per request: `ceil(MAX_VISION_PAGES / VISION_PARALLEL) × GEMINI_TIMEOUT = ceil(15/3) × 60 = 300s`; the 480s timeout buffers render + I/O overhead. Don't blindly raise `workers` without checking memory.

### Security guards (already in place)

- `_assert_safe_url` blocks non-http(s) schemes and any private/loopback/link-local/multicast/reserved IP — anti-SSRF; called from `download_file`.
- `download_file` streams with a hard cap (`MAX_DOWNLOAD_BYTES`, default 50 MB) — pre-checks `Content-Length` then enforces during chunked read; warns on Content-Type outside `ALLOWED_DOWNLOAD_TYPES`.
- `/extract` validates `url` starts with `http(s)://` *and* downloads bytes itself, so `process_pdf` never receives an attacker-controlled string (closes path traversal).
- Token compared with `hmac.compare_digest`; Bearer parsed with slice (not `replace`).
- `telefone` validated against `TELEFONE_RE = ^\d{8,20}$` before reaching Minio.
- `save_to_minio=true` without `telefone` is now rejected (was a silent no-op).
- `create_app()` raises if `PDF_EXTRACTOR_TOKEN` is unset.
- `flask-limiter` rate-limits per IP (`ProxyFix` reads `X-Forwarded-For` for real client IP behind Traefik/Dokploy). `/health` is exempt. **Note**: storage is in-memory — fine for the single-worker setup; needs Redis if you ever scale to >1 worker/replica.
- 500 responses are generic ("internal error"); details go to logger only — no stack/path leak to clients. 400 responses (`ValueError`) keep their messages since those are user-actionable.
- Container runs as non-root user `appuser` (uid 1000).
- `HEALTHCHECK` in Dockerfile pings `/health` every 30s; orchestrator can detect deadlocked workers.
- Minio object name uses UTC timestamp + UUID4 hex suffix (`{telefone}/YYYYMMDDTHHMMSSZ_xxxxxxxx.pdf`) — no 1-second collision; consistent across hosts.

### SDK note

Uses the new `google-genai` SDK (`from google import genai`), not the deprecated `google-generativeai`. Image inputs use `types.Part.from_bytes(...)`. When touching Gemini code, verify against current `google-genai` docs (via context7) — the migration is recent (commit `a4bce16`).

## API

`POST /extract` (Bearer auth). Body:

- `url`: must start with `http://` or `https://` (private IPs blocked)
- `base64`: alternative to url
- `type` (optional): `"pdf"` or `"docx"`; auto-detected via magic bytes if omitted
- `telefone` (optional): digits only, 8-20
- `save_to_minio` (optional): bool

Response: `{success, type, total_pages, pages_with_vision, pages_skipped_vision, failed_pages, text, minio_path}`. `failed_pages` is a list of page numbers where Vision errored or returned empty (those pages still appear in `text` with native fallback).

`GET /health` is unauthenticated.
