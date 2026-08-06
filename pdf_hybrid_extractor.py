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
import select
import socket
import zipfile
import argparse
import ipaddress
import logging
import threading
import requests
from pathlib import Path
from urllib.parse import urlparse, urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed

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
MIN_TEXT_THRESHOLD = int(os.getenv("MIN_TEXT_THRESHOLD", "50"))  # chars mínimos p/ evitar Vision
# Cobertura mínima de imagem (área de imagens / área da página) pra ativar modo
# híbrido (texto nativo + Vision). Cobre laudos com header textual + ultrassom
# embutido — antes essas páginas tinham >50 chars de texto e a imagem era
# ignorada silenciosamente.
IMAGE_COVERAGE_THRESHOLD = float(os.getenv("IMAGE_COVERAGE_THRESHOLD", "0.20"))
MAX_VISION_PAGES = int(os.getenv("MAX_VISION_PAGES", "15"))
VISION_PARALLEL = int(os.getenv("VISION_PARALLEL", "3"))
# Teto por chamada ao Gemini. Latência medida em produção: 15-19s por página no
# caminho feliz, então teto curto (25-30s) cortaria justamente as páginas densas
# — os laudos que interessam. Fica folgado; quem governa o total é o deadline.
GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "45"))
# Teto de saída por página. Transcrição de laudo denso + seção de análise é saída
# longa; se estourar, o modelo corta NO MEIO — e um valor laboratorial cortado é
# pior que valor ausente, porque parece completo. Explícito pra não depender do
# default do modelo, que muda quando o alias troca.
VISION_MAX_OUTPUT_TOKENS = int(os.getenv("VISION_MAX_OUTPUT_TOKENS", "8192"))
# Orçamento total da requisição. Derivado do TIMEOUT DO CHAMADOR (webhook desiste
# em 120s), não do gunicorn — que com gthread não limita request nenhuma. Passar
# disso é trabalho garantidamente descartado ocupando thread viva.
REQUEST_DEADLINE = int(os.getenv("REQUEST_DEADLINE", "110"))
# Cascata primário→fallback condicional ao orçamento restante. Incondicional, ela
# dobra o custo da página exatamente quando o tempo está mais apertado; se não
# cabe outra chamada, é melhor devolver a página como falha e preservar as outras.
FALLBACK_MIN_BUDGET = int(os.getenv("FALLBACK_MIN_BUDGET", "25"))
# Não começar página nova se restar menos que isto. DESLIGADO por padrão (0), e
# de propósito: a latência medida tem cauda pesada (p50 4,5s, p99 85s, máx 95s),
# e apertar isso transformaria em parcial documentos que hoje COMPLETAM — logo os
# mais lentos, que tendem a ser os mais densos. Trocar completude de laudo por
# disponibilidade de thread é o trade errado por default. Existe como alavanca
# caso a exaustão de threads apareça de verdade; o certo mesmo é fila assíncrona.
VISION_START_MIN_BUDGET = int(os.getenv("VISION_START_MIN_BUDGET", "0"))
# Admission control: menor que o nº de threads do gunicorn de propósito, pra
# sempre sobrar thread para o /health e para devolver 503 rápido. Enfileirar não
# ajuda — a espera sai do mesmo orçamento de 120s do chamador.
MAX_CONCURRENT_EXTRACTIONS = int(os.getenv("MAX_CONCURRENT_EXTRACTIONS", "3"))
RETRY_AFTER_SECONDS = int(os.getenv("RETRY_AFTER_SECONDS", "30"))
ABORT_ON_CLIENT_DISCONNECT = os.getenv("ABORT_ON_CLIENT_DISCONNECT", "true").lower() == "true"
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
# Allowlist opcional de hosts de origem, casada por SUFIXO — "backblazeb2.com"
# cobre f004/f005/qualquer cluster. Host cheio quebraria sozinho quando o
# provedor migrasse de bucket.
# Vazio = desligada (só o guard de IP privado vale). Ligar fecha redirect E DNS
# rebinding de uma vez, porque o host precisa ser conhecido em todo hop.
ALLOWED_DOWNLOAD_HOSTS = {
    h.strip().lower().rstrip(".")
    for h in os.getenv("ALLOWED_DOWNLOAD_HOSTS", "").split(",")
    if h.strip()
}
# Rollout em duas etapas, de propósito: com a lista preenchida mas ENFORCE=false,
# host fora da lista só gera WARNING e o download continua. Assim dá pra observar
# alguns dias de tráfego real antes de recusar — se aparecer um domínio novo, ele
# vira linha de log em vez de laudo perdido em silêncio.
# O default é NÃO recusar: preencher a lista sozinho nunca derruba produção.
ALLOWED_HOSTS_ENFORCE = os.getenv("ALLOWED_DOWNLOAD_HOSTS_ENFORCE", "false").lower() == "true"
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
# Prefixo do objeto no Minio: "telefone" (atual) ou "pseudonimo" (HMAC com sal).
# Default no atual de propósito — virar pseudônimo muda o layout do bucket e
# quebra busca por prefixo nos objetos já gravados. É decisão de migração.
MINIO_KEY_MODE = os.getenv("MINIO_KEY_MODE", "telefone").lower()
MINIO_KEY_SALT = os.getenv("MINIO_KEY_SALT", "")
# Retenção. O bucket é área de trabalho para interpretação, NÃO repositório de
# exames — decisão do Dr. Alain em 06/08/2026: apagar após 2 meses.
# Vira regra de lifecycle no bucket; quem apaga é o próprio Minio.
# ATENÇÃO: a regra vale para o bucket inteiro, inclusive objetos JÁ existentes.
# Ao ligar, tudo que já passou do prazo é apagado na primeira varredura, e isso
# não tem volta. 0 = desligado (acumula para sempre).
MINIO_RETENTION_DAYS = int(os.getenv("MINIO_RETENTION_DAYS", "60"))
MAX_COMPRESSION_RATIO = float(os.getenv("MAX_COMPRESSION_RATIO", "200"))
ALLOWED_DOWNLOAD_TYPES = {
    "application/pdf",
    "application/octet-stream",
    "binary/octet-stream",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "application/zip",  # alguns servers servem .docx assim
}
# Análise de imagem de exame: LIGADA por padrão. O defeito antigo não era ela
# existir — era sair misturada com a transcrição, sem nada dizendo qual é qual.
# Separada e rotulada, preserva informação que a transcrição literal perderia
# (uma página que é só foto de ultrassom transcreve quase nada).
VISION_ANALYZE_IMAGES = os.getenv("VISION_ANALYZE_IMAGES", "true").lower() == "true"
VISION_ANALYSIS_MARKER = "===ANALISE_DA_IMAGEM==="

# Duas seções com finalidades diferentes, e a fronteira entre elas é o ponto todo:
#   TRANSCRIÇÃO = cópia fiel do que o documento diz. Nada entra aqui que não esteja
#   escrito — não por segurança, mas por fidelidade: é o registro do documento.
#   ANÁLISE = leitura do modelo sobre a imagem. Pode levantar hipótese diagnóstica,
#   desde que como hipótese; o que não pode é FECHAR diagnóstico.
# Antes as duas saíam concatenadas sob o mesmo marcador, direto pro prontuário,
# sem nada distinguindo o que foi lido do que foi inferido.
VISION_PROMPT_TRANSCRICAO = """Transcreva LITERALMENTE todo o texto visível nesta imagem de documento médico.

Regras obrigatórias:
- Transcreva apenas o que está ESCRITO. Não interprete, não resuma, não complete.
- Não acrescente nesta seção nenhuma leitura sua: ela é a cópia fiel do documento.
- Preserve os valores EXATAMENTE como aparecem: números, unidades, separadores decimais e valores de referência.
- Preserve a estrutura: cabeçalhos, tabelas linha a linha, rodapés, assinaturas e datas.
- Trecho ilegível, cortado ou borrado: escreva [ilegível] no lugar. NUNCA adivinhe valor, unidade, data ou nome.
- Se houver imagem de exame (ultrassom, raio-x, tomografia), escreva apenas [imagem de exame] na posição onde ela aparece.

Se não houver nenhum texto legível, responda exatamente: [sem texto legível]"""

VISION_PROMPT_ANALISE = f"""

Depois da transcrição, acrescente SEMPRE uma seção iniciada por esta linha exata:
{VISION_ANALYSIS_MARKER}

Se não houver imagem de exame nesta página, escreva nela apenas: sem imagem de exame

Essa seção é análise sua, não transcrição. Havendo imagem, organize em duas partes:
- "Visível:" o que aparece objetivamente na imagem (tipo de exame, região, estruturas, marcações, medidas legíveis).
- "Hipóteses:" possibilidades diagnósticas que os achados sugerem.

Nas hipóteses: levante possibilidades, mas NÃO feche diagnóstico. Use formulação não
conclusiva ("achados podem ser compatíveis com...", "considerar..."), mantenha os
diferenciais em aberto e não afirme certeza. Se os achados não permitirem hipótese
com segurança, escreva "Hipóteses: não é possível levantar com segurança"."""


def _build_vision_prompt() -> str:
    """Monta o prompt na hora da chamada — permite trocar por env sem redeploy."""
    if VISION_ANALYZE_IMAGES:
        return VISION_PROMPT_TRANSCRICAO + VISION_PROMPT_ANALISE
    return VISION_PROMPT_TRANSCRICAO


