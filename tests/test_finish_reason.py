"""Diagnóstico de resposta do Gemini (B7 do PRD).

Antes, resposta vazia era sempre logada como "provável safety filter". Mas vazio
tem causas diferentes com tratamentos diferentes — e uma delas é pior que vazio:
MAX_TOKENS devolve texto CORTADO, que volta parecendo completo. Num laudo, um
valor cortado ao meio é mais perigoso que um valor ausente.

O risco cresceu com o B6: transcrição + seção de análise é saída bem mais longa.
"""
import pytest

import pdf_hybrid_extractor as pdfx


PRIMARY = pdfx.VISION_MODEL


class _Finish:
    def __init__(self, name):
        self.name = name


class _Candidate:
    def __init__(self, finish_reason):
        self.finish_reason = finish_reason


class _Feedback:
    def __init__(self, block_reason):
        self.block_reason = block_reason


class _Resp:
    """Resposta configurável — o fake do conftest só tem .text."""
    def __init__(self, text="", finish=None, block=None, text_raises=False):
        self._text = text
        self._raises = text_raises
        self.candidates = [_Candidate(_Finish(finish))] if finish else []
        self.prompt_feedback = _Feedback(_Finish(block)) if block else None

    @property
    def text(self):
        if self._raises:
            raise ValueError("sem parte textual no candidato")
        return self._text


class TestInspect:
    def test_texto_normal(self):
        assert pdfx._inspect_response(_Resp("Hemoglobina 13,2", finish="STOP")) == (
            "Hemoglobina 13,2", None)

    def test_max_tokens_com_texto_e_o_caso_perigoso(self):
        """Texto cortado no meio volta parecendo completo."""
        texto, motivo = pdfx._inspect_response(_Resp("Hemoglobina 13,", finish="MAX_TOKENS"))
        assert texto == "Hemoglobina 13,"
        assert motivo == "truncado_max_tokens"

    def test_max_tokens_sem_texto(self):
        assert pdfx._inspect_response(_Resp("", finish="MAX_TOKENS"))[1] == "truncado_max_tokens"

    def test_safety_nao_e_confundido_com_vazio_generico(self):
        assert pdfx._inspect_response(_Resp("", finish="SAFETY"))[1] == "vazio:safety"

    def test_recitation(self):
        assert pdfx._inspect_response(_Resp("", finish="RECITATION"))[1] == "vazio:recitation"

    def test_prompt_bloqueado(self):
        _, motivo = pdfx._inspect_response(_Resp("", block="OTHER"))
        assert motivo.startswith("prompt_bloqueado")

    def test_vazio_sem_motivo_declarado(self):
        assert pdfx._inspect_response(_Resp("", finish="STOP"))[1] == "vazio:sem_motivo_declarado"

    def test_tolera_resposta_sem_candidates(self):
        """Formato varia entre versões da SDK; diagnóstico não pode derrubar."""
        class Minima:
            text = "ok"
        assert pdfx._inspect_response(Minima()) == ("ok", None)

    def test_tolera_text_que_levanta(self):
        texto, motivo = pdfx._inspect_response(_Resp(text_raises=True, finish="SAFETY"))
        assert texto == ""
        assert motivo == "vazio:safety"


class TestDiagnosticsFlow:
    def _gemini(self, mocker, fake_gemini_client, behavior):
        fake = fake_gemini_client(behavior)
        mocker.patch("pdf_hybrid_extractor.setup_gemini", return_value=fake)
        return fake

    def test_max_output_tokens_vai_no_config(self, mocker, fake_gemini_client):
        fake = self._gemini(mocker, fake_gemini_client, {PRIMARY: "ok"})
        pdfx.analyze_image_with_vision(fake, b"img", 1)
        assert fake.configs[0].max_output_tokens == pdfx.VISION_MAX_OUTPUT_TOKENS

    def test_motivo_do_vazio_chega_no_diagnostics(self, mocker, fake_gemini_client):
        vazio = lambda m, c: _Resp("", finish="SAFETY")
        fake = self._gemini(mocker, fake_gemini_client,
                            {PRIMARY: vazio, pdfx.VISION_MODEL_FALLBACK: vazio})
        diag = {}
        out = pdfx.analyze_image_with_vision(fake, b"img", 7, diagnostics=diag)
        assert out is None
        assert diag[7] == "vazio:safety"

    def test_fallback_bem_sucedido_limpa_o_diagnostico(self, mocker, fake_gemini_client):
        """Primário falhou, fallback salvou: a página NÃO é problemática. Sem
        limpar, ela apareceria em vision_diagnostics tendo funcionado."""
        fake = self._gemini(mocker, fake_gemini_client, {
            PRIMARY: lambda m, c: _Resp("", finish="SAFETY"),
            pdfx.VISION_MODEL_FALLBACK: "transcrição salva pelo fallback",
        })
        diag = {}
        out = pdfx.analyze_image_with_vision(fake, b"img", 3, diagnostics=diag)
        assert out == "transcrição salva pelo fallback"
        assert 3 not in diag

    def test_truncamento_derruba_o_complete(self, make_pdf, mocker, fake_gemini_client):
        """Página cortada é extração incompleta — mesma regra do resto."""
        pdf = make_pdf([{"text": "X", "image_rect": (50, 100, 550, 700)}])
        marcador = pdfx.VISION_ANALYSIS_MARKER
        self._gemini(mocker, fake_gemini_client, {
            PRIMARY: lambda m, c: _Resp(f"Hemoglobina 13,\n{marcador}\nsem imagem de exame",
                                        finish="MAX_TOKENS")
        })
        r = pdfx.process_pdf(pdf)

        assert r["pages_output_truncated"] == [1]
        assert r["vision_diagnostics"]["1"] == "truncado_max_tokens"
        assert r["complete"] is False, "texto cortado não pode se declarar completo"
        # o que deu pra ler continua vindo
        assert "Hemoglobina 13," in r["text"]

    def test_pagina_boa_nao_gera_diagnostico(self, make_pdf, mocker, fake_gemini_client):
        pdf = make_pdf([{"text": "X", "image_rect": (50, 100, 550, 700)}])
        marcador = pdfx.VISION_ANALYSIS_MARKER
        self._gemini(mocker, fake_gemini_client,
                     {PRIMARY: f"transcrição ok\n{marcador}\nsem imagem de exame"})
        r = pdfx.process_pdf(pdf)
        assert r["vision_diagnostics"] == {}
        assert r["pages_output_truncated"] == []
        assert r["complete"] is True
