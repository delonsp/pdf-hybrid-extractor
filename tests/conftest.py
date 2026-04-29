"""Fixtures e config compartilhada dos testes.

Setamos env vars ANTES de importar pdf_hybrid_extractor pra que:
- create_app() não levante por falta de PDF_EXTRACTOR_TOKEN
- Rate limit não dispare em testes que disparam várias requests
- setup_gemini() não falhe nos testes que chegam até lá (eles mockam o client)
"""
import os

os.environ.setdefault("PDF_EXTRACTOR_TOKEN", "test-token-for-pytest")
os.environ.setdefault("GOOGLE_API_KEY", "fake-key-for-tests")
os.environ.setdefault("RATE_LIMIT_DEFAULT", "10000 per minute")
os.environ.setdefault("RATE_LIMIT_EXTRACT", "10000 per minute")

import io
import fitz
import pytest

import pdf_hybrid_extractor as pdfx


@pytest.fixture
def auth_header():
    return {"Authorization": f"Bearer {os.environ['PDF_EXTRACTOR_TOKEN']}"}


@pytest.fixture
def app():
    """Flask app fresh por teste — limiter mantém estado in-memory por app."""
    return pdfx.create_app()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def make_pdf():
    """Factory pra PDFs sintéticos.

    pages = lista de dicts com chaves opcionais:
      - text: str a inserir na página
      - image_rect: tuple (x0, y0, x1, y1) pra inserir uma imagem 100x100 branca
    """
    def _make(pages):
        doc = fitz.open()
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 100))
        pix.clear_with(255)
        for pdef in pages:
            page = doc.new_page()
            if pdef.get("text"):
                page.insert_text((50, 50), pdef["text"])
            if pdef.get("image_rect"):
                page.insert_image(fitz.Rect(*pdef["image_rect"]), pixmap=pix)
        out = doc.tobytes()
        doc.close()
        return out
    return _make


@pytest.fixture
def docx_bytes():
    """DOCX mínimo válido (zip vazio com magic bytes corretos é suficiente
    pra _detect_type; pra extract_text_docx precisa estrutura interna)."""
    # Estrutura mínima de DOCX: zip com [Content_Types].xml + word/document.xml
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>'
        )
        zf.writestr("_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/>'
            '</Relationships>'
        )
        zf.writestr("word/document.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>Hello world from DOCX</w:t></w:r></w:p></w:body>'
            '</w:document>'
        )
    return buf.getvalue()


class FakeGeminiResponse:
    """Mock do response do client.models.generate_content."""
    def __init__(self, text):
        self.text = text


class FakeGeminiClient:
    """Mock do genai.Client. Permite configurar comportamento por modelo:
    - dict {model_name: callable | str | None | Exception}
    - callable recebe (model, contents) e retorna FakeGeminiResponse
    """
    def __init__(self, behavior=None):
        self._behavior = behavior or {}
        self.calls = []  # lista de (model_name, n_contents)

        outer = self
        class _Models:
            def generate_content(self, model, contents):
                outer.calls.append((model, len(contents)))
                spec = outer._behavior.get(model, "OK")
                if isinstance(spec, Exception):
                    raise spec
                if callable(spec):
                    return spec(model, contents)
                # str ou None viram texto direto
                return FakeGeminiResponse(spec)
        self.models = _Models()


@pytest.fixture
def fake_gemini_client():
    """Factory: chama com dict de behavior, retorna FakeGeminiClient.

    Behavior é dict {model_name: spec} onde spec pode ser:
      - str: texto a retornar (ou None/"" pra simular vazio)
      - Exception instance: levantada no generate_content
      - callable(model, contents) → FakeGeminiResponse
    """
    def _build(behavior=None):
        return FakeGeminiClient(behavior)
    return _build
