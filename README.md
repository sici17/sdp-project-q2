# AROL Service Portal

`Project Q2`: Multi-Agent AI Framework for Industrial Fleet Management and Autonomous Troubleshooting

Design, evaluation and limitations: [DOCUMENTATION.md](DOCUMENTATION.md).

## Run

Requires Docker Engine. From the repository root execute the following:

```bash
cp .env.example .env          #to generate the .env file
docker compose -f infrastructure/docker-compose.yml --env-file .env up --build 
```

First it builds the images, downloads the models (`nomic-embed-text`,
 `qwen2.5:3b-instruct`, which is skipped when
`LLM_PROVIDER=deterministic`) and indexes the eight manuals. 

The frontend starts last, so once http://localhost:5173 loads, the platform is up and running.

- From a phone on the same network, open
  `http://192.168.1.128:5173`
- Sign in by choosing a user (there is no password since Keycloak integration is not implemented). To compare the three
  visibility levels, use Elena Fabbri (full), Matteo Bonetti (technician) and
  Davide Ranieri (commercial), all at Valgrande Bevande S.p.A.
- `docs/qr/` has a QR code per machine, pointing at `http://192.168.1.128:5173`.
- The secrets in `.env.example` are placeholders for demo purposes they can be used



## Services

| Service | Role | Port |
| --- | --- | ---: |
| `frontend` | React app; its nginx proxies the API and the manual PDFs | 5173 |
| `gateway` | Node.js API: sign-in, access control, audit, manual PDFs | 8080 |
| `ai-service` | FastAPI and LangGraph: routing, agents, answers | 8000 |
| `telemetry-mcp` | Telemetry and alarm queries | 8090 |
| `business-mcp` | Quotes, revisions, orders, maintenance tickets | 8091 |
| `doc-mcp` | Manual search, one machine at a time | 8092 |
| `qdrant` | Vector index of the manuals | 6333 |
| `ollama` | Embedding and chat models | 11434 |


## Execution parameters

Set these in `.env`. The rest of the configuration (paths, service URLs,
retention periods) is fixed in `infrastructure/docker-compose.yml`, and
`.env.example` lists a few further options.

| Variable | Default | Effect |
| --- | --- | --- |
| `LLM_PROVIDER` | `ollama` | `ollama`: a local model rewrites answers into prose. `deterministic`: answers are sent as composed, with no model. `azure_openai`: an Azure OpenAI deployment rewrites them. |
| `OLLAMA_CHAT_MODEL` | `qwen2.5:3b-instruct` | Chat model for `ollama`. |
| `AZURE_OPENAI_ENDPOINT`, `_DEPLOYMENT`, `_API_KEY`, `_API_VERSION` | empty; version `2024-10-21` | Required for `azure_openai`. |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Embedding model for manual search; changing it needs a re-index. |
| `DOC_INGEST_RESET` | `0` | `1` rebuilds the manual index on the next start; set it back to `0` afterwards. |
| `PLATFORM_TODAY` | `2026-08-05` | The date treated as today. The dataset ends on 2026-08-04, so overdue work and quote expiry are judged against this date. |
| `GATEWAY_AUTH_MODE` | `demo` | `demo`: choose a user. `oidc`: sign-in through an OpenID Connect provider such as Keycloak. `off`: no sign-in at all; for tests only. |
| `OIDC_ISSUER`, `OIDC_AUDIENCE`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_REDIRECT_URI` | empty | For `oidc`. The gateway is not ready until the issuer, audience, client ID and redirect URI are set. |
| `GATEWAY_MACHINE_IDS` | all eight machines | Machines this deployment serves; empty means all. |
| `AI_SERVICE_SHARED_SECRET`, `MCP_SHARED_SECRET`, `GATEWAY_CSRF_SECRET` | placeholders | Service-to-service secrets and the CSRF signing key. |
| `PUBLIC_ORIGIN` | `http://localhost:5173` | Allowed CORS origin. Leave it for phone access: the app reaches the API through the frontend's proxy. |
| `FRONTEND_BIND_ADDRESS` | `0.0.0.0` | `127.0.0.1` hides the app from other devices. |
| `AI_SERVICE_TIMEOUT_MS`, `AI_SERVICE_STREAM_IDLE_TIMEOUT_MS` | `200000` | How long the gateway waits for the AI service. |
| `GATEWAY_AUTH_COOKIE_SECURE`, `GATEWAY_HSTS` | `false` | Set both to `true` when serving over HTTPS. |

## Dataset formats

The inputs are the workbook
`requirements/AROL_Q2_synthetic_fleet_dataset.xlsx` and eight manuals named
`requirements/manuals/<serialNumber>_manual_EN.pdf`. 
`scripts/convert_dataset.py` turns the  excel workbook into
the two files in `data/`, which are what the services read.

`data/arol_q2.sqlite` has one table per workbook sheet:
The gateway, the Telemetry and Business MCP servers read this database while the AI service
gets records only through MCP.

`data/manuals-manifest.json` has one entry per machine: `machineId`,
`serialNumber`, `fileName`, `sourceUri`, `title`, `language`, `version`, a
`machine` object, and `printedPageOffset`.

The manual index is the Qdrant collection `manual_chunks`: 1,200-character
chunks overlapping by 160, embedded with `nomic-embed-text`, each tagged with
its machine ID, file and page. Manuals already indexed are skipped on restart;
after replacing a manual or changing the embedding model, start once with
`DOC_INGEST_RESET=1`.