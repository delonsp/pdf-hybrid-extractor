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
CMD ["gunicorn", "--bind", "0.0.0.0:5050", "--workers", "2", "--timeout", "120", "pdf_hybrid_extractor:create_app()"]
