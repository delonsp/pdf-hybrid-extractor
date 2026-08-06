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
    def __init__(self, *a, existe=True, erro_no_put=None, erro_no_make=None,
                 lifecycle=None, erro_no_lifecycle=None, **kw):
        self.existe = existe
        self.erro_no_put = erro_no_put
        self.erro_no_make = erro_no_make
        self.objetos = []
        self.buckets_criados = []
        self.lifecycle = lifecycle
        self.erro_no_lifecycle = erro_no_lifecycle
        self.lifecycles_aplicados = []

    def bucket_exists(self, bucket):
        return self.existe

    def make_bucket(self, bucket):
        if self.erro_no_make:
            raise self.erro_no_make
        self.buckets_criados.append(bucket)

    def get_bucket_lifecycle(self, bucket):
        return self.lifecycle

    def set_bucket_lifecycle(self, bucket, config):
        if self.erro_no_lifecycle:
            raise self.erro_no_lifecycle
        self.lifecycles_aplicados.append((bucket, config))
        self.lifecycle = config

    def put_object(self, bucket, name, data, length, content_type=None):
        if self.erro_no_put:
            raise self.erro_no_put
        self.objetos.append((bucket, name, length))


@pytest.fixture(autouse=True)
def _reset_retencao():
    """Cache de retenção é por processo — limpar entre testes."""
    pdfx._retention_applied.clear()
    yield
    pdfx._retention_applied.clear()


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


class TestRetencao:
    """O bucket é área de trabalho para interpretação, não repositório eterno
    de exames. Quem apaga é o Minio, via regra de lifecycle."""

    def test_aplica_expiracao_no_bucket(self, mocker, monkeypatch):
        monkeypatch.setattr(pdfx, "MINIO_RETENTION_DAYS", 60)
        fake = _patch_minio(mocker, _FakeMinio())
        pdfx.save_to_minio_storage(b"%PDF-1.4", "5511999998888", CONFIG)

        assert len(fake.lifecycles_aplicados) == 1
        _, config = fake.lifecycles_aplicados[0]
        regra = config.rules[0]
        assert regra.expiration.days == 60
        assert regra.rule_id == pdfx.RETENTION_RULE_ID

    def test_aplica_uma_vez_por_processo(self, mocker, monkeypatch):
        """Não faz sentido reconfigurar o bucket a cada upload."""
        monkeypatch.setattr(pdfx, "MINIO_RETENTION_DAYS", 60)
        fake = _patch_minio(mocker, _FakeMinio())
        for _ in range(3):
            pdfx.save_to_minio_storage(b"%PDF-1.4", "5511999998888", CONFIG)
        assert len(fake.lifecycles_aplicados) == 1
        assert len(fake.objetos) == 3

    def test_desligado_nao_toca_no_bucket(self, mocker, monkeypatch):
        monkeypatch.setattr(pdfx, "MINIO_RETENTION_DAYS", 0)
        fake = _patch_minio(mocker, _FakeMinio())
        pdfx.save_to_minio_storage(b"%PDF-1.4", "5511999998888", CONFIG)
        assert fake.lifecycles_aplicados == []

    def test_falha_na_retencao_nao_derruba_o_upload(self, mocker, monkeypatch):
        monkeypatch.setattr(pdfx, "MINIO_RETENTION_DAYS", 60)
        fake = _patch_minio(mocker, _FakeMinio(
            erro_no_lifecycle=RuntimeError("sem permissão")))
        caminho, erro = pdfx.save_to_minio_storage(b"%PDF-1.4", "5511999998888", CONFIG)
        assert caminho is not None, "upload foi perdido por causa da retenção"
        assert erro is None

    def test_falha_na_retencao_tenta_de_novo(self, mocker, monkeypatch):
        """Não pode marcar como aplicada em falha: o bucket ficaria acumulando
        sem prazo até o próximo restart, e ninguém perceberia."""
        monkeypatch.setattr(pdfx, "MINIO_RETENTION_DAYS", 60)
        fake = _patch_minio(mocker, _FakeMinio(
            erro_no_lifecycle=RuntimeError("sem permissão")))
        pdfx.save_to_minio_storage(b"%PDF-1.4", "5511999998888", CONFIG)
        assert CONFIG["bucket"] not in pdfx._retention_applied

        fake.erro_no_lifecycle = None
        pdfx.save_to_minio_storage(b"%PDF-1.4", "5511999998888", CONFIG)
        assert len(fake.lifecycles_aplicados) == 1

    def test_regra_ja_correta_nao_e_reescrita(self, mocker, monkeypatch):
        monkeypatch.setattr(pdfx, "MINIO_RETENTION_DAYS", 60)
        from minio.lifecycleconfig import LifecycleConfig, Rule, Expiration
        from minio.commonconfig import ENABLED, Filter
        atual = LifecycleConfig([Rule(
            ENABLED, rule_id=pdfx.RETENTION_RULE_ID,
            rule_filter=Filter(prefix=""), expiration=Expiration(days=60),
        )])
        fake = _patch_minio(mocker, _FakeMinio(lifecycle=atual))
        pdfx.save_to_minio_storage(b"%PDF-1.4", "5511999998888", CONFIG)
        assert fake.lifecycles_aplicados == []

    def test_prazo_alterado_reescreve_a_regra(self, mocker, monkeypatch):
        monkeypatch.setattr(pdfx, "MINIO_RETENTION_DAYS", 30)
        from minio.lifecycleconfig import LifecycleConfig, Rule, Expiration
        from minio.commonconfig import ENABLED, Filter
        antiga = LifecycleConfig([Rule(
            ENABLED, rule_id=pdfx.RETENTION_RULE_ID,
            rule_filter=Filter(prefix=""), expiration=Expiration(days=60),
        )])
        fake = _patch_minio(mocker, _FakeMinio(lifecycle=antiga))
        pdfx.save_to_minio_storage(b"%PDF-1.4", "5511999998888", CONFIG)
        assert fake.lifecycles_aplicados[0][1].rules[0].expiration.days == 30
