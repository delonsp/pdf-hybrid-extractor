"""Separação entre transcrição e análise da imagem (B6 do PRD).

O prompt antigo terminava com "descreva o que é visível" e o texto inferido saía
concatenado com a transcrição real, sob o mesmo marcador, direto pro prontuário.
Hipótese diagnóstica é permitida — o que não pode é ficar indistinguível do que
estava escrito no documento.
"""
import pdf_hybrid_extractor as pdfx
from conftest import FakeGeminiResponse


PRIMARY = pdfx.VISION_MODEL
MARK = pdfx.VISION_ANALYSIS_MARKER


def _patch_gemini(mocker, fake_gemini_client, behavior):
    fake = fake_gemini_client(behavior)
    mocker.patch("pdf_hybrid_extractor.setup_gemini", return_value=fake)
    return fake


class TestSplit:
    def test_separa_transcricao_de_analise(self):
        raw = f"Hemoglobina 13,2 g/dL\n{MARK}\nVisível: ultrassom abdominal\nHipóteses: considerar esteatose"
        t, a, falhou = pdfx._split_vision_output(raw)
        assert t == "Hemoglobina 13,2 g/dL"
        assert "considerar esteatose" in a
        assert falhou is False

    def test_sem_imagem_nao_vira_campo_de_analise(self):
        """Página de texto puro: a seção vem, mas vazia de conteúdo."""
        t, a, falhou = pdfx._split_vision_output(f"Hemograma completo\n{MARK}\nsem imagem de exame")
        assert t == "Hemograma completo"
        assert a is None
        assert falhou is False

    def test_marcador_ausente_sinaliza_em_vez_de_fingir(self, monkeypatch):
        """Sem marcador não dá pra saber onde a transcrição termina. Fingir que
        separou seria pior: é aí que hipótese entraria passando por transcrição."""
        monkeypatch.setattr(pdfx, "VISION_ANALYZE_IMAGES", True)
        t, a, falhou = pdfx._split_vision_output("texto solto sem marcador")
        assert t == "texto solto sem marcador"
        assert a is None
        assert falhou is True

    def test_marcador_ausente_ok_quando_analise_desligada(self, monkeypatch):
        monkeypatch.setattr(pdfx, "VISION_ANALYZE_IMAGES", False)
        _, _, falhou = pdfx._split_vision_output("só transcrição")
        assert falhou is False


class TestPromptRules:
    def test_transcricao_proibe_adivinhar_valor(self):
        p = pdfx.VISION_PROMPT_TRANSCRICAO
        assert "[ilegível]" in p
        assert "NUNCA adivinhe" in p

    def test_analise_permite_hipotese_mas_proibe_fechar(self):
        # normaliza quebras de linha: a asserção é sobre a regra, não o layout
        p = " ".join(pdfx.VISION_PROMPT_ANALISE.split())
        assert "Hipóteses" in p
        assert "NÃO feche diagnóstico" in p
        assert "não conclusiva" in p
        assert "diferenciais em aberto" in p

    def test_prompt_montado_respeita_a_env(self, monkeypatch):
        monkeypatch.setattr(pdfx, "VISION_ANALYZE_IMAGES", False)
        assert MARK not in pdfx._build_vision_prompt()
        monkeypatch.setattr(pdfx, "VISION_ANALYZE_IMAGES", True)
        assert MARK in pdfx._build_vision_prompt()


class TestNoLeakIntoTranscript:
    def test_hipotese_nao_entra_no_texto(self, make_pdf, mocker, fake_gemini_client):
        """O teste que mais importa: o que o modelo inferiu não pode aparecer no
        campo que o prontuário trata como transcrição do documento."""
        pdf = make_pdf([{"text": "X", "image_rect": (50, 100, 550, 700)}])
        resposta = (
            "Paciente: Joana\nExame: US abdome\n[imagem de exame]\n"
            f"{MARK}\n"
            "Visível: imagem em escala de cinza com marcações de medida\n"
            "Hipóteses: achados podem ser compatíveis com esteatose hepática"
        )
        _patch_gemini(mocker, fake_gemini_client, {PRIMARY: resposta})
        r = pdfx.process_pdf(pdf)

        assert "esteatose" not in r["text"], "hipótese vazou pro texto transcrito"
        assert "Paciente: Joana" in r["text"]
        assert "esteatose" in r["image_analysis"]["1"]
        assert r["analysis_unseparated"] == []

    def test_pagina_sem_separacao_fica_sinalizada(self, make_pdf, mocker, fake_gemini_client):
        pdf = make_pdf([{"text": "X", "image_rect": (50, 100, 550, 700)}])
        _patch_gemini(mocker, fake_gemini_client,
                      {PRIMARY: "modelo ignorou o formato e misturou tudo"})
        r = pdfx.process_pdf(pdf)
        assert r["analysis_unseparated"] == [1]

    def test_hibrido_mantem_texto_nativo_e_transcricao(self, make_pdf, mocker, fake_gemini_client):
        pdf = make_pdf([{
            "text": "Cabeçalho nativo do laudo " * 4,
            "image_rect": (50, 300, 550, 700),
        }])
        resposta = f"transcrição da imagem\n{MARK}\nVisível: raio-x de tórax\nHipóteses: considerar consolidação"
        _patch_gemini(mocker, fake_gemini_client, {PRIMARY: resposta})
        r = pdfx.process_pdf(pdf)

        assert "Cabeçalho nativo do laudo" in r["text"]
        assert "transcrição da imagem" in r["text"]
        assert "consolidação" not in r["text"]
        assert "consolidação" in r["image_analysis"]["1"]

    def test_analise_ausente_nao_cria_campo(self, make_pdf, mocker, fake_gemini_client):
        pdf = make_pdf([{"text": "X", "image_rect": (50, 100, 550, 700)}])
        _patch_gemini(mocker, fake_gemini_client,
                      {PRIMARY: f"Hemograma\n{MARK}\nsem imagem de exame"})
        r = pdfx.process_pdf(pdf)
        assert r["image_analysis"] == {}
        assert r["analysis_unseparated"] == []
