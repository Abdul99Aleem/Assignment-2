# Weather-Based Delivery Automation — Yellow.ai AI Intern Assignment 2

A Python automation that reads customer orders, fetches current weather for every
destination city **concurrently**, marks orders as `Delayed` when the weather condition
is `Rain`, `Snow` or `Extreme`, generates a personalized weather-aware apology for the
affected customers, survives invalid cities without crashing, and persists the updated
dataset safely.

---

## Current execution status

| Item | Status |
|---|---|
| Implementation complete | Yes |
| Automated tests | **89 passed** — all against a local fake API; the suite is hermetic (an autouse fixture blocks real credentials) |
| Live OpenWeatherMap run | **Done** — all four cities fetched concurrently in 0.120 s; real conditions were `Clouds` for New York, Mumbai and London, so **no order qualified as Delayed** (see `screenshots/live-run-attempt.txt` and `logs/execution.log`) |
| Invalid city, live | `InvalidCity123` returned a real HTTP 404, logged as `CityNotFoundError`; the other three orders finished normally and order 1004 stayed `Pending` |
| Live Gemini apology | **Done via a controlled integration test** (`screenshots/gemini-integration-test.txt`): model `gemini-3.5-flash-lite`, `apology_source=gemini`. Gemini fallback also verified live by calling with an invalid key → `apology_source=template` |
| `orders.json` | Live-updated dataset from the real run (all four records preserved, `Delayed` count 0 because no city had Rain/Snow/Extreme) |
| `orders.original.json` | Pristine mandatory input, kept for repeat demos |
| `orders.mock.json` | Clearly labelled **mock weather** run with **live Gemini** apologies (`apology_source: gemini`) |

No live weather value was invented. Because today's real weather in all three destination cities was
`Clouds`, the live run legitimately produced zero delayed orders and therefore zero live apologies —
the apology path is evidenced separately by the labelled Gemini integration test above and by the
mocked suite.

A live run produces real weather values; the mock run uses fixed sample payloads that are
labelled `MOCK` in every log line and in the summary header. Nothing else differs — the mock path exercises
the same concurrency, classification, apology, persistence and error-handling code.

---

## Architecture and execution flow

```text
orders.json
    |  load_orders()                 validate: list of objects with order_id/customer/city/status
    v
one aiohttp.ClientSession            single connection pool, shared timeouts
    |
    |  asyncio.gather(*tasks, return_exceptions=True)   <-- all 4 cities in flight together
    v
process_order(order)                 per-order isolation, never raises to the caller
    |  fetch_weather(session, city)  GET /data/2.5/weather?q=&appid=&units=metric
    |  classify_weather(data)        weather[0]["main"] in {Rain, Snow, Extreme} (case-insensitive)
    |  generate_apology_with_ai()    Gemini gemini-3.5-flash-lite -> template fallback
    v
save_orders(orders)                  temp file + os.replace() atomic write, only after all results
    |
    v
execution summary                    per-city result, apology text, totals, elapsed time
```

| Module | Responsibility |
|---|---|
| `main.py` | CLI, logging setup, mock provider, summary printing |
| `src/weather_client.py` | HTTP calls, error taxonomy, payload validation, `classify_weather()`, key loading, secret redaction |
| `src/order_processor.py` | `load_orders`, `process_order`, `process_orders` (gather), `run_pipeline`, `save_orders`, report counts |
| `src/notification.py` | Gemini apology, prompt construction, deterministic template fallback |

### Concurrency
`process_orders()` builds one coroutine per order and launches them all with
`asyncio.gather(..., return_exceptions=True)` on a single `aiohttp.ClientSession`
(`src/order_processor.py:250`). There is no `sleep` between requests and no per-request session.
The suite proves overlap rather than asserting it: the fake API only answers a request once all
four have arrived at a 4-party `asyncio.Barrier`, and it records a high-water mark of in-flight
requests (`tests/test_integration.py::test_all_four_cities_are_fetched_concurrently`). A
sequential implementation would time out at the barrier and fail that test.

### Weather classification
Classification reads `weather[0]["main"]` only. `Rain`, `Snow`, `Extreme` → `Delayed`
(case-insensitive); every other condition keeps the order's existing status. A payload with
`main: "Clouds"` and `description: "heavy rain with thunder"` is **not** delayed — the test
`test_classification_uses_main_not_description` locks that in.

### Error handling
Each failure has its own exception type, all subclasses of `WeatherClientError`:

| Condition | Type | Order outcome |
|---|---|---|
| HTTP 404 / `cod: 404` / unknown city | `CityNotFoundError` | status preserved, error recorded |
| HTTP 401/403 / bad key | `AuthenticationError` | status preserved, error recorded |
| HTTP 429 | `RateLimitError` | status preserved, error recorded |
| HTTP 5xx | `WeatherServerError` | status preserved, error recorded |
| Timeout | `NetworkTimeoutError` | status preserved, error recorded |
| DNS/connect failure | `NetworkError` | status preserved, error recorded |
| Non-JSON / missing `weather` / blank city | `MalformedResponseError` | status preserved, error recorded |
| Missing config | `ConfigurationError` | exit 1 with instructions, nothing written |

Nothing is swallowed: every failure is logged at `ERROR` with order id, city, exception class and
message, and is also counted in the "Failed weather requests" summary line. `InvalidCity123` is the
assignment's case; `test_invalid_city_is_isolated_and_other_orders_finish` proves the other three
orders still finish.

Failed requests never become `Delayed` and never receive an apology.

### Security
- Keys come from `.env` via `python-dotenv`; `.env` is git-ignored, `.env.example` holds placeholders.
- The weather key is sent as a query param and the Gemini key as the `x-goog-api-key` header
  (never in a URL that can end up in a log line).
- `redact()` scrubs `appid=`/`key=`/`api_key=` values out of any third-party exception text before
  it reaches a log or the summary.
- `test_no_credentials_written_to_output_or_logs` asserts no key value appears in `orders.json` or
  in captured log output.

---

## Technology stack
Python 3.10+ · `aiohttp` · `asyncio` (`gather`) · `python-dotenv` · OpenWeatherMap Current Weather API ·
Google Gemini REST (`gemini-3.5-flash-lite`) for apology wording with a deterministic fallback · `pytest` + `pytest-asyncio`.
No database, no Docker, no web framework.

## Project structure
```text
weather-delivery-automation/
├── main.py
├── orders.json               # mandatory 4-order input dataset
├── orders.original.json      # pristine copy kept for repeated demos
├── orders.mock.json          # labelled MOCK output of a real run
├── .env                      # your keys (git-ignored)
├── .env.example
├── .gitignore
├── requirements.txt
├── pytest.ini
├── README.md
├── AI_LOG.md
├── DEMO_SCRIPT.md
├── src/
│   ├── __init__.py
│   ├── weather_client.py
│   ├── order_processor.py
│   └── notification.py
├── tests/
│   ├── __init__.py
│   ├── conftest.py           # in-process fake OpenWeatherMap + Gemini server
│   ├── test_weather_client.py
│   ├── test_order_processor.py
│   ├── test_notifications.py
│   └── test_integration.py
├── logs/
│   ├── .gitkeep
│   └── execution.log
└── screenshots/
    ├── .gitkeep
    ├── terminal-output.txt        # captured console output of the mock-weather + live-Gemini run
    ├── live-run-attempt.txt       # captured console output of the genuine live run
    └── gemini-integration-test.txt # live Gemini apology + live fallback, controlled input
```

## Setup

Linux / macOS:
```bash
python3 --version                      # 3.10 or newer
cd weather-delivery-automation
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
```

