"""Visibilidade do modelo e nível de raciocínio (C1/C2 do PRD).

`gemini-flash-latest` é alias que a Google troca a quente. O prompt do B6 e o
teto de tokens do B7 foram calibrados contra o que ele resolve HOJE — se o alias
mudar, os dois mudam de comportamento junto, sem deploy e sem aviso.
"""
import pytest

import pdf_hybrid_extractor as pdfx


PRIMARY = pdfx.VISION_MODEL


class _Resp:
    def __init__(self, text="ok", model_version=None):
        self.text = text
        self.model_version = model_version
        self.candidates = []
        self.prompt_feedback = None


@pytest.fixture(autouse=True)
def _limpa_cache():
    pdfx._model_version_seen.clear()
    yield
    pdfx._model_version_seen.clear()


class TestModelVersionVisivel:
    def test_loga_qual_modelo_atendeu(self, caplog):
        import logging
        with caplog.at_level(logging.INFO):
            pdfx._log_model_version("gemini-flash-latest", _Resp(model_version="gemini-3.6-flash"))
        msgs = [r.message for r in caplog.records if "[modelo]" in r.message]
        assert msgs and "gemini-3.6-flash" in msgs[0]

    def test_loga_uma_vez_so(self, caplog):
        import logging
        with caplog.at_level(logging.INFO):
            for _ in range(5):
                pdfx._log_model_version("alias", _Resp(model_version="v1"))
        assert len([r for r in caplog.records if "[modelo]" in r.message]) == 1

    def test_troca_de_alias_vira_warning(self, caplog):
        """O evento que o C1 existe pra pegar: a Google trocou o alias embaixo."""
        import logging
        with caplog.at_level(logging.INFO):
            pdfx._log_model_version("alias", _Resp(model_version="gemini-3.5-flash"))
            pdfx._log_model_version("alias", _Resp(model_version="gemini-3.6-flash"))
        avisos = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert avisos and "MUDOU" in avisos[0].message

    def test_resposta_sem_model_version_nao_quebra(self):
        pdfx._log_model_version("alias", _Resp())  # não levanta
        assert pdfx._model_version_seen == {}


class TestThinkingConfig:
    def test_nao_seta_nada_por_padrao(self):
        """Baixar o raciocínio no escuro pode piorar o OCR — medir antes."""
        from google.genai import types
        assert pdfx.VISION_THINKING_LEVEL == ""
        assert pdfx._thinking_config(types) is None

    def test_aplica_nivel_configurado(self, monkeypatch):
        from google.genai import types
        monkeypatch.setattr(pdfx, "VISION_THINKING_LEVEL", "low")
        cfg = pdfx._thinking_config(types)
        assert cfg is not None
        assert cfg.thinking_level == "LOW"

    def test_nivel_invalido_e_ignorado_com_aviso(self, monkeypatch, caplog):
        """A SDK NÃO levanta em nível inválido — só emite UserWarning e monta o
        enum errado. Sem validação própria, um typo na env do Dokploy iria pra
        API e derrubaria toda página, já com o tempo gasto."""
        import logging
        from google.genai import types
        monkeypatch.setattr(pdfx, "VISION_THINKING_LEVEL", "altíssimo")
        pdfx._thinking_warned.clear()
        with caplog.at_level(logging.WARNING):
            assert pdfx._thinking_config(types) is None
        assert any("inválido" in r.message for r in caplog.records)

    def test_sdk_realmente_aceita_lixo(self):
        """Fixa a premissa acima: se um dia a SDK passar a validar, este teste
        falha e a validação própria pode ser reavaliada."""
        from google.genai import types
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            cfg = types.ThinkingConfig(thinking_level="ALTÍSSIMO")
        assert cfg.thinking_level == "ALTÍSSIMO", "SDK passou a validar — rever _thinking_config"

    def test_aviso_de_nivel_invalido_nao_repete_por_pagina(self, monkeypatch, caplog):
        import logging
        from google.genai import types
        monkeypatch.setattr(pdfx, "VISION_THINKING_LEVEL", "errado")
        pdfx._thinking_warned.clear()
        with caplog.at_level(logging.WARNING):
            for _ in range(10):
                pdfx._thinking_config(types)
        assert len([r for r in caplog.records if "inválido" in r.message]) == 1

    def test_vai_no_config_da_chamada(self, mocker, fake_gemini_client, monkeypatch):
        monkeypatch.setattr(pdfx, "VISION_THINKING_LEVEL", "minimal")
        fake = fake_gemini_client({PRIMARY: "ok"})
        pdfx.analyze_image_with_vision(fake, b"img", 1)
        assert fake.configs[0].thinking_config.thinking_level == "MINIMAL"


class TestModeloSemThinking:
    """Medido em 06/08/2026: gemini-2.5-flash devolve 400 INVALID_ARGUMENT para
    thinking_level. Como 400 não é retentável, setar VISION_THINKING_LEVEL
    mataria TODA chamada ao fallback — a rede de segurança sumiria justamente
    quando o primário está falhando."""

    @pytest.fixture(autouse=True)
    def _limpa(self):
        pdfx._models_sem_thinking.clear()
        yield
        pdfx._models_sem_thinking.clear()

    def test_detecta_a_mensagem_do_google(self):
        e = Exception("400 INVALID_ARGUMENT. {'error': {'message': "
                      "'Thinking level is not supported for this model.'}}")
        assert pdfx._nao_suporta_thinking(e)
        assert not pdfx._nao_suporta_thinking(Exception("429 quota"))

    def test_repete_sem_thinking_e_salva_a_pagina(self, mocker, fake_gemini_client,
                                                  monkeypatch):
        monkeypatch.setattr(pdfx, "VISION_THINKING_LEVEL", "minimal")
        # primeira chamada falha por thinking; a segunda (sem thinking) devolve OK
        estado = {"n": 0}

        def comportamento(m, c):
            estado["n"] += 1
            if estado["n"] == 1:
                raise Exception("400 Thinking level is not supported for this model.")
            from conftest import FakeGeminiResponse
            return FakeGeminiResponse("transcrição salva")

        fake = fake_gemini_client({pdfx.VISION_MODEL: comportamento})
        out = pdfx.analyze_image_with_vision(fake, b"img", 1)
        assert out == "transcrição salva"
        assert pdfx.VISION_MODEL in pdfx._models_sem_thinking
        # a segunda chamada foi feita SEM thinking_config
        assert fake.configs[0].thinking_config is not None
        assert fake.configs[1].thinking_config is None

    def test_nao_manda_thinking_de_novo_pro_mesmo_modelo(self, mocker, fake_gemini_client,
                                                         monkeypatch):
        monkeypatch.setattr(pdfx, "VISION_THINKING_LEVEL", "minimal")
        pdfx._models_sem_thinking.add(pdfx.VISION_MODEL)
        fake = fake_gemini_client({pdfx.VISION_MODEL: "ok"})
        pdfx.analyze_image_with_vision(fake, b"img", 1)
        assert fake.configs[0].thinking_config is None
