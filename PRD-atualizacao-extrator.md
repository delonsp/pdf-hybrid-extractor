# PRD — Atualização do PDF Hybrid Extractor

**Data:** 2026-08-05 · **Versão do documento:** 2 (revisada após crítica externa)
**Baseline auditado:** commit `ace35ef` (master limpo)
**Análise:** Claude Opus 5 · **Auditoria e crítica independentes:** Codex `gpt-5.6-sol` (reasoning high)

> **Versão 2:** a v1 tinha erros factuais apontados pela crítica externa e confirmados por mim
> na fonte. Estão corrigidos e marcados com ⚠ ao longo do texto, para não voltarem.

---

## 1. Contexto

`pdf_hybrid_extractor.py` roda em produção no Dokploy (porta 5050), é chamado pelo n8n e
processa **documentos médicos de pacientes** (PHI / LGPD). Já passou por 3 rodadas de hardening
e tem testes. Este PRD cobre o que sobrou.

**O que já está correto (não mexer):** guard anti-SSRF de IP privado, cap de download, token com
`compare_digest`, rate limit, container non-root, healthcheck, 500 genérico, timestamp+UUID no
Minio, classificação de página em 3 modos, render lazy, cascata de modelo.

**Achado central desta revisão:** a maior parte dos problemas restantes não é *bug de lógica* — é
**ausência de limite**. Não há teto de pixels, de expansão de ZIP, de tempo total por request, nem
de páginas totais. Cada um deles derruba o worker único, e o gunicorn **não** intervém (§4.2).

---

## 2. Gate 0 — decisão contratual, não código

**Nenhuma linha de Python resolve este item, e ele é o de maior consequência.**

`genai.Client(api_key=...)` (`:104`) usa o **Gemini Developer API**. Na quota gratuita, os termos
do Google permitem usar entrada e saída para melhorar produtos, **com revisão humana**, e pedem
para não enviar dado pessoal sensível. Os termos também restringem uso em prática clínica. O que
sai daqui é laudo com nome, idade, data e resultado — dado de saúde, o mais protegido pela LGPD
(art. 5º, II).

- [x] **0.1** ~~Confirmar se a chave do Gemini está em projeto com billing pago~~ — **CONFIRMADO em 05/08/2026: chave em billing pago.** Isso remove o risco principal: no plano pago o Google não usa entrada/saída para treinar nem submete a revisão humana. O gate deixa de ser bloqueante.
- [ ] **0.2** Resta verificar **retenção** (Zero Data Retention não vem ligado por padrão) — risco bem menor que o do plano gratuito, mas ainda é dado de saúde armazenado temporariamente em terceiro
- [ ] **0.3** Registrar a decisão por escrito no repo (`LGPD.md`), com finalidade, retenção e base legal
- [ ] **0.4** Estender a mesma análise ao Minio: retenção, criptografia, controle de acesso e exclusão

A troca recente da chave do Gemini é a hora certa de verificar em qual projeto/billing ela está.

---

## 3. Achados priorizados

| # | Sev | Área | Achado | Linha |
|---|-----|------|--------|-------|
| 0 | **Gate** | LGPD | PHI vai para o Gemini Developer API sem plano pago/DPA/ZDR confirmado | `:66`, `:104` |
| 1 | **P0** | Segurança | SSRF sobrevive via **redirect**; rebinding de DNS segue aberto | `:140-141` |
| 2 | **P0** | DoS/memória | **Sem teto de pixels no render** — `MediaBox` gigante estoura a memória | `:340` |
| 3 | **P0** | DoS/memória | Corpo HTTP e base64 sem limite; ZIP de DOCX sem limite de expansão | `:504`, `:526`, `:180` |
| 4 | **P1** | Concorrência | PyMuPDF roda em threads em **duas** camadas (gthread + executor) | `:294`, `:339`, `Dockerfile:27` |
| 5 | **P1** | Operação | **Não existe limite superior de tempo por request** — o timeout do gunicorn não vale aqui | `:76`, `Dockerfile:27` |
| 6 | **P1** | Clínico | Prompt pede **interpretação**, e o texto inferido sai misturado à transcrição | `:92` |
| 7 | **P1** | Correção | Exceção no render (e no `get_text`) mata o documento inteiro | `:299`, `:337-346` |
| 8 | **P1** | Honestidade | Extração truncada/parcial retorna `success: true` mudo | `:75`, `:321`, `:401` |
| 9 | **P1** | Modelo | `gemini-flash-latest` é alias hot-swap, com thinking ligado por padrão | `:81` |
| 10 | **P1** | Minio | Grava antes de validar; falha silenciosa; telefone dentro da chave | `:277`, `:434`, `:448` |
| 11 | **P1** | Build | Só pisos `>=`; sem lock de transitivas nem digest da imagem | `requirements.txt` |
| 12 | **P2** | API | Corpo não-JSON, JSON escalar e `type` não-string viram HTML/500 | `:504`, `:533` |
| 13 | **P2** | Extração | Cobertura de imagem ignora conteúdo **vetorial** | `:233` |
| 14 | **P2** | Extração | Classificação por 50 caracteres erra nos dois sentidos | `:69`, `:301` |
| 15 | **P2** | DOCX | Todo ZIP vira DOCX; `total_pages: 1` é falso; scan em DOCX não vai ao Vision | `:171`, `:538` |
| 16 | **P2** | Encoding | Sem normalização de símbolo clínico (`µ` vs `μ`) | `:395` |
| 17 | **P3** | Rede | `ProxyFix(x_for=1)` permite spoof de IP se a porta estiver exposta | `:466` |
| 18 | **P3** | Licença | PyMuPDF é AGPL/comercial — serviço em rede precisa de decisão registrada | `requirements.txt:3` |

