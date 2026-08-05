"""Endpoint /extract: auth, validações de input, dispatch DOCX, /health."""
import base64
import pytest
import pdf_hybrid_extractor as pdfx


PRIMARY = pdfx.VISION_MODEL


class TestHealth:
    def test_no_auth_required(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.get_json() == {"status": "ok"}


class TestAuth:
    def test_missing_header(self, client):
        r = client.post("/extract", json={"url": "https://example.com/x.pdf"})
        assert r.status_code == 401

    def test_wrong_prefix(self, client):
        r = client.post("/extract", json={}, headers={"Authorization": "Basic xxx"})
        assert r.status_code == 401

    def test_wrong_token(self, client):
        r = client.post("/extract", json={}, headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 403

    def test_correct_token(self, client, auth_header):
        # Sem url/base64 → 400 (mas passou da auth)
        r = client.post("/extract", json={}, headers=auth_header)
        assert r.status_code == 400


class TestBodyLimits:
    """Sem MAX_CONTENT_LENGTH, um POST gigante existia como bytes crus, string
    base64 e bytes decodificados ao mesmo tempo — 1 request matava o worker."""

    def test_max_content_length_is_set(self, app):
        cap = app.config["MAX_CONTENT_LENGTH"]
        assert cap is not None
        # tem que caber o cap de download inflado em base64 (4/3), com folga
        assert cap > pdfx.MAX_DOWNLOAD_BYTES

    def test_oversized_body_returns_413_json(self, client, auth_header, app):
        payload = b"x" * (app.config["MAX_CONTENT_LENGTH"] + 1024)
        r = client.post("/extract", data=payload,
                        content_type="application/json", headers=auth_header)
        assert r.status_code == 413
        assert r.is_json
        assert r.get_json()["success"] is False

    def test_base64_above_cap_rejected_before_decode(self, client, auth_header, monkeypatch):
        monkeypatch.setattr(pdfx, "MAX_DOWNLOAD_BYTES", 100)
        r = client.post("/extract", json={"base64": "A" * 400}, headers=auth_header)
        assert r.status_code == 400
        assert "grande demais" in r.get_json()["error"]

    def test_base64_not_a_string(self, client, auth_header):
        r = client.post("/extract", json={"base64": 12345}, headers=auth_header)
        assert r.status_code == 400
        assert "string" in r.get_json()["error"]


class TestErrorsAreJson:
    """n8n espera {"error": ...}; HTML quebrava o parsing do lado dele."""

    def test_non_json_body_returns_json_error(self, client, auth_header):
        r = client.post("/extract", data="isso não é json",
                        content_type="text/plain", headers=auth_header)
        assert r.status_code in (400, 415)
        assert r.is_json, "erro veio em HTML"
        assert r.get_json()["success"] is False

    def test_unknown_route_returns_json(self, client):
        r = client.get("/nao-existe")
        assert r.status_code == 404
        assert r.is_json

    def test_wrong_method_returns_json(self, client):
        r = client.get("/extract")
        assert r.status_code == 405
        assert r.is_json


class TestInputValidation:
    def test_no_url_no_base64(self, client, auth_header):
        r = client.post("/extract", json={}, headers=auth_header)
        assert r.status_code == 400
        assert "url" in r.get_json()["error"].lower()

    def test_url_not_http(self, client, auth_header):
        r = client.post("/extract", json={"url": "/etc/passwd"}, headers=auth_header)
        assert r.status_code == 400
        assert "http" in r.get_json()["error"].lower()

    def test_url_file_scheme(self, client, auth_header):
        r = client.post("/extract", json={"url": "file:///etc/passwd"}, headers=auth_header)
        assert r.status_code == 400

    def test_url_private_ip_blocked_by_ssrf(self, client, auth_header):
        r = client.post("/extract", json={"url": "http://10.0.0.1/x.pdf"}, headers=auth_header)
        assert r.status_code == 400
        assert "internal address" in r.get_json()["error"].lower()

    def test_base64_garbage(self, client, auth_header):
        r = client.post("/extract", json={"base64": "!!not-valid-base64!!"}, headers=auth_header)
        assert r.status_code == 400
        assert "base64" in r.get_json()["error"].lower()

    def test_pdf_corrupted(self, client, auth_header):
        # base64 válido mas conteúdo não é PDF nem DOCX
        bad = base64.b64encode(b"this is just text, not a document").decode()
        r = client.post("/extract", json={"base64": bad}, headers=auth_header)
        assert r.status_code == 400
        # Cai como PDF inválido (default quando magic bytes não batem)
        assert "pdf" in r.get_json()["error"].lower() or "inválido" in r.get_json()["error"].lower()

    @pytest.mark.parametrize("bad_telefone", [
        "123",          # < 8 dígitos
        "1" * 21,       # > 20 dígitos
        "abc12345",     # letras
        "5511/../999",  # path injection
    ])
    def test_telefone_invalido(self, client, auth_header, bad_telefone, make_pdf):
        pdf_b64 = base64.b64encode(make_pdf([{"text": "ok"}])).decode()
        r = client.post(
            "/extract",
            json={"base64": pdf_b64, "telefone": bad_telefone},
            headers=auth_header,
        )
        assert r.status_code == 400
        assert "telefone" in r.get_json()["error"].lower()

    def test_save_to_minio_sem_telefone(self, client, auth_header, make_pdf):
        pdf_b64 = base64.b64encode(make_pdf([{"text": "ok"}])).decode()
        r = client.post(
            "/extract",
            json={"base64": pdf_b64, "save_to_minio": True},
            headers=auth_header,
        )
        assert r.status_code == 400
        assert "telefone" in r.get_json()["error"].lower()


class TestDocxDispatch:
    def test_docx_via_magic_bytes(self, client, auth_header, docx_bytes):
        b64 = base64.b64encode(docx_bytes).decode()
        r = client.post("/extract", json={"base64": b64}, headers=auth_header)
        assert r.status_code == 200
        data = r.get_json()
        assert data["type"] == "docx"
        assert "Hello world from DOCX" in data["text"]

    def test_docx_via_explicit_type(self, client, auth_header, docx_bytes):
        b64 = base64.b64encode(docx_bytes).decode()
        r = client.post(
            "/extract",
            json={"base64": b64, "type": "docx"},
            headers=auth_header,
        )
        assert r.status_code == 200
        assert r.get_json()["type"] == "docx"


class TestPdfHappyPath:
    def test_pdf_native(self, client, auth_header, make_pdf, mocker):
        gemini = mocker.patch("pdf_hybrid_extractor.setup_gemini")
        pdf_b64 = base64.b64encode(make_pdf([{"text": "documento simples " * 10}])).decode()
        r = client.post("/extract", json={"base64": pdf_b64}, headers=auth_header)
        assert r.status_code == 200
        data = r.get_json()
        assert data["type"] == "pdf"
        assert data["total_pages"] == 1
        assert data["pages_with_vision"] == 0
        assert "documento simples" in data["text"]
        gemini.assert_not_called()

    def test_pdf_vision_with_mock(self, client, auth_header, make_pdf, mocker, fake_gemini_client):
        mocker.patch(
            "pdf_hybrid_extractor.setup_gemini",
            return_value=fake_gemini_client({PRIMARY: "vision extracted text"}),
        )
        pdf_b64 = base64.b64encode(
            make_pdf([{"text": "X", "image_rect": (50, 100, 550, 700)}])
        ).decode()
        r = client.post("/extract", json={"base64": pdf_b64}, headers=auth_header)
        assert r.status_code == 200
        data = r.get_json()
        assert data["pages_with_vision"] == 1
        assert "vision extracted text" in data["text"]
