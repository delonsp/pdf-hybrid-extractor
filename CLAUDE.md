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

# Tests
pip install -r requirements-dev.txt
pytest                          # 101 tests, ~1s
pytest tests/test_vision.py -v  # single file
pytest -k "encrypted or ssrf"   # by name pattern
```

Tests live in `tests/` (pytest + pytest-mock). Gemini and `requests.get` are mocked — suite runs fully offline. PDFs are synthesized in-memory via `fitz`. No linter/formatter configured.

## Required env vars

- `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) — Gemini Vision
- `PDF_EXTRACTOR_TOKEN` — bearer token for `/extract`. **Required**; `create_app()` raises if missing. No default — service won't start without it.
- Optional Minio: `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, `MINIO_SECURE`, `MINIO_BUCKET`
- Optional tuning: `VISION_MODEL` (default `gemini-flash-latest`), `VISION_MODEL_FALLBACK` (default `gemini-2.5-flash`), `MAX_DOWNLOAD_BYTES` (default 50 MB), `IMAGE_COVERAGE_THRESHOLD` (default 0.20), `RATE_LIMIT_DEFAULT` (default `60 per minute`), `RATE_LIMIT_EXTRACT` (default `30 per minute`)
- Optional limits (added in the Lote A hardening — see `PRD-atualizacao-extrator.md`):
  - `ALLOWED_DOWNLOAD_HOSTS` — comma-separated **suffix** allowlist (e.g. `backblazeb2.com,temp-file.download`). Suffix, not full host, on purpose: Z-API serves media from `f004.backblazeb2.com`, and a bucket migration to `f005` must not break ingestion. Empty = disabled.
  - `ALLOWED_DOWNLOAD_HOSTS_ENFORCE` (default `false`) — **two-stage rollout by design.** With the list set but enforce off, an out-of-list host only logs a WARNING and the download proceeds; a new domain becomes a log line instead of a silently lost report. Flip to `true` only after observing real traffic. Setting the list alone can never break production. **Enforcing closes both redirect-based SSRF and DNS rebinding**, since every hop must resolve to a known host.
  - Every download logs its origin **host only** (`[origem] download de <host>`) — never the full URL, whose path carries a signed token and the patient's file id. This is what accumulates the real domain list before enforcing.
  - `MAX_REDIRECTS` (3), `DOWNLOAD_DEADLINE` (120s total, not per-socket), `DOWNLOAD_CONNECT_TIMEOUT` (10s), `DOWNLOAD_READ_TIMEOUT` (30s)
  - `MAX_RENDER_PIXELS` (20 MP), `VISION_ZOOM` (2.0), `MIN_RENDER_ZOOM` (0.25)
  - `MAX_TOTAL_PAGES` (500), `MAX_OUTPUT_CHARS` (5M)
  - `MAX_DOCX_UNCOMPRESSED_BYTES` (200 MB), `MAX_ZIP_ENTRIES` (2000), `MAX_COMPRESSION_RATIO` (200)
- Time budget (Lote B — **the numbers are calibration, tune them in Dokploy without a deploy**):
  - `REQUEST_DEADLINE` (110s) — **must stay below the caller's timeout**, which is 120s for the webhook. Past that, work is guaranteed-discarded while still holding a live thread.
  - `GEMINI_TIMEOUT` (45s) — measured page latency is 15–19s end-to-end, so a tight 25–30s ceiling would cut exactly the dense reports that matter. Kept loose on purpose; the deadline governs the total, and each call's timeout is additionally clamped to whatever budget remains.
  - `FALLBACK_MIN_BUDGET` (25s) — the primary→fallback cascade only fires if at least this much budget is left. Unconditional, it doubled a page's cost precisely when time was tightest, stealing the budget from later pages.
  - `MAX_CONCURRENT_EXTRACTIONS` (3) — deliberately below the gunicorn thread count so a thread is always free for `/health` and for fast 503s. Over capacity returns **503 + `Retry-After`** instead of queueing: waiting comes out of the same 120s budget, so a queue only converts fast rejection into slow, silent timeout.
  - `RETRY_AFTER_SECONDS` (30), `ABORT_ON_CLIENT_DISCONNECT` (`true`)
  - `MAX_VISION_PAGES`, `VISION_PARALLEL`, `MIN_TEXT_THRESHOLD` are now env-tunable too (were hardcoded).
  - **Before raising `VISION_PARALLEL`**: it multiplies by the thread count. 5 × 4 threads = 20 concurrent Gemini calls at peak — check the account's RPM first, or you trade timeouts for mass 429s.

## Architecture

Pipeline in `process_pdf()`:

1. **Open document** — `fitz.open(stream=pdf_bytes, filetype="pdf")` (kept open for the whole pipeline so rendering can be lazy).
2. **Pass 1: classify each page into one of 3 modes** (no rendering yet):
   - **`native`** — `len(text) >= MIN_TEXT_THRESHOLD` (50 chars) AND image area < `IMAGE_COVERAGE_THRESHOLD` (20% of page). Native text only.
   - **`vision`** — `len(text) < MIN_TEXT_THRESHOLD`. Pure scan / image page; native text useless.
   - **`hybrid`** — text >= threshold AND image >= 20% of page. Covers medical reports with textual header + embedded ultrasound/x-ray. Without `hybrid`, those pages were native-only and the image content was silently lost.
   Image coverage computed by `_page_image_coverage(page)` via `page.get_image_info()` (PyMuPDF >= 1.20) — sums bbox areas of raster images, divided by page area. Cheap (no pixel extraction).
3. **Pass 2: Vision (parallel + lazy render)** — `vision` and `hybrid` pages enter the queue, capped at `MAX_VISION_PAGES`. The cap'd subset goes through `ThreadPoolExecutor(max_workers=VISION_PARALLEL)`. Each worker renders its page (2x zoom PNG) only when picked up, calls `analyze_image_with_vision`, and bytes go out of scope on return. Pages over the cap stay with their native text and are tagged "ignorada".
4. **Failure handling** — `analyze_image_with_vision` returns `None` on exception OR empty/blocked response (safety filter). Failed pages fall back to native text (when `hybrid`/has any) and land in `failed_pages` in the response.
5. **Combine** — page outputs joined with markers depending on mode and outcome:
   - `--- Página N ---` (native)
   - `--- Página N (Vision AI) ---` (vision-only success)
   - `--- Página N (texto + Vision AI) ---` (hybrid success — text + Vision concatenated)
   - `--- Página N (Vision AI falhou ...) ---` / `(imagem ignorada - cap atingido ...)` for failures or cap.

Vision model: `gemini-2.5-flash` (set at `VISION_MODEL`). When changing models, sanity-check the SDK still accepts `types.Part.from_bytes(..., mime_type="image/png")` and that `response.text` shape is unchanged — newer models sometimes shift candidate handling.

DOCX path (Flask route only, not `process_pdf`): `_detect_type()` peeks magic bytes (`%PDF` vs `PK\x03\x04`); DOCX bytes go straight to `extract_text_docx` (mammoth). Caller can override with `type: "pdf"|"docx"` in the body.

Optional: original PDF persisted to Minio at `<bucket>/<telefone>/<timestamp>.pdf` when `save_to_minio=True`. DOCX uploads to Minio not yet implemented.

### Key constants (top of file)

`MIN_TEXT_THRESHOLD` (50), `IMAGE_COVERAGE_THRESHOLD` (0.20 — ratio of page area covered by raster images to trigger hybrid mode), `MAX_VISION_PAGES` (15), `GEMINI_TIMEOUT` (60s, passed as ms to the SDK's `http_options`), `VISION_PARALLEL` (3), `VISION_MODEL`, `VISION_PROMPT`, `TELEFONE_RE`. Tune these before adding new knobs.

### Gunicorn config rationale — **two claims here were wrong; corrected 2026-08-05**

`workers=1 threads=4 gthread timeout=480`. Single worker keeps memory bounded; gthread serves multiple HTTP requests concurrently. Each request *also* spins up its own `ThreadPoolExecutor(max_workers=VISION_PARALLEL=3)` for parallel Gemini calls — those are independent of gunicorn's thread pool.

**⚠ `--timeout 480` does NOT bound request duration here.** Verified in gunicorn's `workers/gthread.py`: the worker loop calls `self.notify()` unconditionally every iteration, without checking whether request threads are stuck. With `gthread`, `timeout` is a *silent-worker* detector, not a request cap. A slow extraction runs indefinitely, holding one of the 4 threads; four of them and the service stops responding with no crash, no error log, and no restart. **The only enforceable time cap is a deadline in application code** (PRD item B3 — not yet implemented).

**⚠ Worst-case Vision time was understated.** The documented `ceil(15/3) × 60 = 300s` ignores the primary→fallback cascade, which costs up to `2 × GEMINI_TIMEOUT` per page: `5 waves × 120s = 600s`, before any retry. Any change to `MAX_VISION_PAGES`, `VISION_PARALLEL`, `GEMINI_TIMEOUT` or retry must be sized in one shared budget.

Don't blindly raise `workers` without checking memory — and note it would also multiply the in-memory rate limit by N.

### Security guards (already in place)

- `_assert_safe_url` blocks non-http(s) schemes, hosts outside `ALLOWED_DOWNLOAD_HOSTS` (when set), and any private/loopback/link-local/multicast/reserved IP — anti-SSRF.
- **`download_file` follows redirects manually (`allow_redirects=False`) and re-runs the guard on every hop.** Previously `requests` followed them itself and the guard had only seen the original URL — a public host answering `302 → 169.254.169.254` (cloud metadata) or `→ 10.x.x.x` (internal Minio) went straight through. Relative `Location` is resolved with `urljoin`; hops capped by `MAX_REDIRECTS`.
- **Known gap:** DNS rebinding is still open (the guard resolves, then `requests` resolves again). Setting `ALLOWED_DOWNLOAD_HOSTS` is what closes it.
- `download_file` streams with a hard cap (`MAX_DOWNLOAD_BYTES`, default 50 MB) — pre-checks `Content-Length` then enforces during chunked read; warns on Content-Type outside `ALLOWED_DOWNLOAD_TYPES`. `DOWNLOAD_DEADLINE` bounds total time (requests' `timeout` is per-socket-op, so a slow-drip server could hold a thread forever).
- **Resource caps** (each one guards a way to kill the single worker that the byte cap does not): `MAX_CONTENT_LENGTH` on the Flask app (base64 inflates 4/3, so the body existed three times over); base64 length checked *before* decoding; `MAX_RENDER_PIXELS` (a tiny PDF with a huge MediaBox made `get_pixmap` allocate GB); `MAX_TOTAL_PAGES`; `MAX_OUTPUT_CHARS`; and DOCX zip-bomb limits (uncompressed size, entry count, compression ratio) applied *before* mammoth reads anything.
- **All HTTP errors return JSON** via an `HTTPException` handler — 413/415/404/405/429 used to come back as HTML and break n8n's parsing.
- **PyMuPDF never runs inside a thread pool.** Rendering happens in the request's own thread; only the Gemini HTTP call is parallelised (that's where the 4–30s lives — rendering is 50–200ms). Additionally, `_pymupdf_lock` is a **process-wide** lock serialising every PyMuPDF call. The old `doc_lock` was created inside `process_pdf`, so it was per-request: the 4 gunicorn threads used MuPDF simultaneously with locks that could not see each other. PyMuPDF's docs support multiprocessing but **not** multithreading, and the failure mode is silent — a corrupted pixmap becomes garbage "OCR" text in a medical record, or the interpreter dies and takes all 4 in-flight requests with it. **Rule when editing: no PyMuPDF object (Document, Page, Pixmap) may be touched outside that lock** — the helpers enter it, do the whole operation, and return plain data.
- **Per-page failure isolation:** both `get_text()` (pass 1) and the render are wrapped per page. A corrupt page lands in `failed_pages`; previously an exception escaped `ThreadPoolExecutor.map` and turned the whole document into a 500, discarding pages already extracted.
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

Response: `{success, complete, type, total_pages, pages_with_vision, pages_hybrid, pages_skipped_vision, pages_deadline_skipped, deadline_exceeded, caller_gone, failed_pages, text_truncated, text, minio_path}`.

**`complete` is the key the caller must check.** It is `false` whenever anything was dropped — page cap, deadline, caller disconnect, per-page failure, or text truncation. `success: true` only means the request itself did not error; it never meant the extraction was whole. Persisting a record without checking `complete` is how an incomplete clinical document gets stored with nobody noticing.

`pages_deadline_skipped` / `deadline_exceeded` are kept separate from `failed_pages` on purpose: running out of budget is not a Vision failure, and conflating them makes diagnosis harder. `caller_gone: true` means the caller disconnected and extraction stopped early.

`503` + `Retry-After` means the server was at capacity — retry with backoff, the request was not processed. `pages_hybrid` is a subset of `pages_with_vision` — pages where text + Vision were combined (medical-report case). `failed_pages` lists page numbers where Vision errored or returned empty (those pages still appear in `text` with native fallback when available).

`GET /health` is unauthenticated.
