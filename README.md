# Assignment 2 — Weather-Based Delivery Automation

Yellow.ai AI Automation Internship · Python · OpenWeatherMap · Gemini

**Project folder:** [`weather-delivery-automation/`](weather-delivery-automation/) — full setup,
architecture and example output live in
[`weather-delivery-automation/README.md`](weather-delivery-automation/README.md).

## The business problem

Delivery operations lose customers quietly. When a courier is stuck behind a downpour or a snow
closure, the delay only becomes visible after the customer complains. Support teams then improvise
apologies that are either generic or over-promised, and neither protects trust.

This automation closes that gap. It reads the order queue, checks real weather for every destination
city at the same moment, flags the orders that weather actually affects, and writes a personalised
apology using the real condition — never an invented one.

## What it does

| Capability | Outcome |
|---|---|
| Reads `orders.json` (the four mandatory orders) | No manual queue review needed |
| Fetches weather for every city **concurrently** with `asyncio.gather` on one `aiohttp` session | Four live city lookups completed in **0.120 s**; latency stops scaling with order volume |
| Marks an order `Delayed` only when `weather[0].main` is Rain, Snow or Extreme | Decisions come from the API field, not fuzzy text matching, so there are no false delays |
| Generates the apology with Gemini `gemini-3.5-flash-lite`, falling back to a deterministic template if the AI is unavailable | The customer always gets a message; an AI outage never blocks operations |
| Isolates failures per city | `InvalidCity123` returned a real HTTP 404, was logged, and the other three orders still finished |
| Writes results back atomically (temp file plus `os.replace`) | The order file can never be left half-updated |
| Records `apology_source` on every order | Operations can audit whether a message was AI-written or template-written |

## Evidence committed in this repository

- `screenshots/live-run-attempt.txt` — a genuine live run against OpenWeatherMap. Real weather in New
  York, Mumbai and London was `Clouds`, so that run correctly produced **zero** delayed orders.
  Nothing was hand-edited to manufacture a delay.
- `orders.mock.json` plus `screenshots/terminal-output.txt` — a clearly labelled `MOCK DATA` run that
  exercises the Rain and Snow paths end to end, with **live** Gemini apologies attached.
- `screenshots/gemini-integration-test.txt` — the live Gemini call plus the live invalid-key fallback.
- 89 automated tests, all passing, all running against an in-process fake API, so they cost no quota
  and never depend on today's weather.

`logs/execution.log` is generated on every run and is deliberately kept out of Git; the captured
console output above carries the same evidence.

## Run it

```bash
cd weather-delivery-automation
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # add OPENWEATHER_API_KEY and GEMINI_API_KEY
python main.py                # live run, updates orders.json
python main.py --mock         # sample weather, labelled MOCK, no weather key required
python -m pytest -q           # 89 tests
```

## Security

API keys are read from `.env` only. `.env` is git-ignored, `.env.example` carries placeholders, and
every error message passes through a redaction step, so a key embedded in a third-party exception URL
can never reach a log, the console, or this repository.

## Documentation

- `weather-delivery-automation/README.md` — architecture, error model, setup, example output
- `weather-delivery-automation/AI_LOG.md` — the actual prompts used for parallel fetching, error
  handling and apology generation, with live versus mocked results kept apart
- `weather-delivery-automation/DEMO_SCRIPT.md` — step-by-step recording walkthrough
