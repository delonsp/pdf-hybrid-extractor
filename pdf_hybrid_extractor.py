#!/usr/bin/env python3
"""
PDF Hybrid Extractor
- Extrai texto de PDFs
- Páginas com pouco texto (<50 chars) são enviadas para Vision AI
- Combina tudo em um texto único

Uso:
  python3 pdf_hybrid_extractor.py <pdf_url_or_path>
  
Ou como servidor Flask para n8n chamar:
  python3 pdf_hybrid_extractor.py --serve --port 5050
"""

import os
import sys
import io
import re
import hmac
import math
import time
import base64
import socket
import zipfile
import argparse
import ipaddress
import logging
import threading
import requests
from pathlib import Path
from urllib.parse import urlparse, urljoin
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# Opcional: Flask para modo servidor
try:
    from flask import Flask, request, jsonify
    from werkzeug.exceptions import HTTPException
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    from werkzeug.middleware.proxy_fix import ProxyFix
    HAS_LIMITER = True
except ImportError:
    HAS_LIMITER = False

# PDF processing
try:
    import fitz  # PyMuPDF
    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False

try:
    import mammoth
    HAS_MAMMOTH = True
except ImportError:
    HAS_MAMMOTH = False

# Vision AI (nova SDK google-genai)
from google import genai

# Config
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
API_TOKEN = os.getenv("PDF_EXTRACTOR_TOKEN")
TELEFONE_RE = re.compile(r"^\d{8,20}$")
MIN_TEXT_THRESHOLD = 50  # caracteres mínimos por página pra evitar Vision
# Cobertura mínima de imagem (área de imagens / área da página) pra ativar modo
# híbrido (texto nativo + Vision). Cobre laudos com header textual + ultrassom
# embutido — antes essas páginas tinham >50 chars de texto e a imagem era
# ignorada silenciosamente.
IMAGE_COVERAGE_THRESHOLD = float(os.getenv("IMAGE_COVERAGE_THRESHOLD", "0.20"))
MAX_VISION_PAGES = 15  # máximo de páginas processadas via Vision AI
GEMINI_TIMEOUT = 60  # segundos por chamada ao Gemini
VISION_PARALLEL = 3  # chamadas Vision paralelas por request (gthread compatível)
# Primário: alias dinâmico (hoje aponta pro flash mais novo, podendo ser preview/3.x).
# Fallback: pinned em 2.5-flash (estável). Cascata é per-page — falha pontual no
# primário não derruba o PDF inteiro.
VISION_MODEL = os.getenv("VISION_MODEL", "gemini-flash-latest")
VISION_MODEL_FALLBACK = os.getenv("VISION_MODEL_FALLBACK", "gemini-2.5-flash")
MAX_DOWNLOAD_BYTES = int(os.getenv("MAX_DOWNLOAD_BYTES", str(50 * 1024 * 1024)))  # 50 MB
# Redirects: seguidos manualmente pra revalidar CADA hop contra o guard anti-SSRF.
# requests seguiria sozinho e o guard só teria visto a URL inicial — um 302 para
# 169.254.169.254 (metadata da nuvem) ou pra rede interna passava direto.
MAX_REDIRECTS = int(os.getenv("MAX_REDIRECTS", "3"))
# Allowlist opcional de hosts de origem (ex: "media.z-api.io,meu-minio.exemplo.com").
# Vazio = desligada (só o guard de IP privado vale). Ligar fecha redirect E DNS
# rebinding de uma vez, porque o host precisa ser conhecido em todo hop.
ALLOWED_DOWNLOAD_HOSTS = {
    h.strip().lower().rstrip(".")
    for h in os.getenv("ALLOWED_DOWNLOAD_HOSTS", "").split(",")
    if h.strip()
}
# Deadline TOTAL do download. O timeout do requests é por operação de socket: um
# servidor que manda poucos bytes a cada N segundos segura a thread pra sempre.
DOWNLOAD_DEADLINE = int(os.getenv("DOWNLOAD_DEADLINE", "120"))  # segundos
DOWNLOAD_CONNECT_TIMEOUT = int(os.getenv("DOWNLOAD_CONNECT_TIMEOUT", "10"))
DOWNLOAD_READ_TIMEOUT = int(os.getenv("DOWNLOAD_READ_TIMEOUT", "30"))
# Teto de rasterização. Nem o cap de bytes nem o de páginas protege disso: um PDF
# de poucos KB com MediaBox gigante faz o get_pixmap alocar GB — × VISION_PARALLEL.
# Pixmap RGB gasta ~3 bytes/pixel, então 20 MP ≈ 60 MB por página em voo.
MAX_RENDER_PIXELS = int(os.getenv("MAX_RENDER_PIXELS", str(20_000_000)))
VISION_ZOOM = float(os.getenv("VISION_ZOOM", "2.0"))
MIN_RENDER_ZOOM = float(os.getenv("MIN_RENDER_ZOOM", "0.25"))
MAX_TOTAL_PAGES = int(os.getenv("MAX_TOTAL_PAGES", "500"))
MAX_OUTPUT_CHARS = int(os.getenv("MAX_OUTPUT_CHARS", str(5_000_000)))
# Limites de expansão do ZIP do DOCX (zip bomb): conferir a estrutura não protege
# contra descompressão abusiva — um .docx de poucos MB vira GB dentro do mammoth.
MAX_DOCX_UNCOMPRESSED = int(os.getenv("MAX_DOCX_UNCOMPRESSED_BYTES", str(200 * 1024 * 1024)))
MAX_ZIP_ENTRIES = int(os.getenv("MAX_ZIP_ENTRIES", "2000"))
MAX_COMPRESSION_RATIO = float(os.getenv("MAX_COMPRESSION_RATIO", "200"))
ALLOWED_DOWNLOAD_TYPES = {
    "application/pdf",
    "application/octet-stream",
    "binary/octet-stream",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "application/zip",  # alguns servers servem .docx assim
}
VISION_PROMPT = """Analise esta imagem de um documento médico/exame e extraia TODAS as informações textuais visíveis.
Inclua:
- Dados do paciente (nome, idade, data)
- Nome do exame
- Resultados e valores
- Valores de referência
- Conclusões e observações
- Assinaturas e datas

Formate de forma clara e organizada. Se for uma imagem de exame (ultrassom, raio-x, etc), descreva o que é visível."""


def setup_gemini():
    """Configura cliente do Gemini (nova SDK) com timeout"""
    if not GOOGLE_API_KEY:
        raise ValueError("GOOGLE_API_KEY ou GEMINI_API_KEY não configurada")
    return genai.Client(
        api_key=GOOGLE_API_KEY,
        http_options={"timeout": GEMINI_TIMEOUT * 1000}  # ms
    )


def _host_allowed(host: str) -> bool:
    """Host bate na allowlist (match exato ou subdomínio). Allowlist vazia = tudo
    liberado (só o guard de IP privado vale)."""
    if not ALLOWED_DOWNLOAD_HOSTS:
        return True
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOWNLOAD_HOSTS)


def _assert_safe_url(url: str) -> None:
    """Guarda anti-SSRF: rejeita schemes não-HTTP, hosts fora da allowlist (quando
    configurada) e IPs internos.
    Why: bearer dá acesso à rota; sem isso, atacante atinge metadata da nuvem
    (169.254.169.254) e serviços internos. How to apply: chamar antes de
    CADA hop HTTP — não só na URL original (ver download_file)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValueError("URL missing host")
    if not _host_allowed(host):
        raise ValueError(f"host not in allowlist: {host}")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError(f"could not resolve host: {host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise ValueError(f"refusing to fetch internal address: {ip}")


def _read_capped(resp, deadline: float) -> bytes:
    """Lê o corpo com cap de tamanho e deadline total."""
    clen = resp.headers.get("Content-Length")
    if clen and clen.isdigit() and int(clen) > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"file too large: {clen} bytes > cap {MAX_DOWNLOAD_BYTES}")

    # Content-Type — warn (não rejeita) em tipos inesperados; magic-bytes
    # depois detecta de verdade. Rejeitar aqui quebraria servers que
    # mandam text/plain pra tudo.
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype and ctype not in ALLOWED_DOWNLOAD_TYPES:
        logger.warning(f"Content-Type inesperado: {ctype!r} (continuando)")

    buf = io.BytesIO()
    total = 0
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        if time.monotonic() > deadline:
            raise ValueError(f"download exceeded deadline of {DOWNLOAD_DEADLINE}s")
        total += len(chunk)
        if total > MAX_DOWNLOAD_BYTES:
            raise ValueError(f"file too large: exceeded {MAX_DOWNLOAD_BYTES} bytes during download")
        buf.write(chunk)
    return buf.getvalue()


def download_file(url: str) -> bytes:
    """Baixa arquivo de URL revalidando o guard anti-SSRF em CADA redirect.

    requests segue redirect por padrão e o guard só teria visto a URL inicial —
    um servidor externo devolvendo `302 -> 169.254.169.254` furava a proteção.
    Aqui os hops são seguidos à mão (`allow_redirects=False`), com `urljoin` pra
    `Location` relativo, teto de hops, cap de tamanho e deadline total."""
    deadline = time.monotonic() + DOWNLOAD_DEADLINE
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _assert_safe_url(current)
        resp = requests.get(
            current,
            timeout=(DOWNLOAD_CONNECT_TIMEOUT, DOWNLOAD_READ_TIMEOUT),
            stream=True,
            allow_redirects=False,
        )
        try:
            if resp.is_redirect or resp.is_permanent_redirect:
                location = resp.headers.get("Location")
                if not location:
                    raise ValueError("redirect without Location header")
                # Location pode ser relativo — resolver contra a URL atual,
                # senão o guard receberia algo que não é URL absoluta.
                current = urljoin(current, location)
                continue
            resp.raise_for_status()
            return _read_capped(resp, deadline)
        finally:
            resp.close()
    raise ValueError(f"too many redirects (max {MAX_REDIRECTS})")


def _detect_type(data: bytes) -> str:
    """Detecta tipo de documento via magic bytes."""
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.startswith(b"PK\x03\x04"):
        return "docx"
    return "unknown"


def _assert_safe_docx(docx_bytes: bytes) -> None:
    """Valida estrutura do DOCX e barra zip bomb ANTES de entregar ao mammoth.

    Conferir só a estrutura não protege: um .docx de poucos MB pode expandir para
    GB durante a leitura. Os três limites (tamanho descomprimido, nº de entradas
    e taxa de compressão) são lidos do índice do ZIP, sem descomprimir nada."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(docx_bytes))
    except zipfile.BadZipFile as e:
        raise ValueError(f"DOCX inválido (não é um ZIP legível): {e}") from e

    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise ValueError(f"DOCX com entradas demais: {len(infos)} > {MAX_ZIP_ENTRIES}")

        names = {i.filename for i in infos}
        # Todo ZIP começa com PK\x03\x04 — xlsx/pptx caíam aqui e morriam com
        # erro confuso do mammoth. Exigir a parte que só existe em DOCX.
        if "word/document.xml" not in names:
            raise ValueError("arquivo ZIP não é um DOCX (falta word/document.xml)")

        total_uncompressed = sum(i.file_size for i in infos)
        if total_uncompressed > MAX_DOCX_UNCOMPRESSED:
            raise ValueError(
                f"DOCX expande demais: {total_uncompressed} bytes > cap {MAX_DOCX_UNCOMPRESSED}"
            )
        ratio = total_uncompressed / max(len(docx_bytes), 1)
        if ratio > MAX_COMPRESSION_RATIO:
            raise ValueError(
                f"taxa de compressão suspeita ({ratio:.0f}x > {MAX_COMPRESSION_RATIO:.0f}x)"
            )


