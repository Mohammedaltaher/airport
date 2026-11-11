# DMA — Airport Queue and Staffing Assistant

Team DMA's historical airport management dashboard and Gemini assistant, built
with FastAPI, pandas, SQLite, Astro, Tailwind CSS and Chart.js. All airport
records and policies are fictional training materials.

## Start the application

Requirements: Python 3.11+ and Node.js 22+. Run from the repository root.
The app reads the supplied database and documents; it does not copy or modify them.

```powershell
# On a machine with a working Python installation:
py -3 -m venv dma/.venv
& ./dma/.venv/Scripts/python.exe -m pip install -r dma/backend/requirements.txt
Push-Location dma/frontend
npm ci
Pop-Location
Copy-Item dma/.env.example dma/.env
& ./dma/start.ps1
```

Open **http://127.0.0.1:4321**. FastAPI documentation is at
**http://127.0.0.1:8000/docs**. The development script starts the backend in a
hidden process and stops it when the frontend exits.

For this workspace, the script also detects the working Python runtime in
`labs/day1_lab3/.runtime/`. Its dependencies were installed during implementation.
This fallback is local convenience; team members should create their own virtual
environment. No runtime binaries or secrets belong in source control.

To run each service separately:

```powershell
# Terminal 1 (substitute the local lab Python if needed)
& ./dma/.venv/Scripts/python.exe -m uvicorn app.main:app --app-dir dma/backend --host 127.0.0.1 --port 8000
# Terminal 2
Set-Location dma/frontend
npm run dev
```

To serve a built frontend through FastAPI:

```powershell
Push-Location dma/frontend
npm run build
Pop-Location
& ./dma/start.ps1 -Mode built
```

Open **http://127.0.0.1:8000** in built mode. Restart the backend after initially
creating the frontend build. Astro dev proxies `/api` to FastAPI; both built
frontend and API use the same origin. Astro's separate `preview` command only
previews static files; use built mode for an integrated preview.

## Data and configuration

Copy `.env.example` to `.env`; environment variables override the file. Relative
source paths are resolved against `dma/`, independent of the working directory.

| Setting | Purpose |
|---|---|
| `DMA_DATABASE_PATH` | Defaults to `backend/data/database/airport_ops.db`. |
| `DMA_CHAT_DATABASE_PATH` | Separate writable SQLite chat store, created automatically at `backend/data/chat/history.db`. |
| `DMA_DOCUMENTS_PATH` | Defaults to `backend/data/documents`. Requires the document register and TXT files. |
| `DMA_POLICY_INDEX_PATH` | Local LlamaIndex vector index cache; defaults to `backend/data/policy_index`. |
| `GEMINI_EMBEDDING_MODEL` | Retrieval embedding model; defaults to `gemini-embedding-001`, using the existing Gemini API key. Requires retrieval-document/query task support. |
| `GEMINI_API_KEY` | Server-side Gemini credential. Never sent to the browser. |
| `GEMINI_MODEL` | An available model ID from your Gemini account; no guessed default. |
| `DMA_ALLOWED_ORIGINS` | Comma-separated development origins, localhost and 127.0.0.1 on port 4321 by default. |

Place the supplied database at `backend/data/database/airport_ops.db` and the
policy documents in `backend/data/documents/`.

The dashboard works without Gemini. Assistant submission remains disabled until
both Gemini settings are present. Restart FastAPI after editing configuration.
Configured credentials do not prove model access; a provider error is shown
explicitly and never replaced with a simulated answer.

## Dashboard workflow

The default view is Terminal 1 departure immigration, September 2026, all
weekdays, 17:00–22:59. Apply filters to refresh the dashboard, export and assistant
scope. Clicking a heatmap cell selects that weekday and hour. Filter changes
must be applied before affecting the assistant. Each conversation message shows
its scope. Use the expand button in Ask DMA for a full-screen workspace with a
conversation sidebar and New chat. Successful exchanges, filters and retrieved
sources are saved in the separate chat database. Reopening a conversation restores
its last scope; choose **Use dashboard filters** to switch back to the dashboard.
Follow-ups include the last six exchanges as context and retrieve fresh evidence.

History uses a random browser profile identifier stored in localStorage, not user
accounts. The same browser and site origin can reopen its chats after a restart;
clearing browser storage loses that link. This identifier is a bearer credential,
not a sign-in system. Add authentication before exposing the app to untrusted users.
Failed provider requests are displayed but are not saved as completed exchanges.

Metrics show selected-period averages, the percentage waiting over 30 minutes,
estimated passengers affected and roster coverage. Review monthly compliance and
staffing groups below the charts. Use the type tabs, attention filter and Show
more button, or download the complete filtered CSV. Incident overlaps and
historical escalation candidates are displayed separately.

## Architecture and API

`backend/app` contains configuration, validated filters, parameterised read-only
data access, pandas aggregation, explicit policy rules, clause retrieval and the
bounded Gemini tool loop. `frontend/src` contains the Astro page, Tailwind styles,
TypeScript state and Chart.js visualisations.

| Endpoint | Result |
|---|---|
| `GET /api/v1/health` | Database and assistant configuration state. |
| `GET /api/v1/metadata` | Available date bounds, terminals, weekdays and sample basis. |
| `GET /api/v1/dashboard` | Metrics, charts, heatmap, findings, incidents and escalation candidates. |
| `GET /api/v1/findings.csv` | Complete findings with policy references and exclusions. |
| `POST /api/v1/assistant` | Gemini answer, retrieved clauses, filters, tool names and limitations. |

