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
# timeout 300s para PDFs grandes com múltiplas chamadas Vision AI
CMD ["gunicorn", "--bind", "0.0.0.0:5050", "--workers", "1", "--threads", "4", "--worker-class", "gthread", "--timeout", "300", "pdf_hybrid_extractor:create_app()"]