def extract_text_docx(docx_bytes: bytes) -> str:
    """Extrai texto cru de DOCX via mammoth (sem Vision)."""
    if not HAS_MAMMOTH:
        raise RuntimeError("mammoth não instalado. Execute: pip install mammoth")
    _assert_safe_docx(docx_bytes)
    return mammoth.extract_raw_text(io.BytesIO(docx_bytes)).value


def analyze_image_with_vision(client, image_bytes: bytes, page_num: int) -> str | None:
    """Envia imagem para Gemini Vision com cascata primário→fallback.

    Tenta VISION_MODEL primeiro; em falha (exceção, vazio ou safety filter),
    tenta VISION_MODEL_FALLBACK uma vez. Retorna o primeiro texto não-vazio,
    ou None se todos falharem. Falha intermediária loga warning; falha final
    loga exception com stack."""
    from google.genai import types
    image_part = types.Part.from_bytes(data=image_bytes, mime_type="image/png")

    models_to_try = [VISION_MODEL]
    if VISION_MODEL_FALLBACK and VISION_MODEL_FALLBACK != VISION_MODEL:
        models_to_try.append(VISION_MODEL_FALLBACK)

    for idx, model_name in enumerate(models_to_try):
        is_last = idx == len(models_to_try) - 1
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=[VISION_PROMPT, image_part]
            )
            text = response.text
            if text:
                if idx > 0:
                    logger.info(f"Página {page_num}: fallback {model_name} ok")
                return text
            msg = f"Página {page_num}: {model_name} retornou vazio (provável safety filter)"
            logger.warning(msg if is_last else f"{msg}, tentando fallback")
        except Exception as e:
            if is_last:
                logger.exception(f"Página {page_num}: {model_name} falhou (último modelo)")
            else:
                logger.warning(f"Página {page_num}: {model_name} falhou ({e!r}), tentando fallback")
    return None