---

## 4. Detalhamento dos itens estruturais

### 4.1 · P0-1 — SSRF por redirect (e o rebinding que fica aberto)

`_assert_safe_url()` valida só a URL inicial; `requests.get()` **segue redirects por padrão** e
nenhum hop seguinte é revalidado. Um servidor externo devolve `302 → 169.254.169.254` (metadata
da nuvem) ou `→ 10.x.x.x:9000` (o próprio Minio) e o download acontece.

**Correção mínima:** `allow_redirects=False` + loop de no máximo 3 hops, revalidando cada
`Location`. Detalhes que a implementação **precisa** tratar, ou o buraco continua: `Location`
relativo (usar `urljoin`), fechar cada resposta intermediária, e o cap de tamanho valendo no
hop final.

⚠ **Correção da v1:** eu havia registrado o rebinding de DNS como "risco residual aceito". A
crítica está certa: aceitar isso deixa o P0 **aberto**, não mitigado. Entre a validação
(`getaddrinfo`) e a conexão (`requests`) há uma segunda resolução que pode devolver IP privado.

**Correção forte, recomendada:** **allowlist de domínios de origem** (o Minio do próprio Dr. Alain
e os domínios de mídia do WhatsApp cobrem o uso real) + bloqueio de egress interno na rede do
Docker. Isso fecha redirect e rebinding de uma vez, sem malabarismo de socket. Se URL arbitrária
for requisito de verdade, aí sim é preciso conectar ao IP já validado preservando Host/SNI.

### 4.2 · P1-5 — Não existe limite superior de tempo por request ⚠

**Este item mudou completamente da v1, e a premissa registrada no `CLAUDE.md` também está errada.**

A v1 deste PRD dizia que o pior caso (600s) estouraria o timeout de 480s e o gunicorn mataria o
worker. **Verifiquei no fonte do gunicorn (`workers/gthread.py`): o loop chama `self.notify()`
incondicionalmente a cada iteração, sem checar se as threads de request estão presas.** Ou seja:

> Com `gthread`, `--timeout 480` é detector de worker **silencioso**, não teto de request.
> Uma extração pode rodar 600s, 1200s ou mais, e o gunicorn nunca intervém.

O que realmente acontece é pior que morrer no timeout: a request **fica**, segurando uma das 4
threads. Quatro PDFs lentos ocupam as quatro threads e o serviço para de responder — sem crash,
sem log de erro, sem restart. O n8n estoura do lado dele e provavelmente **repete**, piorando.

E o pior caso é alto mesmo: a cascata primário→fallback gasta até `2 × GEMINI_TIMEOUT` por
página, então `5 ondas × 120s = 600s` **antes** de qualquer retry.

**Correção:** deadline global por request, aplicado no código (único lugar que pode aplicá-lo),
verificado antes de despachar cada onda. Páginas que não couberem no orçamento entram em
`failed_pages` e a resposta volta 200 **parcial e explícita**. Alinhar com o timeout do n8n.

**Itens que compartilham este mesmo orçamento e têm que ser dimensionados num cálculo só:**
deadline (aqui), cap de páginas (§4.4), retry (§5, Lote C) e a cascata de modelos.

### 4.3 · P1-4 — PyMuPDF em threads, em duas camadas ⚠

O `doc_lock` (`:294`) é criado **dentro** de `process_pdf` — é um lock por request. Com
`gthread` e `threads=4`, quatro requests processam PDFs ao mesmo tempo no mesmo processo, cada
uma com um lock que não enxerga os outros. A documentação do PyMuPDF diz que multiprocessing é
suportado e **multithreading não é**.

