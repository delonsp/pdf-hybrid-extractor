"""Limites de recurso (Lote A do PRD).

Cada teste aqui cobre um jeito de derrubar o worker único que os caps antigos
NÃO pegavam: o cap de 50 MB só valia pro download por URL, e nem ele protege
contra rasterização gigante ou expansão de ZIP.
"""
import io
import zipfile

import fitz
import pytest

import pdf_hybrid_extractor as pdfx


PRIMARY = pdfx.VISION_MODEL


class TestRenderPixelCap:
    """Um PDF de poucos KB com MediaBox gigante fazia o get_pixmap alocar GB."""

    def test_huge_page_gets_zoom_reduced(self, monkeypatch):
        doc = fitz.open()
        doc.new_page(width=2000, height=2000)  # 16 MP no zoom padrão (2x)
        monkeypatch.setattr(pdfx, "MAX_RENDER_PIXELS", 4_000_000)
        try:
            png = pdfx._render_page_png(doc, 0)
            # zoom cai de 2.0 para 1.0 → 2000x2000 px, dentro do teto
            pix = fitz.Pixmap(png)
            assert (pix.width, pix.height) == (2000, 2000)
            assert pix.width * pix.height <= pdfx.MAX_RENDER_PIXELS
        finally:
            doc.close()
        assert png.startswith(b"\x89PNG")

    def test_page_refused_when_zoom_would_be_illegible(self, monkeypatch):
        """Cap muito baixo levaria a um render borrado — gasto de Gemini sem
        retorno. Melhor falhar a página."""
        doc = fitz.open()
        doc.new_page(width=5000, height=5000)
        monkeypatch.setattr(pdfx, "MAX_RENDER_PIXELS", 10_000)  # zoom ~0.02
        with pytest.raises(ValueError, match="legível"):
            try:
                pdfx._render_page_png(doc, 0)
            finally:
                doc.close()

    def test_normal_page_uses_configured_zoom(self):
        doc = fitz.open()
        doc.new_page(width=595, height=842)  # A4
        png = pdfx._render_page_png(doc, 0)
        doc.close()
        assert png.startswith(b"\x89PNG")


class TestPageCountCap:
    def test_pdf_with_too_many_pages_is_refused(self, monkeypatch):
        doc = fitz.open()
        for _ in range(5):
            doc.new_page()
        pdf = doc.tobytes()
        doc.close()
        monkeypatch.setattr(pdfx, "MAX_TOTAL_PAGES", 3)
        with pytest.raises(ValueError, match="páginas demais"):
            pdfx.process_pdf(pdf)


class TestPerPageFailureIsolation:
    """Falha numa página não pode derrubar o documento inteiro."""

    def test_render_exception_becomes_failed_page(self, make_pdf, mocker, fake_gemini_client):
        pdf = make_pdf([
            {"text": "P1 " * 40},
            {"text": "X", "image_rect": (50, 100, 550, 700)},
        ])
        mocker.patch("pdf_hybrid_extractor.setup_gemini",
                     return_value=fake_gemini_client({PRIMARY: "visto"}))
        mocker.patch("pdf_hybrid_extractor._render_page_png",
                     side_effect=RuntimeError("JBIG2 quebrado"))
        result = pdfx.process_pdf(pdf)
        # Antes: ex.map re-levantava e o request virava 500, perdendo a página 1
        assert result["success"] is True
        assert result["failed_pages"] == [2]
        assert "P1" in result["text"]

    def test_get_text_exception_becomes_failed_page(self, make_pdf, mocker, fake_gemini_client):
        pdf = make_pdf([{"text": "qualquer coisa " * 10}])
        mocker.patch("pdf_hybrid_extractor.setup_gemini",
                     return_value=fake_gemini_client({PRIMARY: "visto"}))
        mocker.patch.object(fitz.Page, "get_text", side_effect=RuntimeError("xref ruim"))
        result = pdfx.process_pdf(pdf)
        assert result["success"] is True
        assert 1 in result["failed_pages"]


class TestOutputCap:
    def test_text_is_truncated_with_flag(self, make_pdf, monkeypatch):
        pdf = make_pdf([{"text": "Lorem ipsum " * 40}])
        monkeypatch.setattr(pdfx, "MAX_OUTPUT_CHARS", 50)
        result = pdfx.process_pdf(pdf)
        assert result["text_truncated"] is True
        assert "texto truncado" in result["text"]

    def test_normal_output_not_flagged(self, make_pdf):
        pdf = make_pdf([{"text": "curto " * 20}])
        result = pdfx.process_pdf(pdf)
        assert result["text_truncated"] is False


def _zip_with(entries, compress=True):
    buf = io.BytesIO()
    mode = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(buf, "w", mode) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


class TestDocxLimits:
    def test_valid_docx_passes(self, docx_bytes):
        assert pdfx.extract_text_docx(docx_bytes).strip() == "Hello world from DOCX"

    def test_zip_without_document_xml_is_refused(self):
        """xlsx/pptx começam com PK\\x03\\x04 e caíam no mammoth com erro confuso."""
        xlsx_like = _zip_with({"xl/workbook.xml": "<workbook/>"})
        with pytest.raises(ValueError, match="não é um DOCX"):
            pdfx.extract_text_docx(xlsx_like)

    def test_zip_bomb_by_ratio_is_refused(self):
        bomb = _zip_with({
            "word/document.xml": "<w:document/>",
            "payload.bin": "A" * (5 * 1024 * 1024),  # comprime pra quase nada
        })
        with pytest.raises(ValueError, match="compressão suspeita|expande demais"):
            pdfx.extract_text_docx(bomb)

    def test_too_many_entries_is_refused(self, monkeypatch):
        monkeypatch.setattr(pdfx, "MAX_ZIP_ENTRIES", 3)
        many = _zip_with({f"f{i}.txt": "x" for i in range(10)})
        with pytest.raises(ValueError, match="entradas demais"):
            pdfx.extract_text_docx(many)

    def test_uncompressed_size_cap(self, monkeypatch):
        monkeypatch.setattr(pdfx, "MAX_DOCX_UNCOMPRESSED", 1024)
        monkeypatch.setattr(pdfx, "MAX_COMPRESSION_RATIO", 10_000)
        big = _zip_with({
            "word/document.xml": "<w:document/>",
            "grande.bin": "A" * 8192,
        })
        with pytest.raises(ValueError, match="expande demais"):
            pdfx.extract_text_docx(big)

    def test_corrupt_zip_is_400_not_500(self):
        with pytest.raises(ValueError, match="não é um ZIP legível"):
            pdfx.extract_text_docx(b"PK\x03\x04lixo")