def _split_vision_output(raw: str) -> tuple[str, str | None, bool]:
    """Separa transcrição de análise da imagem.

    Devolve (transcrição, análise|None, separação_falhou). Se a análise está
    ligada mas o modelo não emitiu o marcador, NÃO dá pra saber onde uma termina
    e a outra começa. Nesse caso o texto vai inteiro como transcrição e a página
    é SINALIZADA — fingir que a separação aconteceu seria pior que admitir que
    não aconteceu, porque é exatamente aí que hipótese entraria no prontuário
    passando por transcrição."""
    if VISION_ANALYSIS_MARKER in raw:
        transcricao, _, analise = raw.partition(VISION_ANALYSIS_MARKER)
        analise = analise.strip()
        # A seção é sempre pedida; "sem imagem de exame" é a resposta esperada
        # em página de texto puro e não vira campo de análise.
        if analise.lower().startswith("sem imagem de exame"):
            analise = ""
        return transcricao.strip(), analise or None, False
    # Sem marcador: só é problema quando a análise foi pedida.
    return raw.strip(), None, VISION_ANALYZE_IMAGES


def setup_gemini():
    """Configura cliente do Gemini (nova SDK) com timeout"""
    if not GOOGLE_API_KEY:
        raise ValueError("GOOGLE_API_KEY ou GEMINI_API_KEY não configurada")
    return genai.Client(
        api_key=GOOGLE_API_KEY,
        http_options={"timeout": GEMINI_TIMEOUT * 1000}  # ms
    )


def _host_allowed(host: str) -> bool:
    """Host bate na allowlist, por SUFIXO (match exato ou subdomínio).

    Sufixo e não host cheio de propósito: "backblazeb2.com" cobre f004, f005 e
    qualquer cluster futuro do provedor. Fixar o host completo quebraria sozinho
    numa migração de bucket, sem ninguém ter mexido em nada.
    Allowlist vazia = tudo liberado (só o guard de IP privado vale)."""
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
    # Registra o host de TODA origem usada — é isso que acumula a lista real
    # antes de fechar a allowlist. Loga o HOST, nunca a URL inteira: o path
    # costuma carregar token assinado e identificador do arquivo do paciente.
    logger.info(f"[origem] download de {host}")
    if not _host_allowed(host):
        # Modo observação (default): registra e deixa passar, pra descobrir os
        # domínios reais sem arriscar perder laudo. Só recusa com ENFORCE=true.
        if not ALLOWED_HOSTS_ENFORCE:
            logger.warning(
                f"[allowlist:observação] host fora da lista: {host} — permitido. "
                f"Setar ALLOWED_DOWNLOAD_HOSTS_ENFORCE=true para passar a recusar."
            )
        else:
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


def _inspect_response(response) -> tuple[str, str | None]:
    """Extrai (texto, motivo) de uma resposta do Gemini.

    Resposta vazia era sempre logada como "provável safety filter" — mas vazio
    tem causas diferentes, com tratamentos diferentes: safety (não adianta
    repetir), MAX_TOKENS (a saída não coube e provavelmente está CORTADA),
    recitation, ou candidato sem parte textual. Tratar tudo igual escondia o
    caso mais perigoso: texto truncado no meio de um valor laboratorial, que
    volta parecendo completo.

    Tolerante a resposta sem os campos — o formato varia entre versões da SDK e
    entre modelos, e diagnóstico não pode derrubar extração."""
    try:
        text = response.text or ""
    except Exception:
        text = ""

    finish = None
    try:
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            raw_finish = getattr(candidates[0], "finish_reason", None)
            if raw_finish is not None:
                finish = getattr(raw_finish, "name", None) or str(raw_finish)
                finish = finish.upper()
    except Exception:
        finish = None

    block = None
    try:
        feedback = getattr(response, "prompt_feedback", None)
        raw_block = getattr(feedback, "block_reason", None) if feedback else None
        if raw_block is not None:
            block = getattr(raw_block, "name", None) or str(raw_block)
    except Exception:
        block = None

    if block:
        return text, f"prompt_bloqueado:{block}"
    if finish and "MAX_TOKENS" in finish:
        # Com texto: veio cortado (pior que vazio — parece completo).
        # Sem texto: o raciocínio do modelo consumiu todo o orçamento de saída.
        return text, "truncado_max_tokens"
    if not text:
        if finish and finish not in ("STOP", "FINISH_REASON_UNSPECIFIED"):
            return "", f"vazio:{finish.lower()}"
        return "", "vazio:sem_motivo_declarado"
    return text, None


def _remaining(deadline: float | None) -> float:
    """Segundos restantes do orçamento da requisição (infinito se não há prazo)."""
    if deadline is None:
        return math.inf
    return deadline - time.monotonic()