⚠ **Correção da correção (v1):** eu havia proposto trocar para N workers × 1 thread. **Isso não
basta** — mesmo com 1 thread de gunicorn, o `get_pixmap()` continua rodando dentro do
`ThreadPoolExecutor(max_workers=3)` interno (`:339-344`). O PyMuPDF continuaria em threads.

**Correção real:** tirar o PyMuPDF do executor. **Renderizar no fluxo principal** e paralelizar
**apenas a chamada HTTP ao Gemini**, que é a parte lenta (5-30s) e a única que se beneficia.
Para não segurar 15 PNGs na memória, render com limite de N em voo (produtor com fila limitada).
E aí sim, avaliar N workers × 1 thread no gunicorn.

**Consequência a medir antes:** N workers multiplica a memória e **multiplica o rate limit
in-memory por N** (3 workers = 3× o limite efetivo). Isso empurra o Redis para dentro do escopo.

### 4.4 · P1-8 — Truncamento silencioso, com caso real de campo

**Caso de 05/08/2026, não hipotético.** Chegou um **DUTCH Complete de 16 páginas** (resultado de
30/07). `MAX_VISION_PAGES = 15`, e `to_vision[:15]` (`:321`) fica com as primeiras em ordem de
documento e descarta a cauda — no DUTCH, justamente os gráficos detalhados e a metodologia.

⚠ **Condicional, e a condição importa:** o cap conta **páginas candidatas ao Vision**, não páginas
totais. Um documento de 16 páginas com texto nativo suficiente **não é truncado**. E há um caso
em que o cap nem é o gargalo: se os gráficos do DUTCH forem **vetoriais** (§ item 13), aquelas
páginas têm texto de sobra, a cobertura de imagem dá `0.0`, são classificadas `native` e **nunca
chegam ao Vision** — subir o cap não mudaria nada.

**Por isso a primeira tarefa aqui é medir num DUTCH real, não mexer no número.**

Independente da causa, o defeito de contrato é o mesmo e é sério: **a resposta volta
`success: true` com o texto parecendo completo.** Se o fluxo do n8n não inspecionar
`pages_skipped_vision`, um prontuário incompleto é gravado sem ninguém perceber.

Se for preciso subir o cap, o custo não é trocar 15 por 16 — o orçamento é
`ceil(páginas / paralelismo) × tempo_por_página`, e ele já não tem folga (§4.2):

| Cap | Paralelo | Ondas | Pior caso (com cascata) |
|---|---|---|---|
| 15 | 3 | 5 | 600s |
| 20 | 3 | 7 | 840s |
| 25 | 5 | 5 | 600s |

Subir o cap **exige** subir o paralelismo junto — e `VISION_PARALLEL=5` × 4 threads = até 20
chamadas simultâneas ao Gemini, o que esbarra no RPM da conta. Conferir o limite antes.

`MAX_VISION_PAGES`, `VISION_PARALLEL`, `GEMINI_TIMEOUT` e `MIN_TEXT_THRESHOLD` estão
**hardcoded** (`:69`, `:75`, `:76`, `:77`) — hoje, ajustar exige mudar código e redeployar.

### 4.4b · Dimensionamento do Lote B com carga real medida (05/08/2026)

**Carga do webhook-pacientes, 30 dias** (`conversas_pacientes`). Todo documento é
candidato ao extrator quando é PDF:

| Direção | Total 30d | Média/dia | Pico num dia | Pico no mesmo minuto |
|---|---|---|---|---|
| Entrada (paciente → clínica) | 507 | 16,9 | 147 | **59** |
| Saída (clínica → paciente) | 1.129 | 37,6 | 114 | 12 |
| Somados | 1.636 | 54,5 | 236 | — |

Ressalvas registradas: o pico de 59 foi rajada de DICOM, que não chega ao extrator —
prova o **padrão de comportamento**, não que 59 PDFs simultâneos já tenham ocorrido. E
`mime_detectado` só começou a ser gravado em 05/08, então a fração exata que vira PDF sai
daqui a uma semana. A saída (`fromMe`) costuma ser esquecida e disputa as mesmas threads.

**O dado que manda em tudo: o chamador desiste em 120s.**

> O webhook tem timeout de 120s por requisição. **A thread do extrator continua ocupada
> depois disso.** Ou seja: a fila pode encher com trabalho que ninguém mais está esperando.

Isso reordena o problema inteiro:

1. **O orçamento real é 120s, não 480s.** O pior caso atual é 600s — **5× mais longo do que
   qualquer um espera**. Todo segundo além de 120s é trabalho garantidamente jogado fora que
   ainda assim bloqueia requisição viva. O deadline do B3 tem que ser derivado do timeout do
   chamador (~110s), não do gunicorn.
2. **`GEMINI_TIMEOUT = 60s` é inviável.** Uma única chamada consome metade do orçamento total;
   com a cascata primário→fallback, **uma página sozinha** (120s) já estoura. Precisa cair para
   ~25-30s, e a cascata tem que **dividir** o orçamento, não dobrá-lo.
