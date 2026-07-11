# Exchange Mail MCP Connector

Self-hosted **MCP (Model Context Protocol) + REST** connector for
**Microsoft Exchange** (corporate mail + calendar) over EWS, plus a
local-file RAG index for downloaded attachments.

> **What this is.** A small Python service that exposes Exchange as
> stable, agent-friendly MCP tools and a FastAPI REST layer. It is
> **not** a generic Outlook client and it is **not** a hosted SaaS.
> You run it yourself, inside or next to your corporate VPN.

---

## Why this exists

Exchange via EWS is hostile to AI agents:

- One wrong auth attempt against a corporate NTLM account can **lock
  the user out**. The connector therefore refuses to authenticate
  unless a VPN tunnel to the corporate network is up.
- Real Exchange `item.id` + `changekey` pairs are **sensitive**: they
  let any holder fetch the original. The connector exposes only
  **opaque `message_ref`** values; raw IDs never leave the service.
- `reply` by item id only works if the inbox is well-known; the
  connector maintains a local SQLite index so replies work across
  archive folders and across restarts.
- Calendar operations must round-trip in the user's timezone — the
  connector pins everything to `Europe/Moscow` for predictable behaviour.

This connector fixes all of those for Exchange.

---

## Architecture

```text
┌────────────────────────────┐
│  MCP clients (agents)      │
│                            │
│  stdio: mcp_server.py      │     HTTP: FastAPI main.py
│  (official Python SDK)     │     (REST + OpenAPI at /docs)
└────────────┬───────────────┘
             │
             ▼
┌────────────────────────────────────────────────────┐
│  Service layer                                     │
│  exchange_service.py           → VPN gate, health  │
│  exchange_mail_service.py      → EWS send/reply    │
│  exchange_attachment_service.py→ attachments       │
│  exchange_rag_service.py       → local-file RAG    │
│  exchange_storage.py           → SQLite cache      │
└────────────┬───────────────────────────────────────┘
             │
   ┌─────────┴───────────┐
   ▼                     ▼
 exchangelib / EWS     rag_data/ (local files)
 (requires vpn0)       (attachments for RAG)
```

- The MCP server is a thin client over the REST service on
  `http://127.0.0.1:5015`.
- All Exchange access goes through `exchangelib` with NTLM.
- **VPN gate**: `/ready` returns `503 VPN_REQUIRED` if the
  `vpn0` interface is down. No auth attempt is made in that state.
- **Opaque refs**: public `message_ref` and `attachment_id` are
  server-side hashes. Raw `item.id` / `changekey` are kept only in
  SQLite and never returned in responses.

---

## MCP tools (stdio)

The `mcp_server.py` exposes **19 tools**:

### Mail — read / readiness

| Tool                              | Purpose                                                |
|-----------------------------------|--------------------------------------------------------|
| `work_mail_health`                | Liveness                                               |
| `work_mail_ready`                 | VPN up, DNS OK, route OK, account cached, ready        |
| `work_mail_list_folders`          | Folder tree (`root` or `archive`)                      |
| `work_mail_list_messages`         | List by folder / since / until / limit / offset        |
| `work_mail_search_messages`       | Search by query / sender / subject / date / has_attach |
| `work_mail_get_message`           | Read one mail by `message_ref` or raw `item_id`        |

### Mail — destructive

| Tool                              | Purpose                                                |
|-----------------------------------|--------------------------------------------------------|
| `work_mail_send_message`          | New mail (cc / bcc / attachments)                      |
| `work_mail_reply_message`         | Reply in-thread by `message_ref`                       |

### Attachments

| Tool                              | Purpose                                                |
|-----------------------------------|--------------------------------------------------------|
| `work_mail_list_attachments`      | List attachments for a message                         |
| `work_mail_download_attachment`   | Download to local dir, return safe path               |

### RAG (local-file index)

| Tool                              | Purpose                                                |
|-----------------------------------|--------------------------------------------------------|
| `work_rag_index_documents`        | Index files under a root directory                     |
| `work_rag_search_documents`       | Search the local index                                 |
| `work_rag_get_document`           | Read one indexed document                             |

### Calendar

| Tool                              | Purpose                                                |
|-----------------------------------|--------------------------------------------------------|
| `work_calendar_get`               | Calendar view for a date                               |
| `work_calendar_search`            | Search events in a window                              |
| `work_calendar_add`               | Create an event                                        |
| `work_calendar_delete`            | Delete an event                                        |

### Ops

| Tool                              | Purpose                                                |
|-----------------------------------|--------------------------------------------------------|
| `work_mail_download_status`       | Background dataset download status                     |
| `work_mail_start_dataset_download`| Trigger heavy background dataset download              |

All tool names are stable; agents can depend on them.

---

## REST API

FastAPI service listens on `0.0.0.0:5015` by default. Full OpenAPI
schema at `/docs`.

