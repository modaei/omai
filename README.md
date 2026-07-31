# Omai

## Ometrics Ecosystem

Ometrics is an integrated oil-field operations ecosystem. It captures field
readings, monitors wells and facilities, manages shutdowns and alarms, records
operational notes and work history, calculates production and allocation, and
produces management reports and scheduled operational communications. Its shared
operational database is the system of record, and users work through the
authenticated Ometrics interface.

Omai is the read-only AI agent within that ecosystem. It interprets
natural-language operational questions, retrieves scoped data from the existing
systems of record, selects appropriate analysis or reporting tools, and returns
grounded answers through Ometrics. It does not replace field workflows,
reporting, alarms, or data storage.

```text
Field operations and user workflows
        │
        ▼
Ometrics operational data and reporting ecosystem
        │
        ▼
Omai AI agent retrieves, analyzes, and summarizes approved data
        │
        ▼
Grounded answers in Ometrics and scheduled operational outputs
```

Omai cannot run as a supported standalone application. It depends on the
Ometrics database schema, existing operational data, authenticated user and site
context, and report functions. Operational-text questions additionally require
the derived Postgres + pgvector index. The API can start without some of these
dependencies, but most domain capabilities will be unavailable; that is not a
supported deployment model.

The agent is built with LangChain and an OpenAI-compatible chat model. It can
call Ometrics data, reporting, RAG, and validated SQL tools depending on the
user's request.

## Responsibilities

Omai is responsible for:

- Receiving chat requests from Ometrics and maintaining server-side
  conversation history by `conversation_id`, `user_id`, and `site_id`.
- Using an OpenAI-compatible chat API through LangChain. The model, API key, and
  base URL are configurable, so it can run with OpenAI directly or a compatible
  provider such as OpenRouter.
- Acting as a tool-using operational agent: interpreting user intent, selecting
  the best available tool, passing scoped arguments, and synthesizing the result
  into a user-facing answer.
- Running Omreports functions for report questions, including production,
  sales, gas, water, injection, allocation, and monthly battery summaries.
- Reading Ometrics MySQL data for raw readings, missing readings, comparisons,
  tank volumes, well shutdowns, shutdown-cause summaries, current long
  shutdowns, active and producing wells, well tests, well timelines, alarms,
  notes, work-history context, and data-point values and trends.
- Analyzing rod-pump data and operational telemetry trends, including retrieved
  Graphite time-series values, sudden changes, overall trends, and variability.
- Running tightly validated read-only SQL against allowlisted operational
  tables when the specific report, reading, shutdown, timeline, work-order, or
  capability tools are not the best fit for the question.
- Answering questions about Ometrics capabilities using curated capability
  documents in `knowledge/capabilities`.
- Searching operational text with RAG. Source records are read from MySQL during
  indexing, embedded, and stored in a Postgres + pgvector index.
- Producing answers grounded in retrieved operational data, tool results, and
  curated Ometrics capability guidance.
- Enforcing read-only behavior. Omai can retrieve and summarize data, and it can
  prepare navigation to existing Ometrics forms where supported, but it does not
  create records, update records, send emails, acknowledge alarms, control
  equipment, or export files.

## Runtime Dependencies

At runtime it depends on:

- Ometrics MySQL database for site data, readings, shutdowns, notes, alarms,
  well history, and conversation storage.
- Omreports service for calculated production, sales, gas, water, injection,
  allocation, and battery reports.
- A configured LLM provider through an OpenAI-compatible API.
- Postgres + pgvector for operational-text RAG, if RAG questions should be
  supported.

Without the Ometrics database and reporting service, Omai can start, but most
domain tools will be unavailable or return errors.

## Production Architecture

Laravel Ometrics is the production front end. It owns user authentication, the
chat experience, site selection, voice input, response feedback, and navigation
to existing Ometrics workflows. It calls Omai through its localhost FastAPI
service; browsers do not call Omai directly.

- `FastAPI` is Omai's internal backend boundary. It serves `/health`, `/chat`,
  `/weekly-overview`, and `/rod-pump-health-report`, and accepts only localhost
  clients.
- `LangChain` runs the agent loop: understand the request, decide whether a
  tool is needed, call the selected tool with scoped arguments, then compose the
  final answer.
- Deterministic routing handles common high-confidence workflows before a full
  LLM tool-selection loop is needed.
- The reporting service remains the source for calculated report results.
- Ometrics MySQL remains the source of operational records.
- Postgres + pgvector stores the derived operational-text vector index.
- Conversation messages are stored in the Ometrics database through Omai's
  conversation repository. A conversation is owned by its `user_id` and
  `site_id`, and cannot be loaded by a different user or site.
- Scheduled ecosystem workflows can call Omai for weekly operational-overview
  text and rod-pump health analysis without creating a chat conversation.

Typical flow:

