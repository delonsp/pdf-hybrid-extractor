"""analyze_image_with_vision: cascata primário→fallback, retorno None em falha."""
import pdf_hybrid_extractor as pdfx


PRIMARY = pdfx.VISION_MODEL
FALLBACK = pdfx.VISION_MODEL_FALLBACK


class TestAnalyzeImageWithVision:
    def test_primary_succeeds(self, fake_gemini_client):
        client = fake_gemini_client({PRIMARY: "extracted text from primary"})
        out = pdfx.analyze_image_with_vision(client, b"img-bytes", page_num=1)
        assert out == "extracted text from primary"
        assert client.calls == [(PRIMARY, 2)]  # 1 prompt + 1 image

    def test_primary_empty_falls_back_to_secondary(self, fake_gemini_client):
        client = fake_gemini_client({PRIMARY: None, FALLBACK: "from fallback"})
        out = pdfx.analyze_image_with_vision(client, b"img", page_num=2)
        assert out == "from fallback"
        assert client.calls == [(PRIMARY, 2), (FALLBACK, 2)]

    def test_primary_exception_falls_back(self, fake_gemini_client):
        client = fake_gemini_client({
            PRIMARY: RuntimeError("429 rate limit"),
            FALLBACK: "ok from fallback",
        })
        out = pdfx.analyze_image_with_vision(client, b"img", page_num=3)
        assert out == "ok from fallback"
        assert len(client.calls) == 2

    def test_both_empty_returns_none(self, fake_gemini_client):
        client = fake_gemini_client({PRIMARY: None, FALLBACK: None})
        out = pdfx.analyze_image_with_vision(client, b"img", page_num=4)
        assert out is None

    def test_both_exception_returns_none(self, fake_gemini_client):
        client = fake_gemini_client({
            PRIMARY: RuntimeError("err1"),
            FALLBACK: RuntimeError("err2"),
        })
        out = pdfx.analyze_image_with_vision(client, b"img", page_num=5)
        assert out is None

    def test_skip_fallback_when_same_as_primary(self, monkeypatch, fake_gemini_client):
        """Se VISION_MODEL == VISION_MODEL_FALLBACK, não chama duas vezes."""
        monkeypatch.setattr(pdfx, "VISION_MODEL", "model-x")
        monkeypatch.setattr(pdfx, "VISION_MODEL_FALLBACK", "model-x")
        client = fake_gemini_client({"model-x": "ok"})
        out = pdfx.analyze_image_with_vision(client, b"img", page_num=6)
        assert out == "ok"
        assert client.calls == [("model-x", 2)]  # só 1 chamada

    def test_empty_string_treated_as_failure(self, fake_gemini_client):
        """response.text == "" também conta como falha (não None mas vazio)."""
        client = fake_gemini_client({PRIMARY: "", FALLBACK: "recovered"})
        out = pdfx.analyze_image_with_vision(client, b"img", page_num=7)
        assert out == "recovered"
