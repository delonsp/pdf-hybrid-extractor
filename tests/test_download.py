"""download_file: cap, content-type, SSRF integration."""
import pytest
import pdf_hybrid_extractor as pdfx


class _FakeResponse:
    """Mock mínimo de requests.Response com suporte ao context manager + iter_content."""
    def __init__(self, body=b"", headers=None, status=200):
        self._body = body
        self.headers = headers or {}
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            from requests import HTTPError
            raise HTTPError(f"{self.status_code}")

    def iter_content(self, chunk_size=64 * 1024):
        # Quebra body em chunks pra exercitar o cap durante o stream
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]


def _patch_request(mocker, response):
    """Patcha requests.get pra retornar response, e _assert_safe_url pra
    aceitar qualquer URL (testes focam em download_file, não SSRF)."""
    mocker.patch("pdf_hybrid_extractor._assert_safe_url", return_value=None)
    mocker.patch("pdf_hybrid_extractor.requests.get", return_value=response)


class TestDownloadFile:
    def test_happy_path(self, mocker):
        body = b"%PDF-1.4 fake pdf body"
        _patch_request(mocker, _FakeResponse(
            body=body,
            headers={"Content-Type": "application/pdf", "Content-Length": str(len(body))},
        ))
        result = pdfx.download_file("http://example.com/x.pdf")
        assert result == body

    def test_content_length_above_cap(self, mocker):
        too_big = pdfx.MAX_DOWNLOAD_BYTES + 1
        _patch_request(mocker, _FakeResponse(
            body=b"x",
            headers={"Content-Length": str(too_big)},
        ))
        with pytest.raises(ValueError, match="too large"):
            pdfx.download_file("http://example.com/big.pdf")

    def test_streaming_exceeds_cap(self, mocker):
        # Server omite Content-Length mas o stream ultrapassa o cap
        body = b"x" * (pdfx.MAX_DOWNLOAD_BYTES + 1024)
        _patch_request(mocker, _FakeResponse(body=body, headers={}))
        with pytest.raises(ValueError, match="too large.*during download"):
            pdfx.download_file("http://example.com/big.pdf")

    def test_unexpected_content_type_warns_but_succeeds(self, mocker, caplog):
        body = b"<html>not a pdf</html>"
        _patch_request(mocker, _FakeResponse(
            body=body,
            headers={"Content-Type": "text/html"},
        ))
        import logging
        with caplog.at_level(logging.WARNING):
            result = pdfx.download_file("http://example.com/x")
        assert result == body
        assert any("Content-Type inesperado" in r.message for r in caplog.records)

    def test_http_error_propagates(self, mocker):
        from requests import HTTPError
        _patch_request(mocker, _FakeResponse(body=b"", status=500))
        with pytest.raises(HTTPError):
            pdfx.download_file("http://example.com/x")

    def test_ssrf_guard_called(self, mocker):
        """download_file deve sempre passar pelo _assert_safe_url."""
        guard = mocker.patch("pdf_hybrid_extractor._assert_safe_url")
        mocker.patch("pdf_hybrid_extractor.requests.get",
                     return_value=_FakeResponse(body=b"x"))
        pdfx.download_file("http://example.com/x")
        guard.assert_called_once_with("http://example.com/x")