def analyze_image_with_vision(client, image_bytes: bytes, page_num: int,
                              deadline: float | None = None,
                              diagnostics: dict | None = None) -> str | None:
    """Envia imagem para Gemini Vision com cascata primário→fallback CONDICIONAL
    ao orçamento restante.

    Tenta VISION_MODEL primeiro; em falha (exceção, vazio ou safety filter),
    tenta VISION_MODEL_FALLBACK **apenas se ainda sobrar FALLBACK_MIN_BUDGET**.
    Incondicional, a cascata dobrava o custo da página justamente quando o tempo
    estava mais apertado, roubando o prazo das páginas seguintes.

    O timeout de cada chamada também é apertado para o que resta do orçamento —
    nenhuma chamada individual pode sobreviver ao prazo da requisição.

    `diagnostics` (dict opcional) recebe, por página, o motivo de vazio ou
    truncamento — safety, MAX_TOKENS e recitation pedem tratamentos diferentes e
    antes eram todos logados como "provável safety filter"."""
    from google.genai import types
    image_part = types.Part.from_bytes(data=image_bytes, mime_type="image/png")

    models_to_try = [VISION_MODEL]
    if VISION_MODEL_FALLBACK and VISION_MODEL_FALLBACK != VISION_MODEL:
        models_to_try.append(VISION_MODEL_FALLBACK)

    for idx, model_name in enumerate(models_to_try):
        is_last = idx == len(models_to_try) - 1
        left = _remaining(deadline)
        if idx > 0 and left < FALLBACK_MIN_BUDGET:
            logger.warning(
                f"Página {page_num}: pulando fallback — só restam {left:.0f}s "
                f"do orçamento (mínimo {FALLBACK_MIN_BUDGET}s)"
            )
            return None
        if left <= 0:
            logger.warning(f"Página {page_num}: orçamento esgotado antes de chamar {model_name}")
            return None
        try:
            call_timeout = int(min(GEMINI_TIMEOUT, left) * 1000)  # ms
            response = client.models.generate_content(
                model=model_name,
                contents=[_build_vision_prompt(), image_part],
                config=types.GenerateContentConfig(
                    http_options=types.HttpOptions(timeout=call_timeout),
                    max_output_tokens=VISION_MAX_OUTPUT_TOKENS,
                ),
            )
            text, motivo = _inspect_response(response)
            if text:
                if idx > 0:
                    logger.info(f"Página {page_num}: fallback {model_name} ok")
                if diagnostics is not None and motivo != "truncado_max_tokens":
                    # A tentativa anterior pode ter deixado um motivo registrado.
                    # Se o fallback deu certo, a página NÃO é problemática — sem
                    # isso ela apareceria em vision_diagnostics tendo funcionado.
                    diagnostics.pop(page_num, None)
                if motivo == "truncado_max_tokens":
                    # Texto cortado no meio é pior que texto ausente: volta
                    # parecendo completo, e o corte pode ter comido um valor.
                    logger.warning(
                        f"Página {page_num}: {model_name} truncou a saída em "
                        f"{VISION_MAX_OUTPUT_TOKENS} tokens — texto pode estar cortado"
                    )
                    if diagnostics is not None:
                        diagnostics[page_num] = "truncado_max_tokens"
                return text
            if diagnostics is not None:
                diagnostics[page_num] = motivo or "vazio"
            msg = f"Página {page_num}: {model_name} devolveu vazio ({motivo})"
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


# Lock global do PyMuPDF. NÃO é por requisição — é do processo inteiro.
#
# A documentação do PyMuPDF diz que multiprocessing é suportado e multithreading
# NÃO é. O modo de falha não é exceção (que cairia em failed_pages): é pixmap
# corrompido — imagem lixo que o Gemini "lê" e vira texto inventado no prontuário
# — ou queda do interpretador, que com 1 worker leva junto as 4 requisições em voo.
#
# O antigo doc_lock era criado dentro de process_pdf, então valia só dentro de uma
# requisição; as 4 threads do gunicorn usavam o MuPDF simultaneamente com locks
# que não se enxergavam. Este lock serializa TODA chamada ao PyMuPDF do processo.
#
# Custo: render leva 50-200ms e a chamada ao Gemini 4-30s. Serializar o render
# entre 4 threads adiciona menos de 1s no pior caso, contra p50 de 4,5s por
# documento — ruído. Era risco de corrupção em troca de milissegundos.
#
# Regra ao mexer aqui: NENHUM objeto do PyMuPDF (Document, Page, Pixmap) pode ser
# tocado fora deste lock. As funções abaixo entram, fazem a operação inteira e
# devolvem dado puro (bytes, str, float).
_pymupdf_lock = threading.RLock()


