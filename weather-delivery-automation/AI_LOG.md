# AI Log — Assignment 2 (Weather-Based Delivery Automation)

Tool used: **Qoder CLI** (agentic coding assistant) for design, implementation, test authoring and
debugging. Every prompt below is the actual instruction given; nothing is reconstructed after the fact.
No API keys were ever pasted into a prompt or shown in output.

---

## Prompt 1 — Parallel API fetching with `asyncio.gather`

> Implement asynchronous OpenWeatherMap requests for all four orders using `asyncio.gather` or
> equivalent. Ensure requests execute concurrently, not sequentially. Reuse one `aiohttp.ClientSession`
> instead of creating a new session for every request. Set sensible connection and request timeouts.
> Do not introduce artificial delays between the four requests. For the concurrency test, use mocked
> asynchronous requests with controlled timing or synchronization. Demonstrate that requests overlap
> rather than merely asserting that `asyncio.gather` appears in the source code.

What the AI produced: `process_orders()` in `src/order_processor.py` builds one coroutine per order and
awaits them together with `asyncio.gather(*tasks, return_exceptions=True)`; `run_pipeline()` creates a
single `aiohttp.ClientSession` with an `aiohttp.TCPConnector(limit=len(orders))` and a
`ClientTimeout(total=10, connect=5)` and passes that session into every `fetch_weather(session, city)`
call. No sleeps between requests.

Acceptance / changes: accepted. Review added two guards. (1) `gather(return_exceptions=True)` alone was
not enough proof, so the fake API in `tests/conftest.py` gates each response behind an
`asyncio.Barrier(4)` and counts an in-flight high-water mark — a sequential client can never satisfy the
barrier. (2) `fetch_weather` originally created its own session; refactored to require the caller-owned
session.

Validation: `tests/test_integration.py::test_all_four_cities_are_fetched_concurrently` (barrier satisfied,
`max_in_flight == 4`, elapsed 0.4 s instead of the 1.6 s a sequential run would need) and
`test_single_session_serves_every_request` (4 weather calls, 2 Gemini calls, one live session). Both pass.

## Prompt 2 — Error handling for invalid cities and network failures

> Handle invalid city / HTTP 404, HTTP 401 (invalid API key), HTTP 429 (rate limit), HTTP 5xx server
> errors, network errors, request timeouts, malformed or incomplete API responses, and JSON read/write
> failures. A failure for one city must never crash the entire processing pipeline. The InvalidCity123
> error must be logged clearly. For failed weather requests: preserve the original order status as
> `Pending`, do not generate a weather-based apology without valid weather data, record the processing
> error separately, continue processing all remaining orders. Do not silently swallow exceptions.

What the AI produced: an exception taxonomy in `src/weather_client.py` — `CityNotFoundError`,
`AuthenticationError`, `RateLimitError`, `WeatherServerError`, `NetworkTimeoutError`, `NetworkError`,
`MalformedResponseError`, `ConfigurationError`, all under `WeatherClientError` and carrying the `city`.
`process_order()` wraps fetch + classify in `try/except` and converts any failure into an `OrderResult`
that keeps the original status, records `error`/`error_type`, and produces no apology; a final
`except Exception` (marked `noqa: BLE001`) catches the unforeseen while logging with traceback, so the
other three orders still complete. Timeouts are converted out of `asyncio.TimeoutError`, non-JSON bodies
out of `aiohttp.InvalidJsonError`, and `cod`-in-body 404/401/429 responses are mapped too.

Acceptance / changes: accepted after two review fixes. (1) An invalid `city` value (`""`, `None`) was
reaching the HTTP layer — now rejected as `MalformedResponseError` before the request. (2) `aiohttp`
exception strings can echo the request URL, which contains `appid=`; added `redact()` and applied it to
every error message that reaches a log, a result or the summary.

Validation: `test_invalid_city_is_isolated_and_other_orders_finish`,
`test_one_server_error_does_not_stop_the_other_three`,
`test_rate_limited_and_server_errors_are_reported_per_order`, `test_malformed_api_response_is_recorded_not_delayed`,
`test_http_status_codes_map_to_specific_errors`, `test_request_timeout_is_isolated_error`,
`test_connection_failure_raises_network_error`, `test_no_apology_without_delaying_weather`.
Live evidence: `logs/execution.log` and `screenshots/terminal-output.txt` show
`ERROR … Weather lookup failed for order 1004 (InvalidCity123): Weather API returned 404 …` while orders
1001–1003 finish normally.

## Prompt 3 — AI apology generation (Gemini) with deterministic fallback

> Integrate Gemini using `gemini-3.5-flash-lite` for personalized apology generation for delayed orders.
> Load all API keys from `.env`. Never hardcode or print credentials. Implement AI failure handling and a
> deterministic fallback. Generate messages only for orders classified as delayed. Keep clear separation
> so an LLM provider could be integrated later.

What the AI produced: `src/notification.py` with `build_apology_prompt()` (facts in, nothing invented:
customer name, destination city, `weather[0]["main"]` plus its description, plus explicit "no forecasts,
no severity claims, no delivery dates" instruction), `generate_apology_with_ai()` posting to
`https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent` on the same shared
session with the key in the `x-goog-api-key` header, and `generate_weather_apology()` as the deterministic
template. The AI function returns `(message, source)`; any failure — missing key, HTTP 403/429/500, empty
candidate, unreachable endpoint — is logged at `WARNING` and the template message is returned, so a
provider outage never breaks delivery processing. Only delay-classified weather reaches this code path.

Acceptance / changes: accepted. The model name and endpoint are module constants overridable by
configuration, and the whole provider call sits behind one function so another LLM can be swapped in
without touching the pipeline.

Validation: `test_ai_apology_uses_gemini_response`, `test_ai_failure_falls_back_to_template` (403/429/500),
`test_empty_ai_response_falls_back_to_template`, `test_unreachable_ai_endpoint_falls_back_without_raising`,
`test_missing_ai_key_uses_template_and_sends_no_request`, `test_no_ai_request_for_non_delayed_weather`,
`test_template_apology_matches_assignment_example`.
Actual generated message from the verified code path:
`Hi Alice Smith, your order to New York is delayed due to moderate rain. We apologize for the inconvenience and appreciate your patience!`

---

## Design rationale (as implemented)

- **Why concurrency:** four independent, I/O-bound HTTPS lookups. Sequential fetching multiplies wall-clock
  time by the number of cities and by each timeout in a failure scenario; `asyncio.gather` on one session
  overlaps them, which matters when a slow city would otherwise stall the run.
- **How exceptions are isolated:** every order is its own coroutine, and `process_order` converts failures
  into a returned result object instead of raising; `asyncio.gather(..., return_exceptions=True)` is the
  second net for anything that escapes.
- **How the invalid city is handled:** HTTP 404 (or `cod: 404`) becomes `CityNotFoundError`, logged at
  `ERROR` with order id and city, leaving order 1004 as `Pending` with no weather fields and no apology.
- **How weather classification works:** `classify_weather()` reads `weather[0]["main"]` only and compares
  it case-insensitively against `{rain, snow, extreme}`. Descriptions are used for message wording, never
  for the delay decision.
- **How the apology function was developed:** template first for determinism and testability, then the
  Gemini path wrapped around it with the template retained as the documented fallback, and a
  `apology_source` field persisted so reviewers can see which path produced each message.

## Live verification (after credentials were configured)

Performed with the real APIs, credentials supplied only through `.env` and never printed:

1. **OpenWeatherMap, live — PASS.** `python main.py` fetched all four cities on one shared session in
   **0.120 s**. Actual conditions at the 2026-11-01 21:36 run: New York `Clouds (broken clouds)`,
   Mumbai `Clouds (overcast clouds)`, London `Clouds (overcast clouds)`; `InvalidCity123` returned a real
   **HTTP 404**, logged as `CityNotFoundError`, order 1004 left `Pending`, and the other three orders
   still finished. Because no city reported Rain/Snow/Extreme, the live run correctly produced
   **0 delayed orders** — nothing was upgraded to `Delayed` to make the demo look better.
   Evidence: `screenshots/live-run-attempt.txt`, `logs/execution.log`.
   Note: the key first returned HTTP 401 (`Invalid API key`) while OpenWeatherMap was still activating it;
   that attempt is what produced the `AuthenticationError` path, and the app handled it per-order without
   crashing. Retry after activation succeeded.
2. **Gemini, live — PASS (labelled integration test, controlled input).** Because no live order
   qualified as delayed, the AI path was exercised separately against the real endpoint through
   `generate_apology_with_ai()` with a controlled `Rain (moderate rain)` input. Model
   `gemini-3.5-flash-lite` returned `apology_source=gemini`:
   *"Dear Alice Smith, we sincerely apologise that your order to New York is delayed due to the current
   moderate rain, and we truly thank you for your patience during this time."*
   Grounding check passed: contains the customer name, the city and the actual condition, and no invented
   facts. Evidence: `screenshots/gemini-integration-test.txt`.
3. **Gemini fallback, live — PASS.** The same call with an intentionally invalid key returned HTTP 400,
   logged a `WARNING`, and produced the deterministic template message with `apology_source=template`.
   No exception reached the pipeline.
4. **Pipeline with live Gemini + mock weather — PASS.** `python main.py --mock` (fixed sample Rain/Snow,
   labelled `MOCK DATA — NOT LIVE WEATHER`) produced two `apology_source: gemini` messages, proving the
   AI path inside the real pipeline. Evidence: `screenshots/terminal-output.txt`, `orders.mock.json`.
5. **Automated suite — 89 passed.** The suite is hermetic by design: an autouse fixture deletes the
   credential environment variables and stubs out `.env` discovery, so no test can silently consume API
   quota or depend on live weather.

Mocked tests and live calls are kept separate throughout; every claim above names its evidence file.

## Not claimed
No live order was delayed, therefore no live-weather apology exists — the apology path is evidenced by the
labelled live Gemini integration test and the mocked suite instead.
