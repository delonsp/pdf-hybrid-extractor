"""process_pdf: classificação de páginas, cap, falhas, PDF inválido/criptografado."""
import pytest
import fitz
import pdf_hybrid_extractor as pdfx


PRIMARY = pdfx.VISION_MODEL


def _patch_gemini(mocker, fake_gemini_client, behavior):
    """Patcha setup_gemini pra retornar fake client com behavior dado."""
    fake = fake_gemini_client(behavior)
    mocker.patch("pdf_hybrid_extractor.setup_gemini", return_value=fake)
    return fake


class TestProcessPdf:
    def test_native_only(self, make_pdf, mocker):
        # PDF com texto longo, sem imagem → não deve chamar Gemini
        pdf = make_pdf([{"text": "Lorem ipsum " * 20}])
        gemini_setup = mocker.patch("pdf_hybrid_extractor.setup_gemini")
        result = pdfx.process_pdf(pdf)
        assert result["success"] is True
        assert result["total_pages"] == 1
        assert result["pages_with_vision"] == 0
        assert result["pages_hybrid"] == 0
        assert "Lorem ipsum" in result["text"]
        gemini_setup.assert_not_called()

    def test_vision_only(self, make_pdf, mocker, fake_gemini_client):
        pdf = make_pdf([{"text": "X", "image_rect": (50, 100, 550, 700)}])
        _patch_gemini(mocker, fake_gemini_client, {PRIMARY: "ultrasound description"})
        result = pdfx.process_pdf(pdf)
        assert result["pages_with_vision"] == 1
        assert result["pages_hybrid"] == 0
        assert "ultrasound description" in result["text"]
        assert "Vision AI" in result["text"]

    def test_hybrid_combines_native_and_vision(self, make_pdf, mocker, fake_gemini_client):
        pdf = make_pdf([{
            "text": "Cabeçalho do laudo. Paciente: Joana. Data: 2026-04-29. " * 3,
            "image_rect": (50, 200, 550, 700),
        }])
        _patch_gemini(mocker, fake_gemini_client, {PRIMARY: "image: ultrasound shows X"})
        result = pdfx.process_pdf(pdf)
        assert result["pages_with_vision"] == 1
        assert result["pages_hybrid"] == 1
        assert "Cabeçalho do laudo" in result["text"]
        assert "image: ultrasound shows X" in result["text"]
        assert "texto + transcrição da imagem" in result["text"]

    def test_cap_skips_excess(self, make_pdf, mocker, monkeypatch, fake_gemini_client):
        monkeypatch.setattr(pdfx, "MAX_VISION_PAGES", 2)
        pages = [{"text": "X", "image_rect": (50, 100, 550, 700)} for _ in range(4)]
        pdf = make_pdf(pages)
        _patch_gemini(mocker, fake_gemini_client, {PRIMARY: "vision result"})
        result = pdfx.process_pdf(pdf)
        assert result["pages_with_vision"] == 2
        assert result["pages_skipped_vision"] == 2
        assert "ignorada - cap de 2" in result["text"]

    def test_failed_page_uses_native_fallback(self, make_pdf, mocker, fake_gemini_client):
        pdf = make_pdf([{
            "text": "texto nativo bem comprido pra passar do threshold. " * 2,
            "image_rect": (50, 200, 550, 700),
        }])
        _patch_gemini(mocker, fake_gemini_client, {
            pdfx.VISION_MODEL: None,
            pdfx.VISION_MODEL_FALLBACK: None,
        })
        result = pdfx.process_pdf(pdf)
        assert result["pages_with_vision"] == 0
        assert result["failed_pages"] == [1]
        assert "texto nativo bem comprido" in result["text"]
        assert "Vision AI falhou" in result["text"]

    def test_corrupted_pdf_raises_value_error(self):
        with pytest.raises(ValueError, match="PDF inválido"):
            pdfx.process_pdf(b"this is plain text, not a PDF at all")

    def test_encrypted_pdf_raises_value_error(self):
        doc = fitz.open()
        doc.new_page().insert_text((50, 50), "secret content")
        encrypted = doc.write(
            encryption=fitz.PDF_ENCRYPT_AES_256,
            owner_pw="o",
            user_pw="user-pwd",
        )
        doc.close()
        with pytest.raises(ValueError, match="criptografado"):
            pdfx.process_pdf(encrypted)

    def test_total_pages_counter(self, make_pdf, mocker, fake_gemini_client):
        pdf = make_pdf([
            {"text": "texto longo " * 20},
            {"text": "X", "image_rect": (50, 100, 550, 700)},
            {"text": "header longo " * 5, "image_rect": (50, 200, 550, 700)},
        ])
        _patch_gemini(mocker, fake_gemini_client, {PRIMARY: "vision-out"})
        result = pdfx.process_pdf(pdf)
        assert result["total_pages"] == 3
        assert result["pages_with_vision"] == 2  # vision + hybrid
        assert result["pages_hybrid"] == 1
