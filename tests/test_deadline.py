"""Orçamento de tempo (Lote B do PRD).

Medições de produção que fundamentam os números: uma página no Vision custa
15-19s ponta a ponta, e o webhook chamador desiste em 120s. Daí o deadline de
110s — e a regra de que nenhuma chamada individual pode sobreviver a ele.
"""
import time

import pytest

import pdf_hybrid_extractor as pdfx


PRIMARY = pdfx.VISION_MODEL
FALLBACK = pdfx.VISION_MODEL_FALLBACK


def _patch_gemini(mocker, fake_gemini_client, behavior):
    fake = fake_gemini_client(behavior)
    mocker.patch("pdf_hybrid_extractor.setup_gemini", return_value=fake)
    return fake


class TestConditionalCascade:
    """A cascata dobrava o custo da página justamente quando o tempo apertava."""

    def test_fallback_skipped_when_budget_is_short(self, mocker, fake_gemini_client):
        fake = _patch_gemini(mocker, fake_gemini_client, {PRIMARY: None, FALLBACK: "salvo"})
        # Só 5s restantes: menos que FALLBACK_MIN_BUDGET (25s)
        deadline = time.monotonic() + 5
        out = pdfx.analyze_image_with_vision(fake, b"img", 1, deadline=deadline)
        assert out is None, "deveria desistir em vez de gastar o resto do orçamento"
        assert [m for m, _ in fake.calls] == [PRIMARY], "não podia ter tentado o fallback"

    def test_fallback_used_when_budget_allows(self, mocker, fake_gemini_client):
        fake = _patch_gemini(mocker, fake_gemini_client, {PRIMARY: None, FALLBACK: "salvo"})
        deadline = time.monotonic() + 100
        out = pdfx.analyze_image_with_vision(fake, b"img", 1, deadline=deadline)
        assert out == "salvo"
        assert [m for m, _ in fake.calls] == [PRIMARY, FALLBACK]

    def test_no_call_at_all_when_budget_gone(self, mocker, fake_gemini_client):
        fake = _patch_gemini(mocker, fake_gemini_client, {PRIMARY: "ok"})
        out = pdfx.analyze_image_with_vision(fake, b"img", 1, deadline=time.monotonic() - 1)
        assert out is None
        assert fake.calls == []

    def test_without_deadline_cascade_is_unconditional(self, mocker, fake_gemini_client):
        """Uso via CLI não tem prazo — comportamento antigo preservado."""
        fake = _patch_gemini(mocker, fake_gemini_client, {PRIMARY: None, FALLBACK: "salvo"})
        assert pdfx.analyze_image_with_vision(fake, b"img", 1) == "salvo"


class TestPerCallTimeout:
    """Nenhuma chamada pode sobreviver ao prazo da requisição."""

    def test_call_timeout_clamped_to_remaining_budget(self, mocker, fake_gemini_client):
        fake = _patch_gemini(mocker, fake_gemini_client, {PRIMARY: "ok"})
        pdfx.analyze_image_with_vision(fake, b"img", 1, deadline=time.monotonic() + 10)
        timeout_ms = fake.configs[0].http_options.timeout
        assert timeout_ms <= 10_000, "timeout da chamada excedeu o que restava"

    def test_call_timeout_uses_gemini_timeout_when_budget_is_large(self, mocker, fake_gemini_client):
        fake = _patch_gemini(mocker, fake_gemini_client, {PRIMARY: "ok"})
        pdfx.analyze_image_with_vision(fake, b"img", 1, deadline=time.monotonic() + 9999)
        assert fake.configs[0].http_options.timeout == pdfx.GEMINI_TIMEOUT * 1000


