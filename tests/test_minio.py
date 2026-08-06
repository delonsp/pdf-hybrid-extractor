"""Persistência no Minio (B9 do PRD).

Três defeitos separados: gravava antes de validar (lixo no bucket), falhava em
silêncio (`success: true` com `minio_path: null`), e o telefone ficava na chave
do objeto — identificador pessoal exposto em listagem, métrica e na resposta.
"""
import pytest

import pdf_hybrid_extractor as pdfx


PRIMARY = pdfx.VISION_MODEL
CONFIG = {
    "endpoint": "minio.local:9000",
    "access_key": "k",
    "secret_key": "s",
    "secure": False,
    "bucket": "pacientes",
}


class _FakeS3Error(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _FakeMinio:
    def __init__(self, *a, existe=True, erro_no_put=None, erro_no_make=None, **kw):
        self.existe = existe
        self.erro_no_put = erro_no_put
        self.erro_no_make = erro_no_make
        self.objetos = []
        self.buckets_criados = []

    def bucket_exists(self, bucket):
        return self.existe

    def make_bucket(self, bucket):
        if self.erro_no_make:
            raise self.erro_no_make
        self.buckets_criados.append(bucket)

    def put_object(self, bucket, name, data, length, content_type=None):
        if self.erro_no_put:
            raise self.erro_no_put
        self.objetos.append((bucket, name, length))


def _patch_minio(mocker, fake):
    mocker.patch("minio.Minio", return_value=fake)
    mocker.patch("minio.error.S3Error", _FakeS3Error)
    return fake


class TestChaveDoObjeto:
    def test_default_mantem_telefone(self, monkeypatch):
        """Compatibilidade: virar pseudônimo quebra busca nos objetos já gravados."""
        monkeypatch.setattr(pdfx, "MINIO_KEY_MODE", "telefone")
        assert pdfx._minio_prefix("5511999998888") == "5511999998888"

    def test_pseudonimo_nao_contem_o_telefone(self, monkeypatch):
        monkeypatch.setattr(pdfx, "MINIO_KEY_MODE", "pseudonimo")
        monkeypatch.setattr(pdfx, "MINIO_KEY_SALT", "sal-secreto")
        p = pdfx._minio_prefix("5511999998888")
        assert "5511999998888" not in p
        assert len(p) == 32

    def test_pseudonimo_e_estavel(self, monkeypatch):
        """Mesmo paciente cai sempre no mesmo prefixo — ainda dá pra agrupar."""
        monkeypatch.setattr(pdfx, "MINIO_KEY_MODE", "pseudonimo")
        monkeypatch.setattr(pdfx, "MINIO_KEY_SALT", "sal-secreto")
        assert pdfx._minio_prefix("5511999998888") == pdfx._minio_prefix("5511999998888")
        assert pdfx._minio_prefix("5511999998888") != pdfx._minio_prefix("5511999997777")

    def test_sal_diferente_muda_o_prefixo(self, monkeypatch):
        monkeypatch.setattr(pdfx, "MINIO_KEY_MODE", "pseudonimo")
        monkeypatch.setattr(pdfx, "MINIO_KEY_SALT", "sal-a")
        a = pdfx._minio_prefix("5511999998888")
        monkeypatch.setattr(pdfx, "MINIO_KEY_SALT", "sal-b")
        assert pdfx._minio_prefix("5511999998888") != a

    def test_pseudonimo_sem_sal_e_recusado(self, monkeypatch):
        """Sem sal, o espaço de telefones é pequeno demais: força bruta trivial."""
        monkeypatch.setattr(pdfx, "MINIO_KEY_MODE", "pseudonimo")
        monkeypatch.setattr(pdfx, "MINIO_KEY_SALT", "")
        with pytest.raises(ValueError, match="MINIO_KEY_SALT"):
            pdfx._minio_prefix("5511999998888")


class TestFalhaExplicita:
    def test_sucesso_devolve_caminho_sem_erro(self, mocker):
        fake = _patch_minio(mocker, _FakeMinio())
        caminho, erro = pdfx.save_to_minio_storage(b"%PDF-1.4", "5511999998888", CONFIG)
        assert caminho.startswith("pacientes/5511999998888/")
        assert caminho.endswith(".pdf")
        assert erro is None
        assert len(fake.objetos) == 1

    def test_falha_no_upload_devolve_motivo(self, mocker):
        """Antes: retornava None e o endpoint respondia success:true, minio_path:null."""
        _patch_minio(mocker, _FakeMinio(erro_no_put=RuntimeError("bucket cheio")))
        caminho, erro = pdfx.save_to_minio_storage(b"%PDF-1.4", "5511999998888", CONFIG)
        assert caminho is None
        assert "bucket cheio" in erro

    def test_corrida_na_criacao_do_bucket_nao_e_erro(self, mocker):
        """Duas requests veem o bucket faltando e ambas tentam criar."""
        _patch_minio(mocker, _FakeMinio(
            existe=False, erro_no_make=_FakeS3Error("BucketAlreadyOwnedByYou")))
        caminho, erro = pdfx.save_to_minio_storage(b"%PDF-1.4", "5511999998888", CONFIG)
        assert erro is None
        assert caminho is not None

    def test_outro_erro_de_bucket_propaga(self, mocker):
        _patch_minio(mocker, _FakeMinio(
            existe=False, erro_no_make=_FakeS3Error("AccessDenied")))
        caminho, erro = pdfx.save_to_minio_storage(b"%PDF-1.4", "5511999998888", CONFIG)
        assert caminho is None
        assert "AccessDenied" in erro


class TestGravaDepoisDeValidar:
    def test_pdf_invalido_nao_chega_no_bucket(self, mocker):
        """Antes o arquivo era gravado e só então o open falhava — lixo no bucket."""
        fake = _patch_minio(mocker, _FakeMinio())
        with pytest.raises(ValueError, match="inválido|corrompido"):
            pdfx.process_pdf(b"isso nao e um pdf", save_to_minio=True,
                             telefone="5511999998888", minio_config=CONFIG)
        assert fake.objetos == [], "PDF inválido foi gravado mesmo assim"

    def test_pdf_valido_e_gravado(self, make_pdf, mocker):
        fake = _patch_minio(mocker, _FakeMinio())
        pdf = make_pdf([{"text": "laudo " * 20}])
        r = pdfx.process_pdf(pdf, save_to_minio=True, telefone="5511999998888",
                             minio_config=CONFIG)
        assert len(fake.objetos) == 1
        assert r["minio_stored"] is True
        assert r["minio_error"] is None

    def test_falha_de_persistencia_fica_visivel_na_resposta(self, make_pdf, mocker):
        _patch_minio(mocker, _FakeMinio(erro_no_put=RuntimeError("sem espaço")))
        pdf = make_pdf([{"text": "laudo " * 20}])
        r = pdfx.process_pdf(pdf, save_to_minio=True, telefone="5511999998888",
                             minio_config=CONFIG)
        # A extração não é jogada fora só porque o arquivo não foi guardado
        assert r["success"] is True
        assert "laudo" in r["text"]
        # mas quem pediu persistência tem como saber que ela não aconteceu
        assert r["minio_stored"] is False
        assert "sem espaço" in r["minio_error"]

    def test_sem_pedido_de_persistencia_campos_ficam_nulos(self, make_pdf):
        pdf = make_pdf([{"text": "laudo " * 20}])
        r = pdfx.process_pdf(pdf)
        assert r["minio_stored"] is None
        assert r["minio_error"] is None