def _open_pdf(pdf_bytes: bytes):
    """Abre o documento. PDFs inválidos/criptografados viram ValueError → 400."""
    with _pymupdf_lock:
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        except Exception as e:
            raise ValueError(f"PDF inválido ou corrompido: {e}") from e
        if doc.is_encrypted and not doc.authenticate(""):
            doc.close()
            raise ValueError("PDF está criptografado/protegido por senha")
        if doc.page_count > MAX_TOTAL_PAGES:
            page_count = doc.page_count
            doc.close()
            raise ValueError(f"PDF com páginas demais: {page_count} > cap {MAX_TOTAL_PAGES}")
        return doc


def _close_pdf(doc) -> None:
    with _pymupdf_lock:
        doc.close()


def _classify_page(doc, index: int) -> tuple[str, float, bool]:
    """Texto, cobertura de imagem e flag de falha de UMA página.

    Lock por página, não pela passada inteira: um PDF de 500 páginas bloquearia
    as outras requisições por segundos se o lock fosse pego uma vez só."""
    with _pymupdf_lock:
        page = doc[index]
        try:
            text = page.get_text().strip()
            failed = False
        except Exception:
            logger.exception(f"Página {index + 1}: falha ao extrair texto nativo")
            text = ""
            failed = True
        coverage = _page_image_coverage(page)
    return text, coverage, failed


def _render_page_png(doc, index: int) -> bytes:
    """Rasteriza a página em PNG respeitando MAX_RENDER_PIXELS.

    Uma página com MediaBox gigante faria o get_pixmap alocar centenas de MB a GB
    — o cap de bytes do download não protege disso, porque o PDF em si pode ter
    poucos KB. Aqui o zoom é reduzido até caber no teto; se para caber ele tiver
    que ficar abaixo de MIN_RENDER_ZOOM, a página é recusada como falha isolada
    (render ilegível não vale a chamada ao Gemini) em vez de derrubar o processo.

    Roda sob _pymupdf_lock e devolve bytes — nenhum objeto do PyMuPDF escapa."""
    with _pymupdf_lock:
        page = doc[index]
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
                f"Página {index + 1}: {width:.0f}x{height:.0f}pt excede "
                f"MAX_RENDER_PIXELS, zoom reduzido para {zoom:.2f}"
            )

        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        return pix.tobytes("png")