```text
Ometrics browser
        │
        ▼
Laravel Ometrics (authentication and UI)
        │
        ▼
Local FastAPI Omai service
        │
        ├─ deterministic router, when a safe direct path exists
        └─ LangChain LLM tool-selection loop
                 │
                 ▼
        Operational data / reporting / RAG / validated SQL tools
                 │
                 ▼
        grounded final answer returned to Ometrics
```

The public `/chat` response contains only:

```json
{
  "conversation_id": "uuid",
  "answer": "assistant response"
}
```

Tool calls and timing statistics are internal and are not returned by the API.
RAG source snippets are also not returned by the API. Assistant message metadata,
including compact tool audit records and SQL audit records, is stored in
`ai_messages.info`.

## Chat Behavior and Safeguards

Each `/chat` request includes the authenticated `user_id`, the selected
`site_id`, a message, and an optional `conversation_id`. Omai validates that a
continued conversation belongs to the same user and site before loading its
history. Conversation expiration and retained-history length are configurable.

The public response modes are `fast` and `intelligent`. They map to medium and
high main-model reasoning effort respectively. Ometrics keeps this choice in the
user interface; it is also recorded with the assistant message for debugging.

The API validates request shape and length, applies a configurable daily usage
limit per user and site, and limits concurrent work. Omai is read-only: it can
prepare navigation to supported Ometrics workflows, but it does not create or
change field records, acknowledge alarms, control equipment, send email, or
export files. Its SQL fallback is constrained to validated, site-scoped,
allowlisted `SELECT` queries, with a separate read-only database user as the
final permission boundary.

## Setup

From a normal clone, enter the repository and install Omai:

```bash
cd omai
python3 -m venv .venv
source .venv/bin/activate
pip install .
cp .env.example .env
```

Set the LLM provider values in `.env`.

Set `LOG_LEVEL` to control Omai logging across the API, CLI commands, and RAG
event processor. Supported values are:

```bash
LOG_LEVEL=CRITICAL
LOG_LEVEL=ERROR
LOG_LEVEL=WARNING
LOG_LEVEL=INFO
LOG_LEVEL=DEBUG
LOG_LEVEL=NOTSET
```

For OpenAI directly:

```bash
LLM_API_KEY=your-openai-key
LLM_MODEL=gpt-5-mini
LLM_BASE_URL=https://api.openai.com/v1
```

For OpenRouter:

```bash
LLM_API_KEY=your-openrouter-key
LLM_MODEL=openai/gpt-5-mini
LLM_BASE_URL=https://openrouter.ai/api/v1
```

Optionally configure a local OpenAI-compatible helper model. It is disabled when
`LOCAL_LLM_MODEL` or `LOCAL_LLM_BASE_URL` is empty. When enabled, it is used only
for operational-context query rewriting, simple RAG summaries, and safe
formatting of deterministic tool results. It is not used for intent
classification, domain rejection, complex agent reasoning, SQL generation,
report analysis, or management summaries; those remain with the main LLM.

```bash
LOCAL_LLM_API_KEY=ollama
LOCAL_LLM_MODEL=qwen2.5:7b
LOCAL_LLM_BASE_URL=http://test.ultimatesys.com:11500/v1
LOCAL_LLM_TIMEOUT_SECONDS=10
```

Configure the Ometrics MySQL connection for reading operational data and storing
conversation history:

```bash
DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=ometrics_read_user
DB_PASSWORD=secret
DB_NAME=ometrics
```

For direct operational SQL, configure a separate database user that has only
`SELECT` privileges on the allowlisted operational tables. Omai also validates
SQL shape, table names, columns, site scoping, and row limits before execution,
but database permissions are the final safety boundary:

```bash
OPERATIONAL_SQL_DB_USER=ometrics_ai_readonly
OPERATIONAL_SQL_DB_PASSWORD=secret
OPERATIONAL_SQL_MAX_ROWS=100
OPERATIONAL_SQL_TIMEOUT_SECONDS=10
```

Configure Omreports:

```bash
OMREPORTS_API_URL=http://127.0.0.1:50008/report/
MONITORING_DATA_API_URL=http://metrics1.ultimatesys.com/render
ROD_PUMP_TIMEOUT_SECONDS=20
ROD_PUMP_MAX_DATA_POINTS=96
ROD_PUMP_BATCH_WORKERS=2
# Leave unset until a paraffin model passes the validation gate.
ROD_PUMP_MODEL_METADATA=
OMREPORTS_TIMEOUT_SECONDS=30
MAX_REPORT_DAYS=366
```

## Operational RAG

Operational-text RAG uses a derived hybrid index backed by Postgres full-text
search and pgvector. Omai reads source records from MySQL and writes only
normalized chunks, search vectors, and embeddings to the vector database.

Configure the vector database:

```bash
VECTOR_DB_HOST=127.0.0.1
VECTOR_DB_PORT=5432
VECTOR_DB_USER=ometrics
VECTOR_DB_PASSWORD=
VECTOR_DB_NAME=ometrics
RAG_EMBEDDING_MODEL=text-embedding-3-small
RAG_EMBEDDING_DIMENSIONS=1536
```