class TestDeadlineInProcessPdf:
    def test_expired_deadline_returns_partial_not_error(self, make_pdf, mocker, fake_gemini_client):
        pdf = make_pdf([
            {"text": "cabeçalho do laudo " * 5},
            {"text": "X", "image_rect": (50, 100, 550, 700)},
        ])
        _patch_gemini(mocker, fake_gemini_client, {PRIMARY: "nunca chamado"})
        result = pdfx.process_pdf(pdf, deadline=time.monotonic() - 1)
        assert result["success"] is True
        assert result["complete"] is False, "resposta parcial não pode se declarar completa"
        assert result["deadline_exceeded"] is True
        assert result["pages_deadline_skipped"] == [2]
        # texto nativo das páginas boas preservado
        assert "cabeçalho do laudo" in result["text"]
        assert "prazo" in result["text"]

    def test_deadline_page_is_not_counted_as_vision_failure(self, make_pdf, mocker, fake_gemini_client):
        pdf = make_pdf([{"text": "X", "image_rect": (50, 100, 550, 700)}])
        _patch_gemini(mocker, fake_gemini_client, {PRIMARY: "ok"})
        result = pdfx.process_pdf(pdf, deadline=time.monotonic() - 1)
        assert result["failed_pages"] == [], "prazo não é falha do Vision — confunde diagnóstico"
        assert result["pages_deadline_skipped"] == [1]

    def test_complete_true_on_clean_run(self, make_pdf, mocker, fake_gemini_client):
        pdf = make_pdf([{"text": "laudo completo " * 10}])
        result = pdfx.process_pdf(pdf, deadline=time.monotonic() + 110)
        assert result["complete"] is True


class TestStartMinBudget:
    """Alavanca desligada por padrão: apertar transformaria em parcial documento
    que hoje completa — e logo o mais lento, que tende a ser o mais denso."""

    def test_disabled_by_default(self):
        assert pdfx.VISION_START_MIN_BUDGET == 0

    def test_page_still_starts_with_small_budget_when_disabled(self, make_pdf, mocker, fake_gemini_client):
        pdf = make_pdf([{"text": "X", "image_rect": (50, 100, 550, 700)}])
        _patch_gemini(mocker, fake_gemini_client, {PRIMARY: "extraído"})
        result = pdfx.process_pdf(pdf, deadline=time.monotonic() + 3)
        assert result["pages_with_vision"] == 1

    def test_page_skipped_when_knob_is_on(self, make_pdf, mocker, fake_gemini_client, monkeypatch):
        monkeypatch.setattr(pdfx, "VISION_START_MIN_BUDGET", 30)
        pdf = make_pdf([{"text": "X", "image_rect": (50, 100, 550, 700)}])
        fake = _patch_gemini(mocker, fake_gemini_client, {PRIMARY: "não deveria rodar"})
        result = pdfx.process_pdf(pdf, deadline=time.monotonic() + 10)
        assert fake.calls == []
        assert result["pages_deadline_skipped"] == [1]


class TestCallerGaveUp:
    """O webhook desiste em 120s; seguir trabalhando é desperdício garantido."""

    def test_cancelled_stops_vision_and_marks_response(self, make_pdf, mocker, fake_gemini_client):
        pdf = make_pdf([{"text": "X", "image_rect": (50, 100, 550, 700)}])
        fake = _patch_gemini(mocker, fake_gemini_client, {PRIMARY: "não deveria rodar"})
        result = pdfx.process_pdf(pdf, deadline=time.monotonic() + 110,
                                  is_cancelled=lambda: True)
        assert fake.calls == [], "gastou cota do Gemini depois do chamador sumir"
        assert result["caller_gone"] is True
        assert result["complete"] is False

    def test_not_cancelled_runs_normally(self, make_pdf, mocker, fake_gemini_client):
        pdf = make_pdf([{"text": "X", "image_rect": (50, 100, 550, 700)}])
        fake = _patch_gemini(mocker, fake_gemini_client, {PRIMARY: "extraído"})
        result = pdfx.process_pdf(pdf, deadline=time.monotonic() + 110,
                                  is_cancelled=lambda: False)
        assert result["pages_with_vision"] == 1
        assert result["caller_gone"] is False