def _page_image_coverage(page) -> float:
    """Fração da área da página coberta por imagens raster embutidas.

    Usado pra detectar páginas "híbridas": texto nativo + imagem grande
    (ex: laudo médico com cabeçalho textual + ultrassom). Threshold-só-de-
    chars classificava essas como native_only, perdendo o conteúdo da
    imagem silenciosamente."""
    try:
        page_area = abs(page.rect.get_area())
    except Exception:
        return 0.0
    if not page_area:
        return 0.0
    try:
        infos = page.get_image_info()  # PyMuPDF >= 1.20
    except Exception:
        return 0.0
    total = 0.0
    for info in infos:
        bbox = info.get("bbox")
        if not bbox:
            continue
        try:
            total += abs(fitz.Rect(bbox).get_area())
        except Exception:
            continue
    return min(total / page_area, 1.0)


def _render_page_png(page) -> bytes:
    """Rasteriza a página em PNG respeitando MAX_RENDER_PIXELS.

    Uma página com MediaBox gigante faria o get_pixmap alocar centenas de MB a GB
    — o cap de bytes do download não protege disso, porque o PDF em si pode ter
    poucos KB. Aqui o zoom é reduzido até caber no teto; se para caber ele tiver
    que ficar abaixo de MIN_RENDER_ZOOM, a página é recusada como falha isolada
    (render ilegível não vale a chamada ao Gemini) em vez de derrubar o processo."""
    rect = page.rect
    width, height = abs(rect.width), abs(rect.height)
    if not width or not height:
        raise ValueError("página com dimensão zero")

    zoom = VISION_ZOOM
    if width * height * zoom * zoom > MAX_RENDER_PIXELS:
        zoom = math.sqrt(MAX_RENDER_PIXELS / (width * height))
        # Abaixo de um piso o render sai ilegível e a chamada ao Gemini seria
        # gasto sem retorno — melhor falhar a página do que mandar borrão.
        if zoom < MIN_RENDER_ZOOM:
            raise ValueError(
                f"página grande demais para rasterizar de forma legível "
                f"({width:.0f}x{height:.0f}pt, zoom cairia para {zoom:.3f})"
            )
        logger.warning(
            f"Página {page.number + 1}: {width:.0f}x{height:.0f}pt excede "
            f"MAX_RENDER_PIXELS, zoom reduzido para {zoom:.2f}"
        )

    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    return pix.tobytes("png")