3. **A fila tem que ser ~zero.** Com 4 threads, a vazão é `240 / T_médio` por minuto:

   | Tempo médio por PDF | Vazão | Absorve rajada de… |
   |---|---|---|
   | 18s (5 págs, Gemini rápido) | ~13/min | 13 |
   | 30s | 8/min | 8 |
   | 60s | 4/min | 4 |

   Para absorver 59/min seria preciso `T ≈ 4s` — impossível com PDF escaneado. E como qualquer
   espera na fila é roubada de um orçamento de 120s já apertado, **enfileirar não ajuda: só
   transforma rejeição rápida em timeout lento.** Com as 4 threads ocupadas, o certo é recusar
   na hora com **503 + `Retry-After`**, como você mesmo colocou: erro visível é melhor que travar
   em silêncio.
4. **Trabalho órfão é o pior tipo de ocupação.** Requisição abandonada pelo chamador continua
   rodando, gastando thread *e* cota do Gemini por um resultado que ninguém vai ler. Vale
   detectar desconexão do cliente e abortar.

**Números derivados (a validar com medição real de latência do Gemini):** deadline de request
110s, `GEMINI_TIMEOUT` 25-30s, `VISION_PARALLEL` 5 → 15 páginas em `ceil(15/5) × 25 = 75s`,
dentro do orçamento. **Mas** 5 paralelas × 4 threads = até 20 chamadas simultâneas ao Gemini
no pico: **conferir o RPM da conta antes**, senão troca-se timeout por 429 em massa.

---

### 4.4c · Calibração com distribuição real (06/08/2026) — ⚠ corrige a tabela acima

Um dia inteiro de dados, `n = 38`. **93% dos documentos com mime detectado são PDF** (38 de 41;
o único que ficou local foi um xlsx). Ou seja: **o extrator é o caminho principal, não um
caminho secundário** — o que eleva a consequência de tudo neste PRD.

| p50 | p90 | p99 | máximo |
|---|---|---|---|
| **4,5s** | 32,9s | 85,4s | **95,4s** |

Acima de 60s: 3 · acima de 90s: 1 · acima de 110s: **nenhum**

⚠ **A tabela de vazão do §4.4b estava errada.** Ela usou média de 18-30s, tirada de uma amostra
de 1-2 medições, e chegou a 8-13 PDFs/min. A **mediana real é 4,5s**:

| | Estimado antes | Real |
|---|---|---|
| Vazão, 4 threads | 8-13/min | **~53/min** |
| Vazão, 3 slots de admissão | — | **~40/min** |
| Rajada de 59/min | não absorve | quase absorve |

**A conclusão inverte: o gargalo não é vazão, é a cauda.** Um PDF de 95,4s segura uma thread
pelo tempo de **21 PDFs medianos**. Com 3 slots, bastam 3 documentos da cauda simultâneos para
tudo o mais tomar 503 por ~85s.

**Consequência de desenho: subir `VISION_PARALLEL` rende pouco.** O problema não é falta de
paralelismo, é o item lento ocupando slot. Isso é uma boa notícia dupla — dispensa o risco de
429 em massa que o próprio B3d levantou, e mantém `VISION_PARALLEL=3`.

**Decisão consciente: NÃO apertar o deadline.** A tentação seria cortar em ~70s para liberar
thread mais cedo. **Recusado.** Hoje um documento de 95,4s *completa*; um corte em 70s o
transformaria em parcial. Isso troca completude de laudo clínico por disponibilidade de thread —
e justamente nos documentos mais lentos, que tendem a ser os mais densos, os que mais importam.
O prazo de 110s fica, e a cauda continua cabendo nele.

Existe o knob `VISION_START_MIN_BUDGET` (default `0` = desligado): não começa página nova se
restar menos que X segundos de orçamento. Para quando/se a exaustão de threads acontecer de
verdade. **A solução certa para a cauda é fila assíncrona** (§3, fora de escopo), não truncar
documento clínico.

**O prazo está bem calibrado, mas apertado:** o máximo observado usou 87% do orçamento. Com
amostra maior, algum vai estourar — então o que importa é o parcial ser **preservado e visível**,
não o prazo ser generoso. Confirmado em campo: a detecção de parcial já pegou um laudo
incompleto que antes teria sido gravado como completo.

---

### 4.5 · P1-6 — O prompt pede interpretação clínica

`VISION_PROMPT` (`:92`) termina com: *"Se for uma imagem de exame (ultrassom, raio-x, etc),
**descreva o que é visível**."* Isso convida o modelo a inferir, e o texto gerado sai concatenado
com a transcrição real sob o mesmo marcador — sem nada que separe *"isto estava escrito"* de
*"isto o modelo achou que estava vendo"*. A jusante, n8n e `interpretar_exames.py` recebem os
dois como a mesma coisa, e vai para o prontuário.

