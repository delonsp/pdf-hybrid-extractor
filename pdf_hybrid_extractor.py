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
import base64
import socket
import argparse
import ipaddress
import logging
import requests
from pathlib import Path
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# Opcional: Flask para modo servidor
try:
    from flask import Flask, request, jsonify
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False

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
MIN_TEXT_THRESHOLD = 50  # caracteres mínimos por página
MAX_VISION_PAGES = 15  # máximo de páginas processadas via Vision AI
GEMINI_TIMEOUT = 60  # segundos por chamada ao Gemini
VISION_PARALLEL = 3  # chamadas Vision paralelas por request (gthread compatível)
# Primário: alias dinâmico (hoje aponta pro flash mais novo, podendo ser preview/3.x).
# Fallback: pinned em 2.5-flash (estável). Cascata é per-page — falha pontual no
# primário não derruba o PDF inteiro.
VISION_MODEL = os.getenv("VISION_MODEL", "gemini-flash-latest")
VISION_MODEL_FALLBACK = os.getenv("VISION_MODEL_FALLBACK", "gemini-2.5-flash")
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


def _assert_safe_url(url: str) -> None:
    """Guarda anti-SSRF: rejeita schemes não-HTTP e IPs internos.
    Why: bearer dá acesso à rota; sem isso, atacante atinge metadata da nuvem
    (169.254.169.254) e serviços internos. How to apply: chamar antes de
    qualquer GET de URL vinda do request body."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValueError("URL missing host")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError(f"could not resolve host: {host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise ValueError(f"refusing to fetch internal address: {ip}")


def download_file(url: str) -> bytes:
    """Baixa arquivo de URL (com guarda anti-SSRF)."""
    _assert_safe_url(url)
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content


def _detect_type(data: bytes) -> str:
    """Detecta tipo de documento via magic bytes."""
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.startswith(b"PK\x03\x04"):
        return "docx"
    return "unknown"


def extract_text_docx(docx_bytes: bytes) -> str:
    """Extrai texto cru de DOCX via mammoth (sem Vision)."""
    if not HAS_MAMMOTH:
        raise RuntimeError("mammoth não instalado. Execute: pip install mammoth")
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


def process_pdf(pdf_source: str | bytes, save_to_minio: bool = False,
                telefone: str = None, minio_config: dict = None) -> dict:
    """
    Processa PDF com extração híbrida.

    - Texto nativo via PyMuPDF.
    - Páginas com <MIN_TEXT_THRESHOLD chars são renderizadas (lazy) e enviadas
      ao Gemini Vision em paralelo (até VISION_PARALLEL simultâneas, até
      MAX_VISION_PAGES no total).
    - Falha do Vision em página individual não derruba a request: a página
      vai pra `failed_pages` e o texto nativo é usado como fallback.
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

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        # Passada 1: extrai texto e marca páginas que precisam de Vision (sem renderizar ainda)
        page_metas = []
        for i, page in enumerate(doc):
            text = page.get_text().strip()
            page_metas.append({
                "page_num": i + 1,
                "text": text,
                "needs_vision": len(text) < MIN_TEXT_THRESHOLD,
            })

        # Cap em MAX_VISION_PAGES; o resto é "skipped"
        to_vision = [m for m in page_metas if m["needs_vision"]]
        to_vision_capped = to_vision[:MAX_VISION_PAGES]
        skipped_set = {m["page_num"] for m in to_vision[MAX_VISION_PAGES:]}

        logger.info(
            f"PDF: {len(page_metas)} páginas, {len(to_vision)} precisam de Vision "
            f"({len(to_vision_capped)} processadas, {len(skipped_set)} ignoradas pelo cap)"
        )

        # Passada 2: render lazy + Vision em paralelo
        vision_results: dict[int, str | None] = {}
        if to_vision_capped:
            client = setup_gemini()

            def _vision_one(meta):
                pn = meta["page_num"]
                # Renderiza só agora — pix sai de escopo no return e é coletado
                pix = doc[pn - 1].get_pixmap(matrix=fitz.Matrix(2, 2))
                img_bytes = pix.tobytes("png")
                return pn, analyze_image_with_vision(client, img_bytes, pn)

            with ThreadPoolExecutor(max_workers=VISION_PARALLEL) as ex:
                for pn, text in ex.map(_vision_one, to_vision_capped):
                    vision_results[pn] = text

        # Monta o output
        results = []
        pages_with_vision = 0
        pages_skipped_vision = 0
        failed_pages = []

        for meta in page_metas:
            pn = meta["page_num"]
            native = meta["text"]
            if pn in vision_results:
                vtext = vision_results[pn]
                if vtext is None:
                    failed_pages.append(pn)
                    results.append(f"--- Página {pn} (Vision AI falhou - usando texto nativo) ---\n{native}")
                else:
                    pages_with_vision += 1
                    results.append(f"--- Página {pn} (Vision AI) ---\n{vtext}")
            elif pn in skipped_set:
                pages_skipped_vision += 1
                results.append(
                    f"--- Página {pn} (Vision AI ignorada - limite de {MAX_VISION_PAGES} atingido) ---\n{native}"
                )
            else:
                results.append(f"--- Página {pn} ---\n{native}")

        combined_text = "\n\n".join(results)
        logger.info(
            f"Concluído: {pages_with_vision} vision ok, "
            f"{len(failed_pages)} falharam, {pages_skipped_vision} ignoradas"
        )

        return {
            "success": True,
            "total_pages": len(page_metas),
            "pages_with_vision": pages_with_vision,
            "pages_skipped_vision": pages_skipped_vision,
            "failed_pages": failed_pages,
            "text": combined_text,
            "minio_path": minio_path,
        }
    finally:
        doc.close()


def save_to_minio_storage(pdf_bytes: bytes, telefone: str, config: dict) -> str:
    """Salva PDF no Minio"""
    try:
        from minio import Minio
        from datetime import datetime
        
        client = Minio(
            config["endpoint"],
            access_key=config["access_key"],
            secret_key=config["secret_key"],
            secure=config.get("secure", True)
        )
        
        bucket = config.get("bucket", "pacientes")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        object_name = f"{telefone}/{timestamp}.pdf"
        
        # Cria bucket se não existir
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
        
        # Upload
        client.put_object(
            bucket,
            object_name,
            io.BytesIO(pdf_bytes),
            len(pdf_bytes),
            content_type="application/pdf"
        )
        
        return f"{bucket}/{object_name}"
    except Exception as e:
        print(f"Erro ao salvar no Minio: {e}", file=sys.stderr)
        return None


# === Modo Servidor Flask ===

def create_app():
    """Cria app Flask para modo servidor"""
    if not HAS_FLASK:
        raise RuntimeError("Flask não instalado. Execute: pip install flask")
    if not API_TOKEN:
        raise RuntimeError("PDF_EXTRACTOR_TOKEN env var must be set to start the server")

    app = Flask(__name__)
    
    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok"})
    
    @app.route("/extract", methods=["POST"])
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

        try:
            # Obtém PDF — URL deve ser http/https; download_file aplica guarda anti-SSRF
            if "url" in data:
                url = data["url"]
                if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                    return jsonify({"error": "'url' deve começar com http:// ou https://"}), 400
                pdf_source = download_file(url)
            elif "base64" in data:
                pdf_source = base64.b64decode(data["base64"])
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
            return jsonify({"error": str(e), "success": False}), 400
        except Exception as e:
            return jsonify({"error": str(e), "success": False}), 500
    
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