def process_pdf(pdf_source: str | bytes, save_to_minio: bool = False,
                telefone: str = None, minio_config: dict = None) -> dict:
    """
    Processa PDF com extração híbrida em 3 modos por página:
    - "native":  só texto via PyMuPDF (texto suficiente E sem imagem grande)
    - "vision":  só Vision (sem texto nativo útil)
    - "hybrid":  texto + Vision (texto suficiente MAS imagem cobre
                 IMAGE_COVERAGE_THRESHOLD da página — ex: laudo com header
                 textual + ultrassom embutido; sem o modo híbrido a imagem
                 ficava silenciosa)

    Pages "vision" e "hybrid" entram na fila de Vision (lazy render +
    paralelo, até VISION_PARALLEL simultâneas e MAX_VISION_PAGES no total).
    Falha do Vision em página individual cai pro texto nativo (se houver)
    e a página entra em `failed_pages`.
    """
    if not HAS_PYMUPDF:
        raise RuntimeError("PyMuPDF (fitz) é obrigatório para processar PDFs")

    # Obtém bytes do PDF
    if isinstance(pdf_source, bytes):
        pdf_bytes = pdf_source
    elif pdf_source.startswith(('http://', 'https://')):
        pdf_bytes = download_file(pdf_source)
    else:
        pdf_bytes = Path(pdf_source).read_bytes()

    # Salva no Minio se configurado
    minio_path = None
    if save_to_minio and telefone and minio_config:
        minio_path = save_to_minio_storage(pdf_bytes, telefone, minio_config)

    # Abertura defensiva: PDFs inválidos/criptografados viram ValueError → 400
    # com mensagem útil em vez do "internal error" genérico do handler de 500.
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        raise ValueError(f"PDF inválido ou corrompido: {e}") from e

    if doc.is_encrypted and not doc.authenticate(""):
        doc.close()
        raise ValueError("PDF está criptografado/protegido por senha")

    # Cap de páginas: um PDF de poucos KB pode declarar milhares de páginas, e a
    # passada 1 roda get_text() em todas antes de qualquer outro limite valer.
    if doc.page_count > MAX_TOTAL_PAGES:
        page_count = doc.page_count
        doc.close()
        raise ValueError(f"PDF com páginas demais: {page_count} > cap {MAX_TOTAL_PAGES}")

    # Lock pra render do PyMuPDF: MuPDF NÃO é thread-safe num mesmo Document.
    # Render é rápido (~50-200ms); a chamada Gemini lenta (5-30s) continua
    # paralela. Sem isso há risco de pixmap corrompido / segfault sob carga.
    doc_lock = threading.Lock()

    try:
        # Passada 1: classifica cada página em native | vision | hybrid (sem renderizar)
        page_metas = []
        classify_failed = []
        for i, page in enumerate(doc):
            # get_text() também estoura em página corrompida (JBIG2/JPX quebrado,
            # xref inconsistente). Sem esta guarda, a passada 1 inteira morria e
            # o documento todo virava 500 — inclusive as páginas boas.
            try:
                text = page.get_text().strip()
            except Exception:
                logger.exception(f"Página {i + 1}: falha ao extrair texto nativo")
                classify_failed.append(i + 1)
                text = ""
            has_text = len(text) >= MIN_TEXT_THRESHOLD
            img_coverage = _page_image_coverage(page)
            has_significant_image = img_coverage >= IMAGE_COVERAGE_THRESHOLD

            if not has_text:
                mode = "vision"
            elif has_significant_image:
                mode = "hybrid"
            else:
                mode = "native"

            page_metas.append({
                "page_num": i + 1,
                "text": text,
                "mode": mode,
                "img_coverage": round(img_coverage, 3),
            })

        # Cap em MAX_VISION_PAGES; resto vira "skipped"
        to_vision = [m for m in page_metas if m["mode"] != "native"]
        to_vision_capped = to_vision[:MAX_VISION_PAGES]
        skipped_set = {m["page_num"] for m in to_vision[MAX_VISION_PAGES:]}

        n_hybrid = sum(1 for m in page_metas if m["mode"] == "hybrid")
        n_vision_only = sum(1 for m in page_metas if m["mode"] == "vision")
        logger.info(
            f"PDF: {len(page_metas)} páginas — "
            f"{n_vision_only} vision-only, {n_hybrid} híbridas, "
            f"{len(to_vision_capped)} processadas, {len(skipped_set)} ignoradas pelo cap"
        )

        # Passada 2: render lazy + Vision em paralelo
        vision_results: dict[int, str | None] = {}
        if to_vision_capped:
            client = setup_gemini()

            def _vision_one(meta):
                # try/except cobrindo o RENDER, não só a chamada ao Gemini:
                # analyze_image_with_vision engole as próprias exceções, mas uma
                # falha no get_pixmap escapava, o ex.map re-levantava e o PDF
                # inteiro virava 500 — jogando fora as páginas já extraídas.
                pn = meta["page_num"]
                try:
                    with doc_lock:
                        img_bytes = _render_page_png(doc[pn - 1])
                except Exception:
                    logger.exception(f"Página {pn}: falha ao rasterizar")
                    return pn, None
                return pn, analyze_image_with_vision(client, img_bytes, pn)

            with ThreadPoolExecutor(max_workers=VISION_PARALLEL) as ex:
                for pn, text in ex.map(_vision_one, to_vision_capped):
                    vision_results[pn] = text

        # Monta o output
        results = []
        pages_with_vision = 0
        pages_hybrid = 0
        pages_skipped_vision = 0
        failed_pages = []

        for meta in page_metas:
            pn = meta["page_num"]
            native = meta["text"]
            mode = meta["mode"]

            if pn in vision_results:
                vtext = vision_results[pn]
                if vtext is None:
                    failed_pages.append(pn)
                    if mode == "hybrid":
                        results.append(
                            f"--- Página {pn} (Vision AI falhou - usando só texto) ---\n{native}"
                        )
                    else:
                        results.append(
                            f"--- Página {pn} (Vision AI falhou - usando texto nativo) ---\n{native}"
                        )
                else:
                    pages_with_vision += 1
                    if mode == "hybrid":
                        pages_hybrid += 1
                        results.append(
                            f"--- Página {pn} (texto + Vision AI) ---\n{native}\n\n"
                            f"[Vision AI - conteúdo de imagem]:\n{vtext}"
                        )
                    else:
                        results.append(f"--- Página {pn} (Vision AI) ---\n{vtext}")
            elif pn in skipped_set:
                pages_skipped_vision += 1
                if mode == "hybrid":
                    results.append(
                        f"--- Página {pn} (imagem ignorada - cap de {MAX_VISION_PAGES} atingido; usando só texto) ---\n{native}"
                    )
                else:
                    results.append(
                        f"--- Página {pn} (Vision AI ignorada - cap de {MAX_VISION_PAGES} atingido) ---\n{native}"
                    )
            else:
                results.append(f"--- Página {pn} ---\n{native}")

        # Páginas que falharam já na classificação também são falha — sem isso
        # sumiam do relatório quando não passavam pelo Vision.
        failed_pages = sorted(set(failed_pages) | set(classify_failed))

        combined_text = "\n\n".join(results)
        text_truncated = False
        if len(combined_text) > MAX_OUTPUT_CHARS:
            combined_text = (
                combined_text[:MAX_OUTPUT_CHARS]
                + f"\n\n--- [texto truncado em {MAX_OUTPUT_CHARS} caracteres] ---"
            )
            text_truncated = True
            logger.warning(f"Texto de saída truncado em {MAX_OUTPUT_CHARS} caracteres")

        logger.info(
            f"Concluído: {pages_with_vision} vision ok ({pages_hybrid} híbridas), "
            f"{len(failed_pages)} falharam, {pages_skipped_vision} ignoradas"
        )

        return {
            "success": True,
            "total_pages": len(page_metas),
            "pages_with_vision": pages_with_vision,
            "pages_hybrid": pages_hybrid,
            "pages_skipped_vision": pages_skipped_vision,
            "failed_pages": failed_pages,
            "text_truncated": text_truncated,
            "text": combined_text,
            "minio_path": minio_path,
        }
    finally:
        doc.close()