**Correção:** prompt principal exige **transcrição literal**, marca trecho ilegível como tal e
**proíbe** diagnóstico ou inferência. Se a descrição visual for desejada, vai em **campo separado
na resposta**, rotulado como geração do modelo.

**Evidência a favor, de 05/08:** o modelo transcreveu literalmente uma tela de sistema — incluindo
"1 de 1" e o aviso do Adobe Reader — em vez de inventar contexto. O comportamento certo já
aparece; é o prompt que ainda autoriza o contrário.

### 4.6 · P0-2 e P0-3 — Os limites que faltam

Nenhum destes é coberto pelo cap de 50 MB de download:

- **Pixels do render** (`:340`): `Matrix(2,2)` sobre um `MediaBox` enorme tenta alocar centenas de
  MB a GB **por página**, × 3 threads. Um PDF de poucos KB derruba o worker. Precisa de
  `MAX_RENDER_PIXELS` com fallback de escala.
- **Corpo HTTP / base64** (`:504`, `:526`): sem `MAX_CONTENT_LENGTH`, o corpo existe
  simultaneamente como bytes crus, string base64 e bytes decodificados. ⚠ **Correção da v1:**
  `MAX_CONTENT_LENGTH` devolve **413**, não 400 — e o cálculo certo é
  `ceil(MAX_DOWNLOAD_BYTES / 3) * 4` mais margem de JSON. Precisa de handler JSON para o 413.
- **ZIP bomb no DOCX** (`:180`): ⚠ conferir `word/document.xml` (proposta da v1) valida
  *estrutura*, **não protege contra expansão**. Um DOCX de poucos MB expande para GB dentro do
  mammoth. Precisa de limite de tamanho descomprimido, número de entradas e taxa de compressão.
- **Páginas totais** (`:299`): `MAX_TOTAL_PAGES`, mais limite de texto de saída.
- **Deadline de download** (`:141`): `timeout=60` do requests é por operação de socket, **não é
  deadline total** — um servidor que envia poucos bytes a cada 59s segura a thread indefinidamente.
- **No proxy também:** o limite do Flask não protege o Traefik/gunicorn do tráfego gigante que
  chega antes. Configurar limite equivalente no proxy reverso.

### 4.7 · P1-9 — Modelo pinado, e o thinking medido (não chutado) ⚠

`gemini-flash-latest` é alias que a Google troca a quente. **Pinar continua certo.** Mas ⚠ a
justificativa da v1 estava mal fundamentada: minhas fontes e as da crítica **divergem** sobre
para qual modelo o alias aponta hoje (3.5 vs 3.6 Flash) e sobre o nível padrão de thinking
(`medium` vs `high`).

**Essa divergência é, ela própria, o argumento:** se duas leituras da documentação no mesmo dia
não concordam sobre o que o alias resolve, o serviço não deveria depender dele.

**Correção:** pinar em ID explícito. **Não** setar `thinking_level` no escuro — medir primeiro,
lendo `usage_metadata.thoughts_token_count`, `finish_reason` e `model_version` numa amostra
anonimizada de laudos reais, e escolher o nível pelo resultado. Baixar o thinking pode reduzir a
qualidade do OCR, que é exatamente o que não se quer perder.

Ler `finish_reason` também corrige um erro de diagnóstico atual: hoje resposta vazia é logada como
"provável safety filter" (`:209`), mas pode ser `MAX_TOKENS`, recitação ou candidato sem parte
textual — causas com tratamentos diferentes.

### 4.8 · P1-10 — Minio

- Grava **antes** de `fitz.open()` validar (`:277` vs `:283`): PDF corrompido fica armazenado, e
  fica órfão quando a extração falha depois.
- Falha de upload é engolida (`:448`): retorna `success: true` com `minio_path: null`. Se a
  persistência foi pedida, isso deveria ser falha explícita.
- **Telefone dentro da chave do objeto** (`:434`): identificador pessoal exposto em listagens,
  métricas e no `minio_path` devolvido ao n8n. Mascarar o log não resolve — a chave precisa ser
  pseudonimizada.
- Corrida entre `bucket_exists()` e `make_bucket()` (`:436-437`) com duas requests simultâneas.

---

## 5. Tarefas

### Lote A — Fecha buracos de derrubar/vazar (P0)

**Status: implementado em 05/08/2026 — suíte de 70 → 101 testes, verde.** Pendentes: A2 (decisão de
allowlist) e A8 (config de infra).