def process_pdf(pdf_source: str | bytes, save_to_minio: bool = False,
                telefone: str = None, minio_config: dict = None,
                deadline: float = None, is_cancelled=None) -> dict:
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

    `deadline` é o instante (time.monotonic) em que a requisição tem que acabar,
    derivado do timeout do chamador. Ao estourar, as páginas restantes voltam com
    o texto nativo e a resposta vem marcada como parcial — nunca `success: true`
    mudo. `is_cancelled` é um callable que indica que o chamador desistiu; a
    extração para, porque seguir gastando thread e cota do Gemini por resultado
    que ninguém vai ler é a pior ocupação possível.
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

    # Abertura defensiva: PDFs inválidos/criptografados viram ValueError → 400
    # com mensagem útil em vez do "internal error" genérico do handler de 500.
    doc = _open_pdf(pdf_bytes)
    page_count = doc.page_count

    # Minio DEPOIS da validação: antes, um arquivo corrompido era gravado e só
    # então o open falhava, deixando lixo no bucket. Continua sendo salvo mesmo
    # se a extração falhar adiante — aí o original é justamente o que se quer ter.
    minio_path = None
    minio_error = None
    minio_requested = bool(save_to_minio and telefone and minio_config)
    if minio_requested:
        minio_path, minio_error = save_to_minio_storage(pdf_bytes, telefone, minio_config)

    try:
        # Passada 1: classifica cada página em native | vision | hybrid (sem renderizar)
        page_metas = []
        classify_failed = []
        for i in range(page_count):
            # get_text() também estoura em página corrompida (JBIG2/JPX quebrado,
            # xref inconsistente). Sem esta guarda, a passada 1 inteira morria e
            # o documento todo virava 500 — inclusive as páginas boas.
            text, img_coverage, failed = _classify_page(doc, i)
            if failed:
                classify_failed.append(i + 1)
            has_text = len(text) >= MIN_TEXT_THRESHOLD
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

        # Passada 2: RENDER NO FLUXO PRINCIPAL, só o HTTP do Gemini em paralelo.
        #
        # Antes o render rodava dentro do ThreadPoolExecutor — PyMuPDF em threads,
        # o que a doc dele desaconselha e falha em silêncio (pixmap corrompido ou
        # queda do interpretador). O paralelismo ali não comprava nada: render é
        # 50-200ms contra 4-30s da chamada ao Gemini. Agora o pool só carrega a
        # espera de rede, que é o que de fato se beneficia.
        #
        # Pipeline contínuo (não em ondas): com p99 de 85s, esperar cada onda
        # fechar faria uma página lenta segurar duas rápidas. O semáforo limita
        # quantas imagens ficam vivas ao mesmo tempo, dando contrapressão sem
        # travar quem já terminou.
        vision_results: dict[int, str | None] = {}
        aborted_pages: set[int] = set()   # prazo estourado ou chamador sumiu
        vision_diagnostics: dict[int, str] = {}
        cancelled = False
        if to_vision_capped:
            client = setup_gemini()
            images_inflight = threading.Semaphore(VISION_PARALLEL + 1)
            futures = {}

            with ThreadPoolExecutor(max_workers=VISION_PARALLEL) as ex:
                for meta in to_vision_capped:
                    pn = meta["page_num"]
                    # Checado no fluxo principal, antes de gastar render ou cota:
                    # páginas que só começariam fora do prazo desistem de graça.
                    if not cancelled and is_cancelled is not None and is_cancelled():
                        cancelled = True
                    if cancelled or _remaining(deadline) <= max(VISION_START_MIN_BUDGET, 0):
                        aborted_pages.add(pn)
                        vision_results[pn] = None
                        continue

                    images_inflight.acquire()
                    try:
                        img_bytes = _render_page_png(doc, pn - 1)
                    except Exception:
                        logger.exception(f"Página {pn}: falha ao rasterizar")
                        images_inflight.release()
                        vision_results[pn] = None
                        continue

                    fut = ex.submit(analyze_image_with_vision, client, img_bytes, pn,
                                    deadline=deadline, diagnostics=vision_diagnostics)
                    # Libera a vaga assim que a imagem sai de cena — inclusive em
                    # exceção, senão o semáforo seca e o loop trava pra sempre.
                    fut.add_done_callback(lambda _f: images_inflight.release())
                    futures[fut] = pn

                for fut in as_completed(futures):
                    pn = futures[fut]
                    try:
                        vision_results[pn] = fut.result()
                    except Exception:
                        logger.exception(f"Página {pn}: falha inesperada no Vision")
                        vision_results[pn] = None

            if cancelled:
                logger.warning(
                    f"Chamador desistiu — extração interrompida com "
                    f"{len(aborted_pages)} página(s) pendente(s)"
                )
            elif aborted_pages:
                logger.warning(
                    f"Prazo de {REQUEST_DEADLINE}s esgotado — "
                    f"{len(aborted_pages)} página(s) sem Vision"
                )

        # Monta o output
        results = []
        pages_with_vision = 0
        pages_hybrid = 0
        pages_skipped_vision = 0
        failed_pages = []

        # Análise da imagem vai em campo PRÓPRIO, nunca concatenada no texto: é
        # leitura do modelo (inclusive hipótese diagnóstica), não transcrição, e
        # no prontuário as duas coisas não podem ficar indistinguíveis.
        image_analysis: dict[int, str] = {}
        analysis_unseparated: list[int] = []

        for meta in page_metas:
            pn = meta["page_num"]
            native = meta["text"]
            mode = meta["mode"]

            if pn in aborted_pages:
                # Prazo estourado / chamador desistiu: não é falha do Vision,
                # é orçamento. Marcador próprio pra não confundir diagnóstico.
                motivo = "chamador desistiu" if cancelled else f"prazo de {REQUEST_DEADLINE}s esgotado"
                results.append(f"--- Página {pn} ({motivo} - usando texto nativo) ---\n{native}")
            elif pn in vision_results:
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
                    transcricao, analise, nao_separou = _split_vision_output(vtext)
                    if analise:
                        image_analysis[pn] = analise
                    if nao_separou:
                        # O modelo não emitiu o marcador: não dá pra garantir que
                        # o texto abaixo é só transcrição. Fica sinalizado em vez
                        # de passar por transcrição sem ser.
                        analysis_unseparated.append(pn)
                        logger.warning(
                            f"Página {pn}: análise da imagem não veio separada — "
                            f"o texto pode conter leitura do modelo"
                        )
                    if mode == "hybrid":
                        pages_hybrid += 1
                        results.append(
                            f"--- Página {pn} (texto + transcrição da imagem) ---\n{native}\n\n"
                            f"[Transcrição da imagem]:\n{transcricao}"
                        )
                    else:
                        results.append(
                            f"--- Página {pn} (transcrição via Vision AI) ---\n{transcricao}"
                        )
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

        # `complete` é a chave honesta: nada foi cortado por cap, prazo,
        # desistência do chamador ou truncamento de texto. Antes a resposta
        # vinha `success: true` mesmo com páginas descartadas, e um prontuário
        # incompleto era gravado sem ninguém perceber.
        # Truncamento por MAX_TOKENS conta como incompleto: o texto foi cortado
        # no meio e volta parecendo inteiro. Mesma razão do resto desta lista.
        output_truncated = [
            pn for pn, m in vision_diagnostics.items() if m == "truncado_max_tokens"
        ]
        complete = not (
            pages_skipped_vision or aborted_pages or text_truncated
            or failed_pages or output_truncated or analysis_unseparated
        )

        return {
            "success": True,
            "complete": complete,
            "total_pages": len(page_metas),
            "pages_with_vision": pages_with_vision,
            "pages_hybrid": pages_hybrid,
            "pages_skipped_vision": pages_skipped_vision,
            "pages_deadline_skipped": sorted(aborted_pages),
            "deadline_exceeded": bool(aborted_pages) and not cancelled,
            "caller_gone": cancelled,
            "failed_pages": failed_pages,
            "text_truncated": text_truncated,
            "text": combined_text,
            # Geração do modelo, separada da transcrição de propósito. Quem
            # persistir isso no prontuário tem que manter a distinção.
            "image_analysis": {str(k): v for k, v in sorted(image_analysis.items())},
            "analysis_unseparated": sorted(analysis_unseparated),
            # Motivo por página de vazio/truncamento. Safety, MAX_TOKENS e
            # recitation pedem reações diferentes; antes eram todos "vazio".
            "vision_diagnostics": {str(k): v for k, v in sorted(vision_diagnostics.items())},
            "pages_output_truncated": sorted(output_truncated),
            "minio_path": minio_path,
            # Explícito: quem pediu persistência precisa saber se ela aconteceu.
            # Antes a falha voltava como success:true + minio_path:null.
            "minio_stored": bool(minio_path) if minio_requested else None,
            "minio_error": minio_error,
        }
    finally:
        _close_pdf(doc)


def _minio_prefix(telefone: str) -> str:
    """Prefixo do objeto: telefone cru ou pseudônimo estável.

    O telefone na chave expõe identificador pessoal em listagem de bucket, em
    métrica e no `minio_path` que volta pro chamador — mascarar o log não
    resolvia isso. O pseudônimo é HMAC-SHA256 com sal secreto: estável (o mesmo
    paciente cai sempre no mesmo prefixo, então continua dando pra agrupar) e
    não reversível. Sem sal, HMAC de telefone é força-bruta trivial: o espaço de
    telefones é pequeno demais.

    Default é `telefone` de propósito — virar pseudônimo muda o layout do bucket
    e quebra qualquer busca por prefixo de telefone nos objetos JÁ gravados.
    Isso é decisão de migração, não de código."""
    if MINIO_KEY_MODE != "pseudonimo":
        return telefone
    if not MINIO_KEY_SALT:
        raise ValueError(
            "MINIO_KEY_MODE=pseudonimo exige MINIO_KEY_SALT "
            "(sem sal, o HMAC de um telefone é quebrado por força bruta)"
        )
    import hashlib
    digest = hmac.new(MINIO_KEY_SALT.encode(), telefone.encode(), hashlib.sha256)
    return digest.hexdigest()[:32]


# Aplicar a regra de retenção uma vez por processo, não a cada request.
# Se falhar, NÃO marca como feito — senão a retenção sumiria em silêncio até o
# próximo restart, que é o tipo de falha que ninguém percebe.
_retention_applied: set[str] = set()
_retention_lock = threading.Lock()
RETENTION_RULE_ID = "pdf-extractor-retencao"


def _ensure_retention(client, bucket: str) -> None:
    """Garante a regra de expiração no bucket. Nunca derruba o upload.

    Quem apaga é o Minio, não este serviço — a regra é declarativa e vale para
    o bucket inteiro, incluindo o que já estava lá."""
    if MINIO_RETENTION_DAYS <= 0:
        return
    with _retention_lock:
        if bucket in _retention_applied:
            return
    try:
        from minio.lifecycleconfig import LifecycleConfig, Rule, Expiration
        from minio.commonconfig import ENABLED, Filter

        existente = None
        try:
            existente = client.get_bucket_lifecycle(bucket)
        except Exception:
            existente = None  # bucket sem lifecycle ainda

        ja_correta = False
        if existente is not None:
            for r in getattr(existente, "rules", []) or []:
                exp = getattr(r, "expiration", None)
                if (getattr(r, "rule_id", None) == RETENTION_RULE_ID
                        and exp is not None
                        and getattr(exp, "days", None) == MINIO_RETENTION_DAYS):
                    ja_correta = True
                    break

        if not ja_correta:
            client.set_bucket_lifecycle(bucket, LifecycleConfig([
                Rule(
                    ENABLED,
                    rule_id=RETENTION_RULE_ID,
                    rule_filter=Filter(prefix=""),
                    expiration=Expiration(days=MINIO_RETENTION_DAYS),
                ),
            ]))
            logger.info(
                f"Retenção aplicada em '{bucket}': objetos expiram em "
                f"{MINIO_RETENTION_DAYS} dias"
            )
        with _retention_lock:
            _retention_applied.add(bucket)
    except Exception:
        # Não marca como aplicada: tenta de novo na próxima request, em vez de
        # deixar o bucket acumulando sem ninguém saber.
        logger.exception(
            f"Falha ao aplicar retenção em '{bucket}' — o bucket pode estar "
            f"acumulando sem prazo"
        )


