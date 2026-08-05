# PDF Hybrid Extractor

Extrai texto de PDFs de forma híbrida:
- Páginas com texto → extração normal
- Páginas com imagens → Gemini Vision AI

## Variáveis de Ambiente

| Variável | Descrição |
|----------|-----------|
| `GEMINI_API_KEY` | API key do Google Gemini |
| `PDF_EXTRACTOR_TOKEN` | Token de autenticação para requests |
| `ALLOWED_DOWNLOAD_HOSTS` | Opcional. Lista de **sufixos** de host permitidos, separada por vírgula (ex: `backblazeb2.com,temp-file.download`). Sufixo cobre `f004`/`f005` sem quebrar em migração de bucket. Vazio = desligado. |
| `ALLOWED_DOWNLOAD_HOSTS_ENFORCE` | Default `false`. Com a lista preenchida e isto `false`, host fora da lista só gera WARNING no log e passa. Virar `true` só depois de observar o tráfego real — aí fecha SSRF por redirect **e** DNS rebinding. |

Demais variáveis de ajuste (limites de tamanho, páginas, render, ZIP, timeouts) estão
documentadas no `CLAUDE.md`.

## Endpoints

### `GET /health`
Health check.

### `POST /extract`
Extrai texto de PDF.

**Headers:**
- `Authorization: Bearer <PDF_EXTRACTOR_TOKEN>`
- `Content-Type: application/json`

**Body:**
```json
{
  "url": "https://...",
  "telefone": "5511..."
}
```

**Response:**
```json
{
  "success": true,
  "total_pages": 3,
  "pages_with_vision": 2,
  "text": "..."
}
```

## Deploy no Dokploy

1. Criar serviço vinculado a este repo
2. Configurar variáveis de ambiente
3. Network: `dokploy-network`
4. Porta: `5050`
