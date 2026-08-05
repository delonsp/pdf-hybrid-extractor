"""download_file: cap, content-type, SSRF integration."""
import pytest
import pdf_hybrid_extractor as pdfx


class _FakeResponse:
    """Mock mínimo de requests.Response: iter_content, close e redirect.

    download_file segue redirects à mão (allow_redirects=False) pra revalidar o
    guard em cada hop, então o fake precisa de is_redirect/close.
    """
    def __init__(self, body=b"", headers=None, status=200):
        self._body = body
        self.headers = headers or {}
        self.status_code = status
        self.closed = False

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308) and "Location" in self.headers

    @property
    def is_permanent_redirect(self):
        return self.status_code in (301, 308) and "Location" in self.headers

    def close(self):
        self.closed = True

    def raise_for_status(self):
        if self.status_code >= 400:
            from requests import HTTPError
            raise HTTPError(f"{self.status_code}")

    def iter_content(self, chunk_size=64 * 1024):
        # Quebra body em chunks pra exercitar o cap durante o stream
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]


def _redirect(location, status=302):
    return _FakeResponse(status=status, headers={"Location": location})


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

    def test_response_is_closed(self, mocker):
        resp = _FakeResponse(body=b"x")
        _patch_request(mocker, resp)
        pdfx.download_file("http://example.com/x")
        assert resp.closed


class TestRedirectRevalidation:
    """O guard tem que rodar em CADA hop — era o furo do SSRF: um host público
    devolvia 302 pra 169.254.169.254 e o download acontecia."""

    def test_guard_runs_on_every_hop(self, mocker):
        guard = mocker.patch("pdf_hybrid_extractor._assert_safe_url")
        mocker.patch("pdf_hybrid_extractor.requests.get", side_effect=[
            _redirect("http://hop2.example.com/x"),
            _FakeResponse(body=b"%PDF-1.4 ok"),
        ])
        assert pdfx.download_file("http://hop1.example.com/x") == b"%PDF-1.4 ok"
        assert [c.args[0] for c in guard.call_args_list] == [
            "http://hop1.example.com/x",
            "http://hop2.example.com/x",
        ]

    def test_redirect_to_internal_ip_is_blocked(self, mocker):
        """Guard real (não mockado): o segundo hop aponta pra metadata da nuvem."""
        mocker.patch("pdf_hybrid_extractor.requests.get", side_effect=[
            _redirect("http://169.254.169.254/latest/meta-data/"),
            _FakeResponse(body=b"segredo"),
        ])
        mocker.patch("pdf_hybrid_extractor.socket.getaddrinfo", side_effect=lambda host, _: {
            "publico.example.com": [(2, 1, 6, "", ("93.184.216.34", 0))],
            "169.254.169.254": [(2, 1, 6, "", ("169.254.169.254", 0))],
        }[host])
        with pytest.raises(ValueError, match="internal address"):
            pdfx.download_file("http://publico.example.com/x")

    def test_relative_location_is_resolved(self, mocker):
        guard = mocker.patch("pdf_hybrid_extractor._assert_safe_url")
        mocker.patch("pdf_hybrid_extractor.requests.get", side_effect=[
            _redirect("/outro.pdf"),
            _FakeResponse(body=b"ok"),
        ])
        pdfx.download_file("http://example.com/pasta/x.pdf")
        assert guard.call_args_list[1].args[0] == "http://example.com/outro.pdf"

    def test_redirect_loop_is_capped(self, mocker):
        mocker.patch("pdf_hybrid_extractor._assert_safe_url", return_value=None)
        mocker.patch("pdf_hybrid_extractor.requests.get",
                     side_effect=lambda *a, **k: _redirect("http://example.com/loop"))
        with pytest.raises(ValueError, match="too many redirects"):
            pdfx.download_file("http://example.com/loop")

    def test_redirect_without_location(self, mocker):
        mocker.patch("pdf_hybrid_extractor._assert_safe_url", return_value=None)
        resp = _FakeResponse(status=302)  # sem header Location
        mocker.patch("pdf_hybrid_extractor.requests.get", return_value=resp)
        # sem Location o requests não considera redirect; vira resposta normal
        assert pdfx.download_file("http://example.com/x") == b""


class TestHostAllowlist:
    def test_disabled_by_default_allows_any_host(self):
        assert pdfx._host_allowed("qualquer.coisa.com")

    def test_exact_and_subdomain_match(self, monkeypatch):
        monkeypatch.setattr(pdfx, "ALLOWED_DOWNLOAD_HOSTS", {"z-api.io"})
        assert pdfx._host_allowed("z-api.io")
        assert pdfx._host_allowed("media.z-api.io")
        assert not pdfx._host_allowed("z-api.io.evil.com")
        assert not pdfx._host_allowed("outro.com")

    def test_guard_rejects_host_outside_allowlist(self, monkeypatch):
        monkeypatch.setattr(pdfx, "ALLOWED_DOWNLOAD_HOSTS", {"z-api.io"})
        with pytest.raises(ValueError, match="allowlist"):
            pdfx._assert_safe_url("https://evil.com/x.pdf")


class TestDownloadDeadline:
    def test_slow_drip_hits_deadline(self, mocker, monkeypatch):
        """Servidor que goteja bytes segurava a thread pra sempre: o timeout do
        requests é por operação de socket, não deadline total."""
        monkeypatch.setattr(pdfx, "DOWNLOAD_DEADLINE", 0)
        _patch_request(mocker, _FakeResponse(body=b"x" * 1024))
        with pytest.raises(ValueError, match="deadline"):
            pdfx.download_file("http://example.com/slow.pdf")
