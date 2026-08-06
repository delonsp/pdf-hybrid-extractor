"""PyMuPDF fora das threads (B2 do PRD).

A doc do PyMuPDF diz que multiprocessing é suportado e multithreading não. O modo
de falha não é exceção — é pixmap corrompido (imagem lixo que o Gemini "lê" e
vira texto inventado no prontuário) ou queda do interpretador, que com 1 worker
leva junto as 4 requisições em voo. Nada disso aparece em teste comum, então os
testes aqui verificam a ESTRUTURA: quem chama o PyMuPDF e sob qual lock.
"""
import threading
import time

import fitz
import pytest

import pdf_hybrid_extractor as pdfx
from conftest import FakeGeminiResponse


PRIMARY = pdfx.VISION_MODEL


def _patch_gemini(mocker, fake_gemini_client, behavior):
    fake = fake_gemini_client(behavior)
    mocker.patch("pdf_hybrid_extractor.setup_gemini", return_value=fake)
    return fake


class TestRenderStaysInCallingThread:
    def test_render_never_runs_in_pool(self, make_pdf, mocker, fake_gemini_client):
        """Antes o render rodava dentro do ThreadPoolExecutor. Agora só a
        chamada HTTP vai pro pool."""
        pdf = make_pdf([{"text": "X", "image_rect": (50, 100, 550, 700)} for _ in range(4)])
        _patch_gemini(mocker, fake_gemini_client, {PRIMARY: "ok"})

        chamador = threading.current_thread()
        vistos = []
        real = pdfx._render_page_png

        def spy(doc, index):
            vistos.append(threading.current_thread())
            return real(doc, index)

        mocker.patch.object(pdfx, "_render_page_png", spy)
        pdfx.process_pdf(pdf)

        assert vistos, "nenhum render aconteceu — teste não provou nada"
        assert all(t is chamador for t in vistos), (
            "PyMuPDF rodou fora da thread chamadora"
        )

    def test_gemini_still_runs_in_parallel(self, make_pdf, mocker, fake_gemini_client,
                                           monkeypatch):
        """O paralelismo que interessa (espera de rede) tem que continuar.
        Barreira em vez de sleep: se as chamadas forem serializadas, ela estoura
        na hora em vez de deixar o teste passar por acaso.
        Nº de páginas == parties da barreira == VISION_PARALLEL, senão sobra
        chamada esperando sozinha."""
        monkeypatch.setattr(pdfx, "VISION_PARALLEL", 2)
        pdf = make_pdf([{"text": "X", "image_rect": (50, 100, 550, 700)} for _ in range(2)])
        barreira = threading.Barrier(2, timeout=5)

        def _concorrente(model, contents):
            try:
                barreira.wait()
            except threading.BrokenBarrierError:
                pytest.fail("chamadas ao Gemini foram serializadas")
            return FakeGeminiResponse("ok")

        _patch_gemini(mocker, fake_gemini_client, {PRIMARY: _concorrente})
        result = pdfx.process_pdf(pdf)
        assert result["pages_with_vision"] == 2


class TestCrossRequestSerialization:
    """A camada que o antigo doc_lock NUNCA cobriu: ele era criado dentro do
    process_pdf, então as 4 threads do gunicorn usavam o MuPDF ao mesmo tempo
    com locks que não se enxergavam."""

    def test_two_concurrent_extractions_never_overlap_in_pymupdf(
        self, make_pdf, mocker, fake_gemini_client
    ):
        pdf = make_pdf([{"text": "X", "image_rect": (50, 100, 550, 700)} for _ in range(3)])
        _patch_gemini(mocker, fake_gemini_client, {PRIMARY: "ok"})

        estado = {"agora": 0, "pico": 0}
        contador = threading.Lock()
        real_pixmap = fitz.Page.get_pixmap

        def spy(self, *args, **kwargs):
            with contador:
                estado["agora"] += 1
                estado["pico"] = max(estado["pico"], estado["agora"])
            try:
                time.sleep(0.02)  # janela pra sobreposição aparecer
                return real_pixmap(self, *args, **kwargs)
            finally:
                with contador:
                    estado["agora"] -= 1

        mocker.patch.object(fitz.Page, "get_pixmap", spy)

        erros = []

        def _extrair():
            try:
                pdfx.process_pdf(pdf)
            except Exception as e:  # pragma: no cover
                erros.append(e)

        ts = [threading.Thread(target=_extrair) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=30)

        assert not erros, f"extração concorrente levantou: {erros}"
        assert not any(t.is_alive() for t in ts), "deadlock entre extrações"
        assert estado["pico"] == 1, (
            f"PyMuPDF rodou em paralelo (pico={estado['pico']}) — o lock global não segurou"
        )


class TestPipelineBackpressure:
    """O semáforo limita quantas imagens ficam vivas ao mesmo tempo. Se ele
    vazasse numa falha, o loop travaria pra sempre segurando a thread."""

    def test_no_deadlock_when_every_render_fails(self, make_pdf, mocker, fake_gemini_client):
        n = pdfx.VISION_PARALLEL + 4  # mais páginas que vagas do semáforo
        pdf = make_pdf([{"text": "X", "image_rect": (50, 100, 550, 700)} for _ in range(n)])
        _patch_gemini(mocker, fake_gemini_client, {PRIMARY: "ok"})
        mocker.patch.object(pdfx, "_render_page_png",
                            side_effect=RuntimeError("render quebrado"))

        resultado = {}

        def _extrair():
            resultado["r"] = pdfx.process_pdf(pdf)

        t = threading.Thread(target=_extrair)
        t.start()
        t.join(timeout=15)
        assert not t.is_alive(), "semáforo vazou: o loop travou"
        assert sorted(resultado["r"]["failed_pages"]) == list(range(1, n + 1))

    def test_live_images_are_bounded(self, make_pdf, mocker, fake_gemini_client):
        """Sem contrapressão, 15 páginas renderizadas de uma vez ficariam todas
        na memória esperando a fila do executor."""
        n = 10
        pdf = make_pdf([{"text": "X", "image_rect": (50, 100, 550, 700)} for _ in range(n)])

        estado = {"vivas": 0, "pico": 0}
        contador = threading.Lock()

        def _segura(model, contents):
            time.sleep(0.05)
            with contador:
                estado["vivas"] -= 1
            return FakeGeminiResponse("ok")

        real = pdfx._render_page_png

        def spy(doc, index):
            out = real(doc, index)
            with contador:
                estado["vivas"] += 1
                estado["pico"] = max(estado["pico"], estado["vivas"])
            return out

        mocker.patch.object(pdfx, "_render_page_png", spy)
        _patch_gemini(mocker, fake_gemini_client, {PRIMARY: _segura})
        pdfx.process_pdf(pdf)

        assert estado["pico"] <= pdfx.VISION_PARALLEL + 1, (
            f"{estado['pico']} imagens vivas ao mesmo tempo — contrapressão não segurou"
        )
