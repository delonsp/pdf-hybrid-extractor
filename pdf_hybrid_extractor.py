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
import base64
import tempfile
import argparse
import logging
import requests
from pathlib import Path

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
    from pdf2image import convert_from_path, convert_from_bytes
    HAS_PDF2IMAGE = True
except ImportError:
    HAS_PDF2IMAGE = False

# Vision AI (nova SDK google-genai)
from google import genai

# Config
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
API_TOKEN = os.getenv("PDF_EXTRACTOR_TOKEN", "XgsXBgexu5HARNDWgtb954QisyNJkB6gvx5PWrGgs7icw3tW")
MIN_TEXT_THRESHOLD = 50  # caracteres mínimos por página
MAX_VISION_PAGES = 15  # máximo de páginas processadas via Vision AI
GEMINI_TIMEOUT = 60  # segundos por chamada ao Gemini
VISION_MODEL = "gemini-2.0-flash"
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


def download_file(url: str) -> bytes:
    """Baixa arquivo de URL"""
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content


def extract_text_pymupdf(pdf_bytes: bytes) -> list[dict]:
    """Extrai texto página por página usando PyMuPDF.
    Imagens são renderizadas sob demanda (lazy) para economizar memória."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = []

    for i, page in enumerate(doc):
        text = page.get_text().strip()
        needs_vision = len(text) < MIN_TEXT_THRESHOLD

        img_bytes = None
        if needs_vision:
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # 2x zoom para qualidade
            img_bytes = pix.tobytes("png")

        pages.append({
            "page_num": i + 1,
            "text": text,
            "text_length": len(text),
            "needs_vision": needs_vision,
            "image_bytes": img_bytes
        })

    doc.close()
    return pages


def extract_text_pdf2image(pdf_bytes: bytes) -> list[dict]:
    """Extrai texto usando pdf2image + pytesseract como fallback"""
    import pytesseract
    
    # Salva temporariamente
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes)
        temp_path = f.name
    
    try:
        images = convert_from_path(temp_path, dpi=200)
        pages = []
        
        for i, img in enumerate(images):
            # OCR básico
            text = pytesseract.image_to_string(img, lang='por').strip()
            
            # Converte para bytes
            img_buffer = io.BytesIO()
            img.save(img_buffer, format='PNG')
            img_bytes = img_buffer.getvalue()
            
            pages.append({
                "page_num": i + 1,
                "text": text,
                "text_length": len(text),
                "image_bytes": img_bytes
            })
        
        return pages
    finally:
        os.unlink(temp_path)


def analyze_image_with_vision(client, image_bytes: bytes, page_num: int) -> str:
    """Envia imagem para Gemini Vision (nova SDK)"""
    try:
        from google.genai import types
        
        # Cria Part de imagem para nova SDK
        image_part = types.Part.from_bytes(data=image_bytes, mime_type="image/png")
        
        response = client.models.generate_content(
            model=VISION_MODEL,
            contents=[VISION_PROMPT, image_part]
        )
        
        return response.text
    except Exception as e:
        return f"[Erro ao analisar página {page_num}: {str(e)}]"


def process_pdf(pdf_source: str | bytes, save_to_minio: bool = False, 
                telefone: str = None, minio_config: dict = None) -> dict:
    """
    Processa PDF com extração híbrida.
    
    Args:
        pdf_source: URL, caminho do arquivo, ou bytes do PDF
        save_to_minio: Se deve salvar original no Minio
        telefone: Telefone do paciente (para path no Minio)
        minio_config: Configuração do Minio
    
    Returns:
        dict com texto combinado e metadados
    """
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
    
    # Extrai páginas
    if HAS_PYMUPDF:
        pages = extract_text_pymupdf(pdf_bytes)
    elif HAS_PDF2IMAGE:
        pages = extract_text_pdf2image(pdf_bytes)
    else:
        raise RuntimeError("Instale PyMuPDF (fitz) ou pdf2image para processar PDFs")
    
    # Conta quantas páginas precisam de vision
    vision_needed = sum(1 for p in pages if p.get("needs_vision"))
    logger.info(f"PDF: {len(pages)} páginas, {vision_needed} precisam de Vision AI")

    # Configura cliente Gemini apenas se necessário
    client = setup_gemini() if vision_needed > 0 else None

    # Processa cada página
    results = []
    pages_with_vision = 0
    pages_skipped_vision = 0

    for page in pages:
        page_num = page["page_num"]
        text = page["text"]

        if page.get("needs_vision") and page["image_bytes"]:
            if pages_with_vision >= MAX_VISION_PAGES:
                results.append(f"--- Página {page_num} (Vision AI ignorada - limite de {MAX_VISION_PAGES} atingido) ---\n{text}")
                pages_skipped_vision += 1
            else:
                logger.info(f"Vision AI: processando página {page_num}/{len(pages)}")
                vision_text = analyze_image_with_vision(client, page["image_bytes"], page_num)
                results.append(f"--- Página {page_num} (Vision AI) ---\n{vision_text}")
                pages_with_vision += 1
        else:
            results.append(f"--- Página {page_num} ---\n{text}")

    combined_text = "\n\n".join(results)

    logger.info(f"Concluído: {pages_with_vision} vision, {pages_skipped_vision} ignoradas")

    return {
        "success": True,
        "total_pages": len(pages),
        "pages_with_vision": pages_with_vision,
        "pages_skipped_vision": pages_skipped_vision,
        "text": combined_text,
        "minio_path": minio_path
    }


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
        # Validação de token
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid Authorization header"}), 401
        
        token = auth_header.replace("Bearer ", "")
        if token != API_TOKEN:
            return jsonify({"error": "Invalid token"}), 403
        
        data = request.json or {}
        
        try:
            # Obtém PDF
            if "url" in data:
                pdf_source = data["url"]
            elif "base64" in data:
                pdf_source = base64.b64decode(data["base64"])
            else:
                return jsonify({"error": "Forneça 'url' ou 'base64'"}), 400
            
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
                telefone=data.get("telefone"),
                minio_config=minio_config
            )
            
            return jsonify(result)
            
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