`CREATE EXTENSION vector` must be run once by a privileged Postgres user inside
the same database configured by `VECTOR_DB_NAME`:

```sql
CREATE EXTENSION vector;
```

Then create/update the vector schema and index operational text:

```bash
omai-vector-db-migrate
omai-index-operational-text --site-id 4 --from-date 2026-01-01 --reset-site
```

The indexer includes general notes, chart notes, work orders and notes,
shutdown comments, downtime codes, well-test comments, reading comments, alarm
logs, and well history records.

Ometrics keeps this index current by writing model-change events into the shared
`ai_rag_index_events` MySQL outbox table. Run `omai-process-rag-index-events`
from cron or a systemd timer every few minutes; each run takes a MySQL lock,
processes currently due events, deletes successful rows, and exits. A nightly
cron job can also call the `omai-index-operational-text` command directly for
rolling repair refreshes.

## Running Omai

Ensure the existing reporting service is running and `OMREPORTS_API_URL` points
to it. From the Omai repository root, start the internal API used by Laravel:

```bash
source .venv/bin/activate
python3 -m uvicorn omai.api.app:app --host 127.0.0.1 --port 50009
```

For production, run this command under a process supervisor, bind it to
`127.0.0.1`, and monitor `GET /health`. Restart the service after changing
`.env`. Laravel is the only production caller of the chat endpoint.

Process queued RAG index events from cron or a systemd timer every few minutes:

```bash
source .venv/bin/activate
omai-process-rag-index-events
```

Run `omai-index-operational-text` nightly for a rolling repair refresh when
needed. Both commands take the necessary indexing locks so concurrent runs do
not process the same work.

## Example Questions

- How much gas was flared during May?
- Compare Battery 6 oil production for each month in 2026.
- Which readings are missing on 2026-05-20?
- Compare mixed tank readings on 2026-05-09 and 2026-05-10.
- What happened with 5144H on 2026-06-14?
- What can you tell me about 4293?
- What has been the main cause of shutdowns in the last 30 days?
- Build a timeline for Well 4048 this year.
- How do I register a LACT reading?
- How can I know how much each well contributed to oil production?
- Analyze current load trend of 2510 in July.
- List wells with more than 20 shutdown hours in May 2026.

## Tests

```bash
source .venv/bin/activate
python3 -m pytest -q
```

The unit tests use fakes and in-memory databases where possible. They do not
require a live LLM API, Omreports service, MySQL database, or vector database.

## Local Evaluation

Omai includes a developer-only live evaluation command:

```bash
pip install ".[dev]"
omai-evaluate
```

This command uses the real local Omai configuration: the configured Omai LLM for
answers, Ometrics MySQL database, Omreports service, operational SQL user, and
Postgres + pgvector RAG index. For DeepEval judge calls, Omai maps `LLM_API_KEY`
and `LLM_BASE_URL` into OpenAI-compatible environment variables when they are not
already set. It is not part of normal CI and may consume LLM/API credits.

Useful filters:

```bash
omai-evaluate --limit 3
omai-evaluate --category rag
omai-evaluate --case-id report-gas-flared-may-2026
omai-evaluate --failed-only
```

Use `--current-date YYYY-MM-DD` to supply a default date for cases that do not
define `current_date`. If a case defines `current_date`, the case value wins.

Before running cases, the command validates local DB, vector DB, and Omreports
connectivity. Results are written to `eval-results/omai-eval-*.json`. Each result
contains only:

- `case.id` and `case.question`
- `passed`
- `answer`
- `tool_calls`
- `failed_checks`
- `failed_metrics`

Passing deterministic checks and passing DeepEval metrics are omitted from the
report to keep files small. Timing stats are intentionally not written to eval
reports.

Use `--failed-only` when running larger suites if the report file should include
only failed cases. The top-level `passed` and `total` counts still refer to all
evaluated cases; only the `results` array is filtered.

Evaluation cases live in `src/omai/evals/cases/core.json`. Important fields:

- `expected_tool_calls`: verifies both the tool name and selected argument
  values. This replaces the older `required_tools` style check.
- `forbidden_tools`, `required_phrases`, and `forbidden_phrases`: deterministic
  answer/tool assertions.
- `deterministic: true`: skips DeepEval judge calls for cases where tool calls,
  arguments, and phrases fully define success. This reduces runtime and token
  cost for data-entry and routing regressions.
- `metrics`: DeepEval metrics to run for non-deterministic cases. Supported
  values include `answer_relevancy`, `correctness`, `faithfulness`, and
  `contextual_relevancy`.
- `expected_output`: reference behavior used by LLM-judged correctness-style
  metrics. It is ignored for `deterministic: true` cases.

`expected_tool_calls[].arguments` supports dotted paths and simple matchers:

```json
{
  "tool": "analyze_well_tests",
  "arguments": {
    "analysis_mode": "recent_tests",
    "group_by": "well",
    "well_name": {"contains": "4048"},
    "test_count": 3
  }
}
```

Available matchers are `contains`, `contains_all`, `one_of`, `present`, and
`absent`.
