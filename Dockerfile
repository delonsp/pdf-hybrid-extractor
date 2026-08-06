# Imagem pinada por DIGEST, não só por tag: "3.12-slim" é tag móvel e muda de
# conteúdo sem aviso, então dois builds do mesmo commit podiam instalar coisas
# diferentes. Resolvido em 06/08/2026; para atualizar, buscar o digest novo.
FROM python:3.12-slim@sha256:646fb0bca3dd3ea1bcc6feb72c17ed16eed6e10cffc732fcc1478bd3e7f02d7b

WORKDIR /app

# requirements.txt é LOCK gerado por pip-compile (versões exatas + hashes,
# incluindo transitivas). Editar requirements.in e regerar:
#   pip-compile --generate-hashes --strip-extras -o requirements.txt requirements.in
# --require-hashes recusa qualquer pacote fora do lock: build reprodutível de
# verdade, e um pacote adulterado no espelho não passa despercebido.
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

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
# --threads vem de env pra ser ajustável no Dokploy sem rebuild. INVARIANTE:
# MAX_CONCURRENT_EXTRACTIONS tem que ser MENOR que isto — a folga é o que garante
# o /health respondendo e o 503 rápido. A aplicação valida e loga erro se furar.
# `exec` na forma shell: sem ele o gunicorn viraria filho do sh e não receberia
# os sinais de parada do Docker.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:5050 --workers 1 --threads ${GUNICORN_THREADS:-8} --worker-class gthread --backlog 32 --timeout 480 'pdf_hybrid_extractor:create_app()'"]