class TestAdmissionControl:
    """Fila só converteria rejeição rápida em timeout lento e silencioso."""

    def test_returns_503_with_retry_after_when_full(self, client, auth_header, monkeypatch):
        import threading
        # Semáforo esgotado: simula todas as extrações em voo
        esgotado = threading.BoundedSemaphore(1)
        esgotado.acquire()
        monkeypatch.setattr(pdfx, "_extract_slots", esgotado)
        r = client.post("/extract", json={"base64": "aGk="}, headers=auth_header)
        assert r.status_code == 503
        assert r.headers["Retry-After"] == str(pdfx.RETRY_AFTER_SECONDS)
        assert r.is_json

    def test_slot_is_released_after_error(self, client, auth_header):
        """Erro não pode vazar slot — senão o serviço se estrangula sozinho."""
        for _ in range(pdfx.MAX_CONCURRENT_EXTRACTIONS + 2):
            r = client.post("/extract", json={"base64": "bm90IGEgcGRm"}, headers=auth_header)
            assert r.status_code == 400, "slot vazou: passou a recusar por capacidade"

    def test_health_stays_available(self, client):
        assert client.get("/health").status_code == 200

    def test_slot_not_leaked_on_internal_error(self, client, auth_header, mocker):
        """Exceção entre o acquire e o finally vazaria o slot PARA SEMPRE — o
        serviço se estrangularia sozinho, sem nunca voltar."""
        mocker.patch("pdf_hybrid_extractor._make_cancel_check",
                     side_effect=RuntimeError("boom"))
        r = client.post("/extract", json={"base64": "aGk="}, headers=auth_header)
        assert r.status_code == 500
        assert pdfx._extract_slots.acquire(blocking=False), "slot vazou"
        pdfx._extract_slots.release()


class TestEsperaNaFila:
    """O REQUEST_DEADLINE começa quando o handler roda, não quando a requisição
    chega. Se ela esperou no backlog, esse tempo saiu do orçamento do CHAMADOR
    sem sair do nosso — e o timeout dele estoura com a gente 'dentro do prazo'."""

    def test_sem_header_comportamento_antigo(self, app):
        with app.test_request_context("/extract"):
            assert pdfx._fila_esperada() == 0.0

    def test_desconta_espera_informada(self, app):
        with app.test_request_context(
                "/extract", headers={"X-Request-Start": str(time.time() - 30)}):
            assert 29 <= pdfx._fila_esperada() <= 32

    def test_aceita_milissegundos(self, app):
        with app.test_request_context(
                "/extract", headers={"X-Request-Start": str(int((time.time() - 20) * 1000))}):
            assert 19 <= pdfx._fila_esperada() <= 22

    def test_aceita_prefixo_t(self, app):
        with app.test_request_context(
                "/extract", headers={"X-Request-Start": f"t={time.time() - 10}"}):
            assert 9 <= pdfx._fila_esperada() <= 12

    def test_valor_futuro_e_ignorado(self, app):
        """Relógios de máquinas diferentes não são confiáveis; skew não pode
        fazer a gente recusar tudo."""
        with app.test_request_context(
                "/extract", headers={"X-Request-Start": str(time.time() + 500)}):
            assert pdfx._fila_esperada() == 0.0

    def test_valor_absurdo_e_ignorado(self, app):
        with app.test_request_context("/extract", headers={"X-Request-Start": "1"}):
            assert pdfx._fila_esperada() == 0.0

    def test_lixo_e_ignorado(self, app):
        with app.test_request_context("/extract", headers={"X-Request-Start": "ontem"}):
            assert pdfx._fila_esperada() == 0.0

    def test_espera_longa_devolve_503_em_vez_de_trabalho_orfao(self, client, auth_header):
        """Sem prazo útil sobrando, começar seria trabalho garantidamente
        descartado ocupando thread viva."""
        r = client.post("/extract", json={"base64": "aGk="},
                        headers={**auth_header,
                                 "X-Request-Start": str(time.time() - pdfx.REQUEST_DEADLINE)})
        assert r.status_code == 503
        assert r.headers["Retry-After"] == str(pdfx.RETRY_AFTER_SECONDS)


class TestInvarianteDeCapacidade:
    def test_semaforo_menor_que_threads(self):
        """A folga é o que garante /health e 503 rápido. Se isto quebrar num
        ajuste de env, o serviço perde as duas garantias em silêncio."""
        assert pdfx.MAX_CONCURRENT_EXTRACTIONS < pdfx.GUNICORN_THREADS

    def test_configuracao_invalida_loga_erro(self, monkeypatch, caplog):
        import logging
        monkeypatch.setattr(pdfx, "MAX_CONCURRENT_EXTRACTIONS", 8)
        monkeypatch.setattr(pdfx, "GUNICORN_THREADS", 8)
        with caplog.at_level(logging.ERROR):
            pdfx.create_app()
        assert any("CONFIGURAÇÃO INVÁLIDA" in r.message for r in caplog.records)