Highlights (the full set is in the OpenAPI schema):

- `GET /health` — process liveness
- `GET /ready` — VPN + DNS + route + auth-cache status
- `POST /list_messages`, `/search_messages`, `/get_message`
- `POST /send_message`, `/reply_message`
- `POST /list_attachments`, `/download_attachment`
- `POST /index_rag_documents`, `/search_rag_documents`, `/get_rag_document`
- Calendar endpoints under `/get_calendar`, `/search_calendar`, `/add_event`, `/delete_event`
- Legacy `/check_archive_new`, `/reply_work_email`, `/send_work_email`,
  `/sync_emails`, `/list_folders`, `/download_dataset`, `/download_status`

---

## Configuration

All credentials live in `.env` (gitignored). Copy `.env.example` to start:

```bash
cp .env.example .env
$EDITOR .env
```

```env
EXCHANGE_USER=DOMAIN\username
EXCHANGE_PASS=
EXCHANGE_EMAIL=user@example.com
EXCHANGE_SERVER=exchange.example.com
CORS_ORIGINS=http://localhost:3000,http://localhost:8080
```

> ⚠️ `EXCHANGE_PASS` is a real corporate credential. Use a service
> account if your organisation allows it. Never commit `.env`.

---

## Install & run

```bash
# 1. create venv
python3 -m venv .venv
. .venv/bin/activate

# 2. core deps
pip install -r requirements.txt

# 3. MCP SDK
pip install -r requirements-mcp.txt

# 4. bring up the corporate VPN interface (vpn0)
#    ...your usual corporate VPN client...

# 5. run REST
python main.py                # production
# or
uvicorn main:app --reload     # development

# 6. run MCP stdio (called by agent host)
python mcp_server.py
```

### VPN requirement

The connector refuses to authenticate against Exchange unless `vpn0`
is up. A typical readiness flow:

```text
vpn0: DOWN     → /ready returns 503 VPN_REQUIRED, no auth attempt
vpn0: UP       → /ready probes DNS + route + cached account
auth cached    → /ready returns ready=true
auth not cached→ /ready performs one safe auth, then caches for 300s
```

This prevents account-lockout storms when the VPN flaps.

### Docker-aware defaults

If `EXCHANGE_CONNECTOR_BASE_URL` is unset and the process runs
inside a container, the MCP server defaults to:

1. `http://host.docker.internal:5015` if that hostname resolves, else
2. `http://172.17.0.1:5015` (Linux Docker bridge gateway).

Outside Docker the default stays `http://127.0.0.1:5015`. The REST
service should still run on the host where the VPN is reachable.

---

## Message model

Public `message_ref` values are **opaque hashes** of `(folder, item_id,
changekey)`. Raw Exchange identifiers are kept only in SQLite and are
never returned in responses.

```jsonc
{
  "message_ref": "work:inbox:abcdef1234...",
  "folder": "inbox",
  "received_at": "2026-07-11T13:45:17+03:00",
  "from": {"name": "...", "email": "..."},
  "to":   [{"name": "...", "email": "..."}],
  "subject": "...",
  "snippet": "...",
  "has_attachments": true
}
```

---

## Safety

- **VPN gate** before any NTLM auth attempt.
- **Account cache** (TTL 300 s) avoids hammering the Exchange server.
- **Filename sanitization** for attachments: dangerous names like
  `../evil.txt` are normalized to `evil.txt` in the API response, in
  the SQLite row, and on disk.
- **Path containment** for the RAG index: symlinks and files outside
  the configured root are skipped.
- **Audit log** for destructive mail and calendar actions. Raw
  Exchange IDs are not stored in audit rows.
- **No secret value** is logged.
- **TLS verification** is intentionally disabled for the EWS endpoint
  with a custom `NoVerifyHTTPAdapter` because some corporate Exchange
  deployments ship with self-signed internal certificates. Replace
  this with your org's CA bundle in production if possible.

---

## Tests

```bash
.venv/bin/python -m pytest -q
```

The suite covers service logic, REST routes, MCP wrappers, attachment
safety, RAG containment, and the VPN-down gate. **No test connects
to a real Exchange server** — all network calls are stubbed.

---

## Repository layout

```
.
├── main.py                       # FastAPI app + REST endpoints
├── exchange_service.py           # VPN gate, health, readiness
├── exchange_mail_service.py      # send / reply service layer
├── exchange_attachment_service.py# attachment list + download
├── exchange_rag_service.py       # local-file RAG index
├── exchange_storage.py           # SQLite cache + audit
├── exchange_connector.py         # local HTTP wrapper used by agents
├── mcp_server.py                 # MCP stdio server
├── requirements.txt
├── requirements-mcp.txt          # MCP SDK only
├── tests/
└── .env.example
```

---

## License

MIT.