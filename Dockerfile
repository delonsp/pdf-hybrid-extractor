FROM python:3.12-slim

WORKDIR /app

# Copia requirements primeiro (cache de layers)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o código
COPY pdf_hybrid_extractor.py .

# Porta
EXPOSE 5050

# Usa gunicorn em produção
# 1 worker + 4 threads (gthread) para I/O concorrente sem estourar memória
# timeout 480s: pior caso = ceil(MAX_VISION_PAGES / VISION_PARALLEL) × GEMINI_TIMEOUT
#   + overhead de render + download (~5×60s + buffer)
CMD ["gunicorn", "--bind", "0.0.0.0:5050", "--workers", "1", "--threads", "4", "--worker-class", "gthread", "--timeout", "480", "pdf_hybrid_extractor:create_app()"]
