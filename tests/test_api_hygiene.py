"""Validação de entrada, resposta de DOCX e símbolos clínicos (C7/C9/C10)."""
import base64
import io
import zipfile

import pytest

import pdf_hybrid_extractor as pdfx


class TestCorpoInvalido:
    """Cada um destes virava 500 genérico — erro de input reportado como falha
    do servidor, o que manda o chamador caçar problema no lugar errado."""

    def test_json_lista_vira_400(self, client, auth_header):
        r = client.post("/extract", json=[1, 2, 3], headers=auth_header)
        assert r.status_code == 400
        assert "objeto JSON" in r.get_json()["error"]

    def test_json_string_vira_400(self, client, auth_header):
        r = client.post("/extract", json="só uma string", headers=auth_header)
        assert r.status_code == 400

    def test_type_nao_string_vira_400(self, client, auth_header):
        r = client.post("/extract", json={"base64": "aGk=", "type": 123},
                        headers=auth_header)
        assert r.status_code == 400
        assert "string" in r.get_json()["error"]

    def test_type_fora_do_dominio_vira_400(self, client, auth_header):
        """Antes caía no caminho de PDF e devolvia 'PDF inválido', escondendo
        o erro real."""
        r = client.post("/extract", json={"base64": "aGk=", "type": "xlsx"},
                        headers=auth_header)
        assert r.status_code == 400
        erro = r.get_json()["error"]
        assert "xlsx" in erro and "inválido" in erro

    def test_type_valido_continua_passando(self, client, auth_header, docx_bytes):
        r = client.post("/extract",
                        json={"base64": base64.b64encode(docx_bytes).decode(),
                              "type": "docx"},
                        headers=auth_header)
        assert r.status_code == 200
        assert r.get_json()["type"] == "docx"


class TestRespostaDocx:
    def test_total_pages_nao_mente(self, client, auth_header, docx_bytes):
        """DOCX não tem paginação confiável sem renderizar — `1` era falso."""
        r = client.post("/extract",
                        json={"base64": base64.b64encode(docx_bytes).decode()},
                        headers=auth_header)
        assert r.get_json()["total_pages"] is None

    def test_docx_simples_e_completo(self, client, auth_header, docx_bytes):
        j = client.post("/extract",
                        json={"base64": base64.b64encode(docx_bytes).decode()},
                        headers=auth_header).get_json()
        assert j["complete"] is True
        assert j["has_unextracted_images"] is False

    def test_docx_com_imagem_nao_se_declara_completo(self, client, auth_header, docx_bytes):
        """Scan colado no Word não passa pelo Vision — o conteúdo dele some."""
        buf = io.BytesIO(docx_bytes)
        saida = io.BytesIO()
        with zipfile.ZipFile(buf) as origem, zipfile.ZipFile(saida, "w") as destino:
            for item in origem.infolist():
                destino.writestr(item, origem.read(item.filename))
            destino.writestr("word/media/image1.png", b"\x89PNG-falso")
        r = client.post("/extract",
                        json={"base64": base64.b64encode(saida.getvalue()).decode()},
                        headers=auth_header)
        j = r.get_json()
        assert j["has_unextracted_images"] is True
        assert j["complete"] is False

    def test_shape_igual_ao_do_pdf(self, client, auth_header, docx_bytes):
        """n8n não deveria precisar tratar dois formatos de resposta."""
        j = client.post("/extract",
                        json={"base64": base64.b64encode(docx_bytes).decode()},
                        headers=auth_header).get_json()
        for chave in ("success", "complete", "type", "total_pages",
                      "pages_with_vision", "pages_hybrid", "pages_skipped_vision",
                      "pages_deadline_skipped", "deadline_exceeded", "caller_gone",
                      "failed_pages", "text_truncated", "text", "image_analysis",
                      "analysis_unseparated", "vision_diagnostics",
                      "pages_output_truncated", "minio_path", "minio_stored",
                      "minio_error"):
            assert chave in j, f"resposta de DOCX sem a chave {chave!r}"


class TestSimbolosClinicos:
    def test_micro_vira_mu_grego(self):
        """'µg/dL' e 'μg/dL' eram strings diferentes: a busca acha uma e perde
        a outra, sem erro nenhum."""
        assert pdfx.normalizar_texto_clinico("µg/dL") == "μg/dL"

    def test_metro_quadrado_e_preservado(self):
        """NFKC resolveria o µ, mas converteria m² em m2 — unidade de superfície
        corporal destruída."""
        assert "m²" in pdfx.normalizar_texto_clinico("1,73 m²")

    def test_sinal_de_menos_unicode_vira_hifen(self):
        assert pdfx.normalizar_texto_clinico("T-score −2,5") == "T-score -2,5"

    def test_espaco_inseparavel_vira_espaco(self):
        assert pdfx.normalizar_texto_clinico("13,2 g/dL") == "13,2 g/dL"

    def test_acento_combinado_e_composto(self):
        combinado = "João"  # "Joa" + til combinante
        assert pdfx.normalizar_texto_clinico(combinado) == "João"

    def test_aplicado_no_texto_do_pdf(self, make_pdf):
        pdf = make_pdf([{"text": "Cortisol 12 µg/dL " * 5}])
        r = pdfx.process_pdf(pdf)
        assert "μg/dL" in r["text"]
        assert "µg/dL" not in r["text"]