- [x] A1. `download_file`: `allow_redirects=False` + no máx. 3 hops revalidados, com `urljoin` para `Location` relativo e fechamento de cada resposta
- [x] A2a. Mecanismo de allowlist por **sufixo** + modo observação (`ALLOWED_DOWNLOAD_HOSTS_ENFORCE`, default `false`) e log do host de toda origem
- [ ] A2b. Rollout: subir com a lista preenchida em **modo observação**, deixar acumular ~1 semana de tráfego real, revisar o log e só então virar `ENFORCE=true`
  - Medido em produção em 05/08/2026 (amostra pequena: 9 URLs, 1 dia de `midia_ref`): `f004.backblazeb2.com` (7×) e `v2.temp-file.download` (2×) → sufixos `backblazeb2.com` e `temp-file.download`
  - **Decisão consciente a registrar:** o `net_guard` do sistema de mídia deliberadamente NÃO tem allowlist (só bloqueia faixa de IP interno), pra não arriscar perder mídia de paciente. Se aqui a allowlist entrar em enforce e lá não, o mesmo domínio passa num caminho e é recusado no outro. A assimetria se justifica — este serviço **devolve o conteúdo baixado no corpo da resposta**, então um SSRF bem-sucedido aqui é primitiva de leitura direta, o que não vale para o outro caminho — mas tem que ser escolha, não acidente
- [ ] A2c. Egress filtering na rede do Docker (complementa a allowlist; fecha o caminho mesmo se a lista falhar)
- [x] A3. `MAX_CONTENT_LENGTH = ceil(MAX_DOWNLOAD_BYTES / 3) * 4` + margem; handler **JSON para 413**; checar tamanho do base64 **antes** de decodificar
- [x] A4. `MAX_RENDER_PIXELS`: reescala o zoom até caber; abaixo de `MIN_RENDER_ZOOM` recusa a página (render ilegível não vale a chamada ao Gemini)
- [x] A5. Limites de ZIP no DOCX: tamanho descomprimido, nº de entradas e taxa de compressão — mais a checagem de `word/document.xml` (item C8, feito junto por abrir o mesmo ZIP)
- [x] A6. `MAX_TOTAL_PAGES` + `MAX_OUTPUT_CHARS` com flag `text_truncated` na resposta
- [x] A7. `DOWNLOAD_DEADLINE` total (o `timeout` do requests é por operação de socket)
- [ ] A8. Limite de corpo equivalente no Traefik/proxy — *config de infra, fora do repo*
- [x] A9. Testes: redirect para IP privado, redirect encadeado, `Location` relativo, loop de redirect, allowlist, deadline, base64 acima do cap, 413/415/404/405 em JSON, MediaBox gigante, ZIP bomb, ZIP que não é DOCX, PDF com páginas demais
- [x] A10. *(extra)* Handler `HTTPException` → **todo erro HTTP em JSON**; era pré-requisito do 413 do A3

### Lote B — Correção e honestidade da extração (P1)

- [x] B1. `try/except` por página cobrindo **`get_text()` (passada 1) e o render**, não só o Vision; página quebrada entra em `failed_pages` *(feito junto com o Lote A em 05/08)*
- [x] B2. Tirar o PyMuPDF do `ThreadPoolExecutor`: render no fluxo principal com N em voo; paralelizar só a chamada HTTP ao Gemini. **Foi além do previsto:** o lock virou global do processo (`_pymupdf_lock`), fechando também a camada entre requisições — as 4 threads do gunicorn usavam o MuPDF ao mesmo tempo com locks que não se enxergavam, e nenhuma versão do `doc_lock` cobria isso
- [x] B3. **Deadline global por request**, verificado antes de cada onda; derivado do **timeout de 120s do chamador** (~110s), não do gunicorn — ver §4.4b. Inclui baixar `GEMINI_TIMEOUT` para 25-30s e fazer a cascata dividir o orçamento em vez de dobrá-lo
- [x] B3b. **Admission control**: com as threads ocupadas, recusar na hora com **503 + `Retry-After`** em vez de enfileirar — fila só converte rejeição rápida em timeout lento, porque a espera é roubada do mesmo orçamento de 120s
- [x] B3c. **Abortar trabalho órfão**: detectar desconexão do chamador e parar a extração — hoje a requisição continua consumindo thread e cota do Gemini depois que o webhook já desistiu
- [ ] B3d. Conferir o **RPM da conta Gemini** antes de subir `VISION_PARALLEL` (5 paralelas × 4 threads = 20 chamadas simultâneas no pico)
- [x] B4. Resposta parcial explícita: `complete: false` / `truncated: true` + `WARNING` no log; nunca `success: true` mudo
- [x] B5. `MAX_VISION_PAGES`, `VISION_PARALLEL`, `GEMINI_TIMEOUT`, `MIN_TEXT_THRESHOLD` por env
- [x] B6. `VISION_PROMPT` de **transcrição literal**; análise da imagem em campo separado (`image_analysis`). ⚠ **Correção de rumo:** a versão inicial proibia diagnóstico e desligava a descrição por padrão — os dois errados. O defeito nunca foi a análise existir, foi ela sair **misturada** com a transcrição; separada, o defeito já está corrigido, e desligá-la só apagaria informação da página que é só foto de exame. Por decisão do Dr. Alain (06/08), **hipótese diagnóstica é permitida** no campo de análise, desde que não feche diagnóstico e mantenha os diferenciais em aberto. A transcrição segue literal — é a cópia fiel do documento
- [x] B7. Ler `finish_reason` / `prompt_feedback` e distinguir safety de `MAX_TOKENS` e de vazio. **Ficou mais urgente por causa do B6:** transcrição + seção de análise é saída bem mais longa, então truncamento virou risco real — e truncado é pior que vazio, porque o texto volta cortado no meio parecendo completo. `VISION_MAX_OUTPUT_TOKENS` agora é explícito (não depende do default do modelo, que muda quando o alias troca), página truncada entra em `pages_output_truncated` e derruba o `complete`
- [ ] B8. **Medir num DUTCH real de 16 páginas**: as páginas de gráfico são classificadas `native` ou `vision`? Só então decidir entre subir o cap e tratar conteúdo vetorial
- [ ] B9. Minio: validar antes de gravar; falha vira erro explícito; chave **pseudonimizada** (sem telefone); resolver a corrida do bucket
- [ ] B10. Testes: exceção real de render e de `get_text`, duas extrações concorrentes, deadline estourado → resposta parcial, cap → `truncated: true`, Minio falhando