The dashboard and CSV accept `terminal` (`T1`, `T2`, `ALL`), ISO `date_from` /
`date_to`, `weekday` (`All` or an English weekday), and inclusive `hour_from` /
`hour_to` (0–23). Reversed ranges and periods above 731 days return HTTP 422.
Assistant requests contain `question` (3–2000 characters) and the same `filters`
object. OpenAPI documents the full validation contract.

The assistant receives verified baseline statistics and date-filtered policy
clauses. It may call queue, staffing, and incident tools for the selected period,
a policy tool that answers from those clauses, and a read-only SQL tool for other
measured questions. The SQL tool accepts one `SELECT` or `WITH` statement, opens
only the airport database read-only, and returns at most 30 rows. The loop allows
five rounds, with at most four executed tools per round, then must answer.
The overall deadline is 60 seconds.
English and Arabic questions are supported; policy sources are in English.
Policy RAG uses LlamaIndex `TextNode` clause chunks and `VectorStoreIndex` semantic
retrieval, with Gemini embeddings through a `BaseEmbedding` adapter for the existing
Google SDK. No OpenAI key or external vector database is required. Drafts are not
indexed; date-eligible node IDs restrict vector search before the top 14 results
are selected. Required measurement and exception clauses are then appended.
The policy tool answers from retrieved clauses under its own prompt and cites
the document and section. Analytics rules remain explicit Python functions.

The index builds on the first policy question and persists locally. Cache keys
include the policy content, metadata and embedding model; changing those creates
a fresh index on the next backend restart. Keep the cache directory writable. The first request can take
longer while documents are embedded, and Gemini embedding access/quota is required.
Successful embedding batches are cached immediately. If an embedding rate limit
returns HTTP 429, retry after a minute; indexing resumes from the saved batches.
An embedding error is reported explicitly; there is no silent keyword fallback.
Blocking embedding calls run in worker threads under the assistant deadline.

## Measurement and policy decisions

- Passenger journeys are a 4% sample. Counts of waits above 30 minutes are
  multiplied by 25; averages and percentages are never scaled. Smart-gate users
  remain included. Measured and total sample counts are available separately.
- Time-of-day uses `immigration_hour`. Counter statistics are computed on unique
  checkpoint-date-hour records, independently of passenger counts.
- Missing roster hours remain unknown and are excluded from open-counter
  averages and shortfall checks. Coverage is against expected selected
  checkpoint-hours within the dataset period.
- Queue compliance groups are calendar month, terminal, weekday and hour.
  Partial months are labelled provisional. Comparisons use full precision;
  displayed rounding does not alter a breach decision.
- GIA-SUP-12 applies through 31 December 2025 (20-minute average, 10% tail,
  peak 18:00–22:59); GIA-POL-01 applies from 1 January 2026 (15-minute average,
  5% tail, peak 17:00–22:59). The average target applies outside peak hours too;
  the tail target applies in policy peak hours.
- Counter requirements use GIA-STD-03, effective throughout the dataset. Its
  checkpoint minimums can be stricter than the historical passenger policy.
  T1 Friday evenings allow five through 30 June 2026 under EX-2025-07, then
  require nine. Night/early-morning rules override ordinary off-peak minimums.
- Escalation staff-request triggers use >40 minutes for three consecutive hours
  in 2025 and >30 from January 2026. Level 1 (>20 hourly average) and Level 3
  (>45 individual wait) are assessed only from GIA-SOP-06's 1 March 2026
  effective date. Earlier versions of that SOP were not supplied.
- Consecutive-hour analysis retains context before the selected hours and across
  midnight. These are sample-derived historical candidates, not recorded
  operational responses. Flight eligibility for priority lanes is unverified.
- Draft v4.0 is never an applied rule. Archived v2.0 is retrieved only for an
  overlapping historical period. Later-effective clauses are excluded from
  earlier-only retrieval. The policy review date is not treated as an expiry.

## Tests and team handoff

```powershell
& ./dma/.venv/Scripts/python.exe -m pytest dma/backend/tests -q
Push-Location dma/frontend
npm run build
npm test
Pop-Location
```

Tests cover historical rules, exact thresholds, expiry, sample scaling, smart
gates, missing rosters, independent counter averages, cross-month and midnight
cases, incident overlaps, validation, CSV, missing sources, mocked Gemini tool
calls, provider failures and round limits. A real-database integration test
verifies dashboard values against independent SQL queries. Live Gemini requires
the team's credentials; mocked provider tests do not prove account access.
Frontend DOM tests exercise filters, heatmap selection, exports, empty/error
states and assistant rendering with mocked network requests and charts. They
require `npm run build` first and do not replace visual browser inspection.

Suggested team ownership: data/rules and verification; API/Gemini integration;
dashboard and accessibility. Agree changes to API filters and response fields
before changing consumers. Run backend tests and the frontend build before
sharing changes. Dependency inputs are pinned; `package-lock.json` is included.

## Limits and accountability

No authentication, public hosting, live integrations, forecasting, roster
optimisation or automatic staffing changes are included. The data ends in
September 2026; displayed results are historical, not live operational alerts.
Missing observations can conceal queue events. There is no policy-specified
minimum sample size for waiting-time groups. Overall averages do not establish
causes. Overlapping incidents do not prove causality, and a lack of recorded
incidents does not prove no disruption occurred.

Model answers are advice requiring source review. Retrieved clauses are shown
alongside answers; they do not guarantee every model statement is correct.
Tool requests and results are logged to ignored `logs/tools.jsonl` without API
credentials. Query results can make this file grow during demonstrations;
delete or archive local logs between sessions according to team needs. Use
fictional training data only. Managers remain accountable for decisions.
