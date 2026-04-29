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