def save_to_minio_storage(pdf_bytes: bytes, telefone: str, config: dict) -> str | None:
    """Salva PDF no Minio. Object name usa timestamp UTC + sufixo UUID curto
    (evita colisão quando duas requests do mesmo telefone caem no mesmo
    segundo)."""
    try:
        import uuid
        from minio import Minio
        from datetime import datetime, timezone

        client = Minio(
            config["endpoint"],
            access_key=config["access_key"],
            secret_key=config["secret_key"],
            secure=config.get("secure", True)
        )

        bucket = config.get("bucket", "pacientes")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = uuid.uuid4().hex[:8]
        object_name = f"{telefone}/{timestamp}_{suffix}.pdf"

        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)

        client.put_object(
            bucket,
            object_name,
            io.BytesIO(pdf_bytes),
            len(pdf_bytes),
            content_type="application/pdf"
        )

        return f"{bucket}/{object_name}"
    except Exception:
        logger.exception("Erro ao salvar no Minio")
        return None


# === Modo Servidor Flask ===

def create_app():
    """Cria app Flask para modo servidor"""
    if not HAS_FLASK:
        raise RuntimeError("Flask não instalado. Execute: pip install flask")
    if not API_TOKEN:
        raise RuntimeError("PDF_EXTRACTOR_TOKEN env var must be set to start the server")

    app = Flask(__name__)

    # Teto de corpo HTTP. Sem isso, um POST de 1 GB de base64 existe ao mesmo
    # tempo como bytes crus, string base64 e bytes decodificados — com 1 worker,
    # um único request derruba o serviço inteiro por memória.
    # base64 infla 4/3, mais folga pro resto do JSON.
    app.config["MAX_CONTENT_LENGTH"] = (
        math.ceil(MAX_DOWNLOAD_BYTES / 3) * 4 + 1024 * 1024
    )

    # Atrás de proxy reverso (Traefik/Dokploy): respeita X-Forwarded-For
    # pra que get_remote_address devolva IP real do cliente, não do proxy
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    @app.errorhandler(HTTPException)
    def _json_errors(e):
        """Erro de HTTP sempre em JSON. O 413 do MAX_CONTENT_LENGTH, o 415 de
        corpo não-JSON e os 404/405/429 saíam em HTML e quebravam o parsing do
        n8n, que espera {"error": ...}."""
        return jsonify({"error": e.description, "success": False}), e.code

    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=[os.getenv("RATE_LIMIT_DEFAULT", "60 per minute")],
    )

    @app.route("/health", methods=["GET"])
    @limiter.exempt
    def health():
        return jsonify({"status": "ok"})

    @app.route("/extract", methods=["POST"])
    @limiter.limit(os.getenv("RATE_LIMIT_EXTRACT", "30 per minute"))
    def extract():
        """
        Endpoint para extrair texto de PDF.
        
        Body JSON:
        {
            "url": "https://...",  // ou
            "base64": "...",       // PDF em base64
            "telefone": "5511...", // opcional, para Minio
            "save_to_minio": false // opcional
        }
        
        Headers:
            Authorization: Bearer <token>
        """
        # Validação de token (constant-time)
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid Authorization header"}), 401
        token = auth_header[len("Bearer "):]
        if not hmac.compare_digest(token, API_TOKEN):
            return jsonify({"error": "Invalid token"}), 403

        data = request.json or {}

        # Validação de telefone (rejeita path injection no Minio)
        telefone = data.get("telefone")
        if telefone is not None and not TELEFONE_RE.fullmatch(str(telefone)):
            return jsonify({"error": "telefone deve conter apenas dígitos (8-20)"}), 400

        # save_to_minio sem telefone vira no-op silencioso — explicita
        if data.get("save_to_minio") and not telefone:
            return jsonify({"error": "save_to_minio=true requer 'telefone'"}), 400

        try:
            # Obtém PDF — URL deve ser http/https; download_file aplica guarda anti-SSRF
            if "url" in data:
                url = data["url"]
                if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                    return jsonify({"error": "'url' deve começar com http:// ou https://"}), 400
                pdf_source = download_file(url)
            elif "base64" in data:
                raw_b64 = data["base64"]
                if not isinstance(raw_b64, str):
                    raise ValueError("'base64' deve ser string")
                # Checar o tamanho ANTES de decodificar: decodificar primeiro pra
                # depois medir já teria materializado os bytes na memória.
                if len(raw_b64) // 4 * 3 > MAX_DOWNLOAD_BYTES:
                    raise ValueError(
                        f"base64 grande demais: excede o cap de {MAX_DOWNLOAD_BYTES} bytes"
                    )
                # validate=True: rejeita lixo cedo. binascii.Error herda de
                # ValueError, então cai no handler de 400 abaixo.
                try:
                    pdf_source = base64.b64decode(raw_b64, validate=True)
                except Exception as e:
                    raise ValueError(f"base64 inválido: {e}") from e
                if len(pdf_source) > MAX_DOWNLOAD_BYTES:
                    raise ValueError(
                        f"arquivo grande demais: {len(pdf_source)} bytes > cap {MAX_DOWNLOAD_BYTES}"
                    )
            else:
                return jsonify({"error": "Forneça 'url' ou 'base64'"}), 400

            # Tipo: override via body.type, senão auto-detecta por magic bytes
            doc_type = (data.get("type") or "").lower() or _detect_type(pdf_source)
            if doc_type == "docx":
                return jsonify({
                    "success": True,
                    "type": "docx",
                    "total_pages": 1,
                    "pages_with_vision": 0,
                    "pages_skipped_vision": 0,
                    "text": extract_text_docx(pdf_source),
                    "minio_path": None,
                })

            # Configuração Minio (do ambiente)
            minio_config = None
            if data.get("save_to_minio"):
                minio_config = {
                    "endpoint": os.getenv("MINIO_ENDPOINT"),
                    "access_key": os.getenv("MINIO_ACCESS_KEY"),
                    "secret_key": os.getenv("MINIO_SECRET_KEY"),
                    "secure": os.getenv("MINIO_SECURE", "true").lower() == "true",
                    "bucket": os.getenv("MINIO_BUCKET", "pacientes")
                }
            
            result = process_pdf(
                pdf_source,
                save_to_minio=data.get("save_to_minio", False),
                telefone=telefone,
                minio_config=minio_config
            )
            result["type"] = "pdf"

            return jsonify(result)

        except ValueError as e:
            # ValueError = input ruim (URL inválida, SSRF, file too large, etc).
            # Mensagem é útil pro cliente saber o que ajustar.
            return jsonify({"error": str(e), "success": False}), 400
        except Exception:
            # Tudo que não é ValueError vira mensagem genérica — não vaza
            # paths/hostnames/stack pro cliente. Stack vai pro log.
            logger.exception("Erro inesperado em /extract")
            return jsonify({"error": "internal error", "success": False}), 500
    
    return app


# === CLI ===

def main():
    parser = argparse.ArgumentParser(description="PDF Hybrid Extractor")
    parser.add_argument("pdf", nargs="?", help="URL ou caminho do PDF")
    parser.add_argument("--serve", action="store_true", help="Rodar como servidor Flask")
    parser.add_argument("--port", type=int, default=5050, help="Porta do servidor")
    parser.add_argument("--host", default="0.0.0.0", help="Host do servidor")
    
    args = parser.parse_args()
    
    if args.serve:
        app = create_app()
        print(f"🚀 Servidor rodando em http://{args.host}:{args.port}")
        app.run(host=args.host, port=args.port, debug=False)
    elif args.pdf:
        result = process_pdf(args.pdf)
        print(f"Total de páginas: {result['total_pages']}")
        print(f"Páginas com Vision AI: {result['pages_with_vision']}")
        print("=" * 50)
        print(result["text"])
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
