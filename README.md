# Omai

Ometrics is an oil-field operations platform used to monitor, control, and
report production data. It tracks field readings, tanks, wells, shutdowns,
alarms, work orders, notes, emails, and production reports across a selected
site.

Omai is the read-only AI assistant service for Ometrics. It provides a
LangChain-based chat agent that can answer operational questions by calling the
existing Ometrics and Omreports data paths. The Laravel Ometrics application
uses Omai through a local FastAPI `/chat` endpoint; a Streamlit UI is also
available for local development and debugging. Omai is not intended to run
independently; it is an AI service layer for an existing Ometrics deployment.

## Responsibilities

Omai is responsible for:

- Receiving chat requests from Ometrics and maintaining server-side
  conversation history by `conversation_id`, `user_id`, and `site_id`.
- Using an OpenAI-compatible chat API through LangChain. The model, API key, and
  base URL are configurable, so it can run with OpenAI directly or a compatible
  provider such as OpenRouter.
- Running Omreports functions for report questions, including production,
  sales, gas, water, injection, allocation, and monthly battery summaries.
- Reading Ometrics MySQL data for raw readings, missing readings, comparisons,
  tank volumes, well shutdowns, shutdown-cause summaries, current long
  shutdowns, well timelines, alarms, notes, and work-history context.
- Answering questions about Ometrics capabilities using curated capability
  documents in `knowledge/capabilities`.
- Searching operational text with RAG. Source records are read from MySQL during
  indexing, embedded, and stored in a Postgres + pgvector index.
- Enforcing read-only behavior. Omai can retrieve and summarize data, but it
  does not create records, update records, send emails, acknowledge alarms,
  control equipment, or export files.

## Runtime Dependencies

Omai is not a standalone application. It is designed to run as part of the
Ometrics software bundle.

At runtime it depends on:

- Ometrics MySQL database for site data, readings, shutdowns, notes, alarms,
  well history, and conversation storage.
- Omreports service for calculated production, sales, gas, water, injection,
  allocation, and battery reports.
- A configured LLM provider through an OpenAI-compatible API.
- Postgres + pgvector for operational-text RAG, if RAG questions should be
  supported.

Without the Ometrics database and Omreports service, Omai can start, but most
domain tools will be unavailable or return errors.

## Architecture

- `FastAPI` serves `/health` and `/chat` for Ometrics. The API accepts only
  localhost clients.
- `LangChain` handles model calls and tool calling.
- `omreports` remains the source for calculated report results.
- Ometrics MySQL remains the source of operational records.
- Postgres + pgvector stores the derived operational-text vector index.
- Conversation messages are stored in the Ometrics database through Omai's
  conversation repository.

The public `/chat` response contains only:

```json
{
  "conversation_id": "uuid",
  "answer": "assistant response"
}
```

Tool calls and timing statistics are internal and are not returned by the API.

## Setup

Create a virtual environment and install dependencies:

```bash
cd /home/mo/Projects/omai
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install .
cp .env.example .env
```

Set the LLM provider values in `.env`.

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

Configure the Ometrics MySQL connection for reading operational data and storing
conversation history:

```bash
DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=ometrics_read_user
DB_PASSWORD=secret
DB_NAME=ometrics
```

Configure Omreports:

```bash
OMREPORTS_API_URL=http://127.0.0.1:50008/report/
OMREPORTS_TIMEOUT_SECONDS=30
MAX_REPORT_DAYS=366
```

## Operational RAG

Operational-text RAG uses a derived vector index backed by Postgres + pgvector.
Omai reads source records from MySQL and writes only the normalized chunks and
embeddings to the vector database.

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

## Running Services

Start Omreports:

```bash
cd /home/mo/Projects/omreports
source venv/bin/activate
python3 -m uvicorn api:app --host 127.0.0.1 --port 50008
```

Start Omai API for Ometrics:

```bash
cd /home/mo/Projects/omai
source .venv/bin/activate
python3 -m uvicorn omai.api.app:app --host 127.0.0.1 --port 50009
```

Process queued Omai RAG index events periodically:

```bash
cd /home/mo/Projects/omai
source .venv/bin/activate
omai-process-rag-index-events
```

Optional local Streamlit UI:

```bash
cd /home/mo/Projects/omai
source .venv/bin/activate
streamlit run app.py
```

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

## Tests

```bash
cd /home/mo/Projects/omai
source .venv/bin/activate
pytest -q
```

The unit tests use fakes and in-memory databases where possible. They do not
require a live LLM API, Omreports service, MySQL database, or vector database.
