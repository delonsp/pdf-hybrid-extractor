FROM python:3.12-slim

WORKDIR /app

# Copia requirements primeiro (cache de layers)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o código
COPY pdf_hybrid_extractor.py .

# Usuário não-root (defesa em camadas: limita blast radius de RCE / path traversal)
RUN useradd -r -u 1000 -m -d /home/appuser appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 5050

# Healthcheck pro orquestrador detectar worker morto / deadlock
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5050/health',timeout=3).status==200 else 1)" || exit 1

# Gunicorn em produção
# 1 worker + 4 threads (gthread) para I/O concorrente sem estourar memória.
#
# ATENÇÃO: com gthread, --timeout NÃO limita a duração de uma request. O worker
# chama notify() a cada iteração do loop independente das threads estarem presas,
# então o timeout só detecta worker silencioso/travado. Serve de backstop, nada
# mais. Quem limita request é o REQUEST_DEADLINE da aplicação (110s por padrão,
# derivado do timeout de 120s do chamador).
#
# --backlog 32: com o default (2048) o kernel aceitaria milhares de conexões que
# ficariam esperando muito além dos 120s que o chamador aguarda. Fila curta +
# 503 com Retry-After da aplicação é melhor que timeout lento e silencioso.
CMD ["gunicorn", "--bind", "0.0.0.0:5050", "--workers", "1", "--threads", "4", "--worker-class", "gthread", "--backlog", "32", "--timeout", "480", "pdf_hybrid_extractor:create_app()"]
