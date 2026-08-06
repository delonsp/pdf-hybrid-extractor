"""Retry em erro transitório (C3 do PRD).

Só entrou DEPOIS do deadline existir: sem orçamento de tempo, retry piora a
situação em vez de melhorar — repete dentro de um prazo que já estoura.
"""
import time

import pytest

import pdf_hybrid_extractor as pdfx
from conftest import FakeGeminiResponse


PRIMARY = pdfx.VISION_MODEL
FALLBACK = pdfx.VISION_MODEL_FALLBACK


class _ApiError(Exception):
    def __init__(self, code, retry_after=None):
        super().__init__(f"{code}")
        self.code = code
        if retry_after is not None:
            self.response = type("R", (), {"headers": {"Retry-After": str(retry_after)}})()


@pytest.fixture(autouse=True)
def _sem_espera_real(mocker):
    """Não gastar tempo de teste dormindo — mas registrar o que foi pedido."""
    return mocker.patch("pdf_hybrid_extractor.time.sleep")


class TestRetryDelay:
    def test_erro_nao_transitorio_nao_repete(self):
        assert pdfx._retry_delay(_ApiError(400), 1) is None
        assert pdfx._retry_delay(_ApiError(403), 1) is None
        assert pdfx._retry_delay(RuntimeError("qualquer"), 1) is None

    def test_transitorios_repetem(self):
        for code in (408, 429, 500, 502, 503, 504):
            assert pdfx._retry_delay(_ApiError(code), 1) is not None

    def test_backoff_exponencial_com_teto(self):
        assert pdfx._retry_delay(_ApiError(429), 1) == 1.0
        assert pdfx._retry_delay(_ApiError(429), 2) == 2.0
        assert pdfx._retry_delay(_ApiError(429), 3) == 4.0
        assert pdfx._retry_delay(_ApiError(429), 9) == 8.0

    def test_retry_after_do_servidor_tem_prioridade(self):
        """Em 429 o servidor diz quando a cota volta. Ignorar é repetir cedo
        demais, tomar 429 de novo e ainda piorar a fila do lado deles."""
        assert pdfx._retry_delay(_ApiError(429, retry_after=17), 1) == 17.0

    def test_retry_after_invalido_cai_no_backoff(self):
        assert pdfx._retry_delay(_ApiError(429, retry_after="depois"), 1) == 1.0


class TestRetryNoFluxo:
    def _cliente(self, mocker, fake_gemini_client, behavior):
        fake = fake_gemini_client(behavior)
        mocker.patch("pdf_hybrid_extractor.setup_gemini", return_value=fake)
        return fake

    def test_repete_o_mesmo_modelo_antes_do_fallback(self, mocker, fake_gemini_client):
        """Trocar de modelo não resolve cota estourada — o fallback tomaria o
        mesmo 429."""
        chamadas = []

        def primario(model, contents):
            chamadas.append(model)
            if len(chamadas) == 1:
                raise _ApiError(429)
            return FakeGeminiResponse("deu certo na segunda")

        fake = self._cliente(mocker, fake_gemini_client, {PRIMARY: primario})
        out = pdfx.analyze_image_with_vision(fake, b"img", 1,
                                             deadline=time.monotonic() + 110)
        assert out == "deu certo na segunda"
        assert chamadas == [PRIMARY, PRIMARY], "caiu pro fallback em vez de repetir"

    def test_respeita_o_teto_de_tentativas(self, mocker, fake_gemini_client):
        sempre429 = lambda m, c: (_ for _ in ()).throw(_ApiError(429))
        fake = self._cliente(mocker, fake_gemini_client,
                             {PRIMARY: sempre429, FALLBACK: sempre429})
        out = pdfx.analyze_image_with_vision(fake, b"img", 1,
                                             deadline=time.monotonic() + 110)
        assert out is None
        assert len(fake.calls) == pdfx.VISION_MAX_ATTEMPTS

    def test_orcamento_de_tentativas_atravessa_os_modelos(self, mocker, fake_gemini_client,
                                                          monkeypatch):
        """Sem orçamento total, N tentativas × 2 modelos caberia numa página só."""
        monkeypatch.setattr(pdfx, "VISION_MAX_ATTEMPTS", 2)
        sempre500 = lambda m, c: (_ for _ in ()).throw(_ApiError(500))
        fake = self._cliente(mocker, fake_gemini_client,
                             {PRIMARY: sempre500, FALLBACK: sempre500})
        pdfx.analyze_image_with_vision(fake, b"img", 1, deadline=time.monotonic() + 110)
        assert len(fake.calls) == 2, "estourou o orçamento total de tentativas"

    def test_nao_repete_se_a_espera_nao_couber(self, mocker, fake_gemini_client,
                                               _sem_espera_real):
        """Repetir só vale se a espera E a chamada seguinte couberem no prazo."""
        fake = self._cliente(mocker, fake_gemini_client,
                             {PRIMARY: lambda m, c: (_ for _ in ()).throw(_ApiError(429, 60))})
        out = pdfx.analyze_image_with_vision(fake, b"img", 1,
                                             deadline=time.monotonic() + 20)
        assert out is None
        assert len(fake.calls) == 1
        _sem_espera_real.assert_not_called()

    def test_erro_definitivo_vai_direto_pro_fallback(self, mocker, fake_gemini_client):
        fake = self._cliente(mocker, fake_gemini_client, {
            PRIMARY: lambda m, c: (_ for _ in ()).throw(_ApiError(400)),
            FALLBACK: "salvo pelo fallback",
        })
        out = pdfx.analyze_image_with_vision(fake, b"img", 1,
                                             deadline=time.monotonic() + 110)
        assert out == "salvo pelo fallback"
        assert [m for m, _ in fake.calls] == [PRIMARY, FALLBACK]

    def test_vazio_nao_e_transitorio(self, mocker, fake_gemini_client):
        """Safety filter não melhora repetindo — vai direto pro outro modelo."""
        fake = self._cliente(mocker, fake_gemini_client,
                             {PRIMARY: None, FALLBACK: "fallback ok"})
        out = pdfx.analyze_image_with_vision(fake, b"img", 1,
                                             deadline=time.monotonic() + 110)
        assert out == "fallback ok"
        assert [m for m, _ in fake.calls] == [PRIMARY, FALLBACK]

    def test_espera_pedida_e_a_do_servidor(self, mocker, fake_gemini_client, _sem_espera_real):
        chamadas = []

        def primario(model, contents):
            chamadas.append(model)
            if len(chamadas) == 1:
                raise _ApiError(429, retry_after=5)
            return FakeGeminiResponse("ok")

        fake = self._cliente(mocker, fake_gemini_client, {PRIMARY: primario})
        pdfx.analyze_image_with_vision(fake, b"img", 1, deadline=time.monotonic() + 110)
        _sem_espera_real.assert_called_once_with(5.0)