### Lote C — Modelo, dependências, API (P1/P2)

- [ ] C1. Pinar `VISION_MODEL` em ID explícito (fim do alias)
- [ ] C2. Medir thinking numa amostra anonimizada (`thoughts_token_count`, qualidade do OCR) e **só então** fixar o nível
- [ ] C3. Retry **depois** do deadline existir (B3): respeitar `Retry-After`, orçamento total de tentativas somando primário + fallback. *(Nota: verifiquei em `_api_client.py:531` que `retry_args(None)` devolve `stop_after_attempt(1)` — a SDK **não** repete sozinha. `MAX_RETRY_COUNT=3` só vale para upload de arquivo.)*
- [ ] C4. Lock completo: versões exatas **com transitivas e hashes**, e imagem Docker pinada por **digest** — ⚠ pinar só os diretos não torna o build reprodutível
- [ ] C5. Congelar o conjunto atual primeiro; upgrades (PyMuPDF 1.28, google-genai 2.16, flask 3.1.3, flask-limiter 4.x, gunicorn 26.x) em **PR separado**, um por vez
- [ ] C6. Handlers de erro **JSON** para 400/404/405/413/415/429 — hoje só o 500 e o 400 do `ValueError` são JSON
- [ ] C7. Validar o corpo: rejeitar JSON escalar/lista (hoje `.get()` num `list` → 500) e `type` não-string (`type: 123` → `.lower()` → 500); `type` restrito a `{pdf, docx}`
- [ ] C8. `_detect_type`: confirmar estrutura DOCX (junto com A5, que é o que de fato protege)
- [ ] C9. Resposta DOCX coerente: sem `total_pages: 1` falso; sinalizar DOCX com imagem/scan que não passou pelo Vision em vez de devolver texto vazio como sucesso
- [ ] C10. Símbolos clínicos: ⚠ **NFC não unifica `µ` (U+00B5) e `μ` (U+03BC)** — confirmei empiricamente; NFKC unifica mas destrói `m² → m2`. Usar mapeamento dirigido, não normalização cega
- [ ] C11. Conferir a cadeia de proxies do Dokploy e restringir a porta 5050 ao Traefik (`ProxyFix(x_for=1)`)
- [ ] C12. Resolver a situação de licença do PyMuPDF (AGPL vs comercial Artifex) e documentar
- [ ] C13. `tests/test_pure_functions.py:70` resolve `example.com` de verdade — mockar `socket.getaddrinfo`
- [ ] C14. Atualizar `CLAUDE.md` e `README.md` — ⚠ **a conta de pior caso do timeout está errada lá** (§4.2), e a premissa de thread-safety do lock também (§4.3)

### Lote D — Spikes medidos (nenhum entra no mesmo release dos Lotes A/B)

