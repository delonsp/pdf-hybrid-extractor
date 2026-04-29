"""Funções puras: _detect_type, _assert_safe_url, _page_image_coverage, TELEFONE_RE."""
import pytest
import fitz
import pdf_hybrid_extractor as pdfx


class TestDetectType:
    def test_pdf_magic(self):
        assert pdfx._detect_type(b"%PDF-1.4\n...") == "pdf"

    def test_docx_magic(self):
        assert pdfx._detect_type(b"PK\x03\x04xx") == "docx"

    def test_unknown(self):
        assert pdfx._detect_type(b"random garbage") == "unknown"

    def test_empty(self):
        assert pdfx._detect_type(b"") == "unknown"


class TestTelefoneRe:
    @pytest.mark.parametrize("valid", ["12345678", "5511999999999", "1" * 20])
    def test_valid(self, valid):
        assert pdfx.TELEFONE_RE.fullmatch(valid)

    @pytest.mark.parametrize("invalid", [
        "1234567",        # 7 dígitos < min 8
        "1" * 21,         # 21 > max 20
        "5511/../9999",   # path injection attempt
        "abc12345",       # letras
        "5511 999",       # espaço
        "+5511999999",    # sinal
        "",               # vazio
    ])
    def test_invalid(self, invalid):
        assert not pdfx.TELEFONE_RE.fullmatch(invalid)


class TestAssertSafeUrl:
    @pytest.mark.parametrize("scheme", ["file", "ftp", "gs", "s3", "javascript"])
    def test_blocks_non_http(self, scheme):
        with pytest.raises(ValueError, match="unsupported URL scheme"):
            pdfx._assert_safe_url(f"{scheme}://anything/x")

    def test_blocks_loopback(self):
        with pytest.raises(ValueError, match="internal address"):
            pdfx._assert_safe_url("http://127.0.0.1/x")

    def test_blocks_link_local_metadata(self):
        with pytest.raises(ValueError, match="internal address"):
            pdfx._assert_safe_url("http://169.254.169.254/latest/meta-data/")

    def test_blocks_rfc1918(self):
        for ip in ["10.0.0.1", "172.16.0.1", "192.168.1.1"]:
            with pytest.raises(ValueError, match="internal address"):
                pdfx._assert_safe_url(f"http://{ip}/x")

    def test_blocks_ipv6_loopback(self):
        with pytest.raises(ValueError, match="internal address"):
            pdfx._assert_safe_url("http://[::1]/x")

    def test_blocks_unresolvable(self):
        with pytest.raises(ValueError, match="could not resolve"):
            pdfx._assert_safe_url("http://this-host-definitely-does-not-exist-xyzqq.invalid/x")

    def test_missing_host(self):
        with pytest.raises(ValueError, match="missing host"):
            pdfx._assert_safe_url("http:///path-only")

    def test_allows_public_dns(self):
        # example.com resolve pra IP público — não deve levantar
        pdfx._assert_safe_url("http://example.com/x")
        pdfx._assert_safe_url("https://example.com/x")


class TestPageImageCoverage:
    def test_no_image(self, make_pdf):
        pdf_bytes = make_pdf([{"text": "só texto, sem imagem"}])
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        assert pdfx._page_image_coverage(doc[0]) == 0.0
        doc.close()

    def test_small_image_logo(self, make_pdf):
        # logo pequeno (60x40 num canvas A4) — coverage <5%
        pdf_bytes = make_pdf([{"text": "header", "image_rect": (500, 750, 560, 790)}])
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        coverage = pdfx._page_image_coverage(doc[0])
        assert coverage < 0.05
        doc.close()

    def test_large_image(self, make_pdf):
        # Imagem cobrindo metade da página
        pdf_bytes = make_pdf([{"image_rect": (50, 50, 550, 700)}])
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        coverage = pdfx._page_image_coverage(doc[0])
        assert coverage > 0.40
        doc.close()
