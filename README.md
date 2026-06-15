# Ometrics AI Report Assistant

First implementation step for the course project: a read-only Streamlit chatbot
that uses LangChain tool calling to answer questions through the existing
`omreports` service.

## Current capabilities

- Uses OpenRouter through LangChain's OpenAI-compatible client.
- Defaults to `openai/gpt-5-mini`; the model is configurable.
- Translates natural-language date ranges into exact report dates.
- Lists the available reports.
- Runs one report for a selected site and period.
- Retrieves two report periods for comparison.
- Retrieves raw readings for one date.
- Compares raw readings between two dates.
- Finds assets missing daily readings for supported reading types.
- Displays tool arguments and results in the UI.
- Validates report names, dates, site IDs, and maximum date ranges.

This version does not use RAG yet. Report questions go through `omreports`;
raw-reading questions use read-only, allowlisted SQL against the Ometrics
database.

## Setup

Create a virtual environment and install dependencies:

```bash
cd /home/mo/Projects/omai
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

The project uses a `src` package layout. Running `streamlit run app.py` from the
project root works without an editable install because `app.py` adds `src` to
the import path.

Set `OPENROUTER_API_KEY` in `.env`.

Set the `DB_*` values in `.env` if you want to use raw-reading tools. Prefer a
read-only MySQL user.

Start the existing report service in another terminal:

```bash
cd /home/mo/Projects/omreports
source venv/bin/activate
python3 -m uvicorn api:app --host 127.0.0.1 --port 50008
```

Start the chatbot:

```bash
cd /home/mo/Projects/omai
source .venv/bin/activate
streamlit run app.py
```

Select the correct site ID in the sidebar before asking report questions.

Example questions:

- Show oil production from the first of this month through today.
- Compare water production this month with last month.
- Show LACT readings for 2026-06-10.
- Compare tank readings on 2026-06-09 and 2026-06-10.
- Which water plant readings are missing for 2026-06-10?

## Tests

```bash
pytest -q
```

The unit tests validate the report request boundary and do not require a live
database or OpenRouter key.