def save_to_minio_storage(pdf_bytes: bytes, telefone: str, config: dict) -> tuple[str | None, str | None]:
    """Salva PDF no Minio. Devolve (caminho, erro) — nunca engole a falha.

    Antes retornava None em erro e o endpoint respondia `success: true` com
    `minio_path: null`: quem pediu persistência não tinha como saber que ela não
    aconteceu. Agora o motivo sobe para o chamador.

    Object name usa timestamp UTC + sufixo UUID curto (evita colisão quando duas
    requests do mesmo paciente caem no mesmo segundo)."""
    try:
        import uuid
        from minio import Minio
        from minio.error import S3Error
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
        object_name = f"{_minio_prefix(telefone)}/{timestamp}_{suffix}.pdf"

        if not client.bucket_exists(bucket):
            try:
                client.make_bucket(bucket)
            except S3Error as e:
                # Corrida: duas requests viram o bucket faltando ao mesmo tempo
                # e ambas tentaram criar. Perder essa corrida não é erro.
                if e.code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                    raise

        _ensure_retention(client, bucket)

        client.put_object(
            bucket,
            object_name,
            io.BytesIO(pdf_bytes),
            len(pdf_bytes),
            content_type="application/pdf"
        )

        return f"{bucket}/{object_name}", None
    except Exception as e:
        # Log sem o caminho (que pode conter telefone) — só o tipo do erro.
        logger.exception("Erro ao salvar no Minio")
        return None, f"{type(e).__name__}: {e}"


# === Modo Servidor Flask ===

# Contador de extrações em voo. Deliberadamente MENOR que o nº de threads do
# gunicorn: sempre sobra thread pro /health responder e pra devolver 503 rápido.
_extract_slots = threading.BoundedSemaphore(MAX_CONCURRENT_EXTRACTIONS)


def _make_cancel_check():
    """Callable que diz se o chamador foi embora, ou None se não dá pra saber.

    O webhook desiste em 120s, mas a thread daqui continuava trabalhando — cota
    do Gemini e thread gastas por resultado que ninguém vai ler. É preciso pegar
    o socket AQUI (dentro do contexto de request); as threads do executor não
    têm acesso a `request`.

    Conservador de propósito: só reporta desistência em EOF certo. Qualquer
    incerteza devolve False — abortar trabalho válido por engano seria pior."""
    if not ABORT_ON_CLIENT_DISCONNECT:
        return None
    sock = request.environ.get("gunicorn.socket")
    if sock is None:  # dev server / test client não expõem socket
        return None

    def _cancelled():
        try:
            readable, _, _ = select.select([sock], [], [], 0)
            if not readable:
                return False
            # Legível e sem dados = peer fechou. Com dados seria pipelining.
            return sock.recv(1, socket.MSG_PEEK) == b""
        except Exception:
            return False

    return _cancelled

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

        # Admission control: com tudo ocupado, recusar AGORA. Enfileirar não
        # ajuda — a espera sairia do mesmo orçamento de 120s do chamador, então
        # a fila só converteria rejeição rápida em timeout lento e silencioso.
        if not _extract_slots.acquire(blocking=False):
            logger.warning(
                f"Capacidade cheia ({MAX_CONCURRENT_EXTRACTIONS} extrações em voo) — 503"
            )
            resp = jsonify({
                "error": "servidor ocupado, tente novamente",
                "success": False,
            })
            return resp, 503, {"Retry-After": str(RETRY_AFTER_SECONDS)}

        # Tudo daqui pra baixo dentro do try/finally: qualquer exceção entre o
        # acquire e o finally vazaria o slot PARA SEMPRE, e o serviço se
        # estrangularia sozinho sem nunca voltar.
        try:
            deadline = time.monotonic() + REQUEST_DEADLINE
            cancel_check = _make_cancel_check()

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
                    "complete": True,
                    "type": "docx",
                    "total_pages": 1,
                    "pages_with_vision": 0,
                    "pages_hybrid": 0,
                    "pages_skipped_vision": 0,
                    "pages_deadline_skipped": [],
                    "deadline_exceeded": False,
                    "caller_gone": False,
                    "failed_pages": [],
                    "text_truncated": False,
                    "text": extract_text_docx(pdf_source),
                    "image_analysis": {},
                    "analysis_unseparated": [],
                    "vision_diagnostics": {},
                    "pages_output_truncated": [],
                    "minio_stored": None,
                    "minio_error": None,
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
                minio_config=minio_config,
                deadline=deadline,
                is_cancelled=cancel_check,
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
        finally:
            _extract_slots.release()
    
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