- [ ] D1. Conteúdo vetorial: somar `get_drawings()` gera falso positivo com borda, tabela e fundo — precisa de heurística e corpus real de validação
- [ ] D2. Classificação: substituir o limiar de 50 caracteres por sinal composto (palavras, proporção de imprimíveis, blocos)
- [ ] D3. DPI adaptativo e PNG vs JPEG, medidos em laudo real
- [ ] D4. `media_resolution` por `Part` — ⚠ a SDK aceita o argumento, mas é experimental e ligada a `v1alpha`, que o cliente atual não configura; **verificar antes de usar**
- [ ] D5. PDF nativo para o Gemini (até 1000 páginas, texto nativo não cobrado) — trocaria render, executor e cap por uma chamada, mas perde controle por página e o fallback nativo
- [ ] D6. Saída estruturada (`response_schema`) para o `interpretar_exames.py` consumir sem parsing frágil

---

## 6. Critérios de aceitação

1. Redirect para IP privado, redirect encadeado e `Location` relativo malicioso são **bloqueados** — com teste.
2. Corpo acima do limite devolve **413 em JSON**; base64 acima do cap devolve erro **antes** de decodificar.
3. PDF com `MediaBox` extremo, DOCX-bomb e PDF de milhares de páginas são recusados **sem derrubar o worker**.
4. PDF com uma página que quebra no render **ou** no `get_text` devolve 200 com aquela página em `failed_pages` e o resto íntegro.
5. Nenhuma request ultrapassa o deadline configurado; ao estourar, volta **200 parcial explícito**, nunca `success: true` mudo.
6. Nenhuma chamada a PyMuPDF acontece dentro de um `ThreadPoolExecutor`.
7. `VISION_MODEL` é ID pinado; o nível de thinking foi escolhido por **medição registrada**, não por padrão.
8. O prompt não pede inferência; se houver descrição visual, ela vem em campo separado e rotulado.
9. Minio: nada é gravado antes da validação, falha de upload é erro explícito, e a chave não contém telefone.
10. Toda resposta de erro é JSON — incluindo 404, 405, 413, 415 e 429.
11. `requirements` com lock de transitivas e hashes; imagem por digest. ⚠ O critério da v1 ("dois builds instalam as mesmas versões") **só é atingível com o lock completo**.
12. Suíte verde, com cobertura nova para cada item dos Lotes A e B.

---

## 7. Riscos

| Risco | Mitigação |
|---|---|
| Lote B (threading + deadline) é o mais invasivo do PRD | Fazer **depois** do Lote A, sozinho, com teste de concorrência antes e depois |
| Tirar o render do executor pode piorar a latência | O render é 50-200ms contra 5-30s do Gemini; medir, mas a expectativa é ruído |
| N workers multiplica o rate limit in-memory | Medir memória por worker; se for para >1 worker, o Redis entra no escopo |
| Allowlist de domínios quebra caso de uso legítimo | Levantar os domínios que o n8n realmente usa **antes**; começar em modo log |
| Baixar o thinking piora o OCR | C2 é medição, não decisão cega — o padrão só muda com resultado na mão |
| Upgrade de dependência quebra em produção e não no teste | Um pacote por PR; a suíte é offline e **não cobre rede real** — validar com PDF real antes do deploy |

**Rollback:** os lotes são independentes e revertíveis por commit. C1/C2 são reversíveis por env
var, sem deploy.

---

## 8. Notas de método

**Divergências entre as duas análises, resolvidas na fonte:**

| Ponto | Resolução |
|---|---|
| "A SDK já faz retry automático em 429/5xx" (crítica externa) | **Falso** para o nosso caminho. `_api_client.py:531`: `retry_args(None)` → `stop_after_attempt(1)`. Retry precisa ser ligado. |
| "O gunicorn mata a request aos 480s" (minha v1) | **Falso.** `gthread.py` chama `notify()` incondicionalmente — não há teto de tempo por request hoje. |
| "NFC unifica `µ` e `μ`" (minha v1) | **Falso**, testado: NFC preserva os dois; NFKC unifica mas converte `m² → m2`. |
| Para qual modelo `gemini-flash-latest` aponta e qual o thinking padrão | **Não resolvido** — as fontes divergem (3.5 vs 3.6; medium vs high). Vira medição (C2), e reforça o pin (C1). |
| Suíte tem 55 ou 70 testes | **55 funções `def test_`**; parametrize eleva os casos coletados. As duas contagens estão certas, em unidades diferentes. |

**Ambiente:** a suíte não rodou nesta máquina — o `python3` padrão (3.14, Homebrew) não tem
`flask` nem `fitz`. Antes do Lote A, criar venv e confirmar **baseline verde**, senão não há como
saber se uma quebra veio da mudança ou já existia.

**Limite dos testes atuais:** são unitários com PDF sintético e Gemini falso. Não cobrem rede
real, redirect, concorrência, JBIG2/JPX, vetor, PDF gigante, ZIP bomb, Unicode clínico nem Minio.
O corpus anonimizado de laudos reais (B8) é o que falta para validar qualidade de verdade.