Windows PowerShell:
```powershell
python --version
cd weather-delivery-automation
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

## API key configuration
1. Free key from <https://openweathermap.org/api> → `OPENWEATHER_API_KEY` (Current Weather data API).
2. Gemini key from <https://aistudio.google.com/apikey> → `GEMINI_API_KEY` (optional; without it the
   template apology is used and the run still succeeds).
3. Put both in `.env`. Never paste keys into chat, never commit `.env`.

Free OpenWeatherMap keys can take a few minutes to activate; a `401` on the first calls is normal.

## Run

```bash
python main.py                        # live: real weather, updates orders.json in place
python main.py --mock                 # no weather key needed: sample payloads, clearly labelled MOCK
python main.py --output orders.out.json --log-file logs/execution.log --timeout 15
python main.py --env-file /path/to/.env   # alternative credential location
python main.py --help
```

## Run the tests
```bash
python -m pytest -v                   # 89 tests, all mocked, no key required
python -m pytest tests/test_integration.py -v
```

## Example output

**Live run** — `python main.py` (real OpenWeatherMap weather, real Gemini; captured in
`screenshots/live-run-attempt.txt`):

```text
2026-11-01 21:36:52 | INFO     | delivery | Run started | orders=orders.json | mode=live
2026-11-01 21:36:52 | INFO     | delivery | Gemini apology generation enabled (key loaded from environment, value not logged).
2026-11-01 21:36:53 | INFO     | src.order_processor | Order 1002 | Mumbai | Clouds (overcast clouds) | status=Pending
2026-11-01 21:36:53 | INFO     | src.order_processor | Order 1001 | New York | Clouds (broken clouds) | status=Pending
2026-11-01 21:36:53 | INFO     | src.order_processor | Order 1003 | London | Clouds (overcast clouds) | status=Pending
2026-11-01 21:36:53 | ERROR    | src.order_processor | Weather lookup failed for order 1004 (InvalidCity123): Weather API returned 404 for city 'InvalidCity123'
2026-11-01 21:36:53 | INFO     | delivery | Updated order dataset written to 'orders.json' (atomic replace).
2026-11-01 21:36:53 | INFO     | delivery | EXECUTION SUMMARY
2026-11-01 21:36:53 | INFO     | delivery | Total orders                : 4
2026-11-01 21:36:53 | INFO     | delivery | Successfully processed      : 3
2026-11-01 21:36:53 | INFO     | delivery | Delayed orders              : 0
2026-11-01 21:36:53 | INFO     | delivery | Orders remaining Pending    : 4
2026-11-01 21:36:53 | INFO     | delivery | Failed weather requests     : 1
2026-11-01 21:36:53 | INFO     | delivery | Total execution time        : 0.120 s
```
All four cities were genuinely `Clouds` at run time, so nothing qualified as `Delayed` — correct
behaviour, not a failure. Four concurrent requests completing in 0.120 s is also the live concurrency
evidence; the deterministic proof is the barrier test.

**Mocked demo** — `python main.py --mock --output orders.mock.json` (sample weather, **live** Gemini
apologies; captured in `screenshots/terminal-output.txt`):

```text
2026-11-01 21:36:18 | WARNING  | delivery | MOCK MODE: using fixed sample weather payloads. No HTTP requests are sent.
2026-11-01 21:36:20 | INFO     | src.order_processor | Apology (gemini) for Alice Smith: Dear Alice Smith, we sincerely apologise that your order to New York is delayed due to the current moderate rain, and we truly thank you for your patience during this time.
2026-11-01 21:36:20 | INFO     | src.order_processor | Order 1003 | London | Clear (clear sky) | status=Pending
2026-11-01 21:36:20 | ERROR    | src.order_processor | Weather lookup failed for order 1004 (InvalidCity123): [MOCK] Weather API returned 404 for city 'InvalidCity123'
2026-11-01 21:36:20 | INFO     | delivery | EXECUTION SUMMARY  [MOCK DATA — NOT LIVE WEATHER]
2026-11-01 21:36:20 | INFO     | delivery | 1001           New York      -> Delayed   Rain (moderate rain)
2026-11-01 21:36:20 | INFO     | delivery | 1002           Mumbai        -> Delayed   Snow (light snow)
2026-11-01 21:36:20 | INFO     | delivery | 1003           London        -> Pending   Clear (clear sky)
2026-11-01 21:36:20 | INFO     | delivery | 1004           InvalidCity123 -> Pending   FAILED (CityNotFoundError): [MOCK] Weather API returned 404 for city 'InvalidCity123'
2026-11-01 21:36:20 | INFO     | delivery | Total orders                : 4
2026-11-01 21:36:20 | INFO     | delivery | Successfully processed      : 3
2026-11-01 21:36:20 | INFO     | delivery | Delayed orders              : 2
2026-11-01 21:36:20 | INFO     | delivery | Failed weather requests     : 1
```

Updated record shape (`orders.mock.json`, mock weather value, live Gemini message):
```json
{
  "order_id": "1001",
  "customer": "Alice Smith",
  "city": "New York",
  "status": "Delayed",
  "weather_condition": "Rain",
  "weather_description": "moderate rain",
  "apology_message": "Dear Alice Smith, we sincerely apologise that your order to New York is delayed due to the current moderate rain, and we truly thank you for your patience during this time.",
  "apology_source": "gemini"
}
```
`apology_source` is `gemini` when the AI path succeeds and `template` when it fell back, so a
reviewer can always tell where a message came from. Failed lookups keep only the four original
fields — no invented weather.

## Demo without revealing secrets
Show `.env.example`, never `.env`; the summary and logs print no key material even on failure.
