# Demo Script — Weather-Based Delivery Automation (Assignment 2)

Record a terminal at 120x40 or wider. Two options:

- **Option A — live weather (this is what was validated).** Run `python main.py` for steps 1–9.
  At validation time all three real cities reported `Clouds`, so the honest live result was
  **0 delayed orders** and no live apology. If your recording day has real Rain/Snow in one of the
  cities, that order will show `Delayed` with a `apology [gemini]` line — say so, and if it does not,
  say that too rather than switching to mock data mid-explanation.
- **Option B — mocked demo (guarantees a full delay + apology demo).** Use `python main.py --mock`.
  Fixed sample payloads are announced as `MOCK MODE` in the logs and labelled
  `MOCK DATA — NOT LIVE WEATHER` in the summary; the apology wording still comes from the live Gemini
  model, marked `apology [gemini]`. Say out loud that the weather is sample data because it is.
- **Option C — live Gemini apology without live weather (labelled integration test).**
  `screenshots/gemini-integration-test.txt` shows a controlled `Rain (moderate rain)` input going through
  `generate_apology_with_ai()` against the real endpoint, plus the invalid-key fallback to the template.
  Present this as an AI integration test, never as a live delayed order.

Before starting: close anything showing secrets (password managers, `.env` in an editor tab, browser key
dialogs). Never `cat .env` on camera — and never `echo $OPENWEATHER_API_KEY`.

```bash
cd weather-delivery-automation
source .venv/bin/activate        # Windows: .\.venv\Scripts\Activate.ps1
clear
```

---

## Recording setup (this machine: Ubuntu + i3, X11, OBS Studio installed)

One-time OBS configuration — takes two minutes:
1. Open OBS (`obs` in a terminal or from the app launcher).
2. The Auto-Configuration Wizard will ask about resolution and FPS. Accept the defaults
   (1920x1080, 30 or 60 FPS).
3. In **Sources**, click **+ → Screen Capture (Xcomposite) → OK**. This grabs the whole X display, so
   switching workspaces and windows during the demo is captured automatically.
   (Alternative: **+ → Window Capture**, select the gnome-terminal window — cleaner, but it will not
   follow you if you open a browser tab.)
4. In **Audio Mixer**, make sure **Mic/Aux** is not muted and click its gear icon → Properties → choose
   your headset/USB mic. Speak at a normal volume and watch the green bar move to confirm it records.
5. **File → Settings → Output → Recording**: Format **mkv** (safe if OBS crashes; convert later with
   `File → Remux Recordings` to mp4), quality **High Quality, Medium File Size**, and set the
   **Recording Path** to `~/Desktop`.
6. Close all other windows and tabs first: browsers (especially any page showing an API key), Slack/mail,
   the `.env` file in your editor, and password managers. Turn on Do Not Disturb.
7. Open **one** gnome-terminal, maximize it (in i3: `Super+Enter` opens a terminal, `Super+F` makes the
   window floating/fullscreen), and enlarge the font so text is legible in a 1080p recording:
   menu → Zoom → 150%.
8. Click **Start Recording** in OBS, do the demo, then **Stop Recording**. The file lands in
   `~/Desktop` as an `.mkv`; use `File → Remux Recordings` to get an `.mp4` for upload.

Pre-flight reset, run this before you press record so the "before" state is the mandatory dataset:
```bash
cd ~/Desktop/Yellow_Assignments_2/weather-delivery-automation
source .venv/bin/activate
cp orders.original.json orders.json     # reset to the four Pending orders
clear
```

Never do these on camera: `cat .env`, `echo $OPENWEATHER_API_KEY`, `env | grep KEY`, opening the
OpenWeatherMap/Google AI Studio console pages, or scrolling terminal history with the key in it. If a key
accidentally appears, stop the recording and re-record — or blur it before uploading.

---

## Demo walkthrough (10 steps, about 6-8 minutes)

### 1. Project overview (~40 s)
```bash
ls
```
Say: *"Python plus the OpenWeatherMap Current Weather API. It reads orders.json, fetches weather for every
destination city concurrently with asyncio and aiohttp, marks Rain, Snow and Extreme orders Delayed,
writes a personalized weather apology, survives an invalid city, saves the JSON atomically and prints a
summary."*

Point at the four modules: `main.py` (CLI plus logging), `src/weather_client.py` (HTTP plus
classification), `src/order_processor.py` (concurrency, persistence), `src/notification.py` (Gemini
apology plus template fallback).

**Screenshot 1:** this `ls` plus the README architecture section.

## 2. Input JSON (~30 s)
```bash
cat orders.json
```
Say: *"The four mandatory orders — Alice Smith to New York, Bob Jones to Mumbai, Charlie Green to London,
and order 1004 whose city is InvalidCity123. All start Pending. `orders.original.json` is a pristine copy
so I can re-run the demo."*

**Screenshot 2:** `cat orders.json`.

## 3. Configuration without revealing secrets (~40 s)
```bash
cat .env.example
grep -c OPENWEATHER_API_KEY .env        # prints a count only, never the value
```
Say: *"Keys come from `.env`, which is git-ignored. `.env.example` carries placeholders only. Nothing is
hardcoded — and if a key is missing the app stops with instructions instead of crashing."*

**Screenshot 3:** `.env.example` (never `.env`).

## 4. Start the application — live, then mocked (~90 s)
```bash
python main.py                      # live: real weather for all four cities
cp orders.original.json orders.json # reset so step 9 can show the mock result too
python main.py --mock --output orders.mock.json
```
Walk the log top to bottom for each run: banner, per-order lines, then `EXECUTION SUMMARY`.
Say on the live run: *"This is the real OpenWeatherMap call — four cities fetched concurrently, and
InvalidCity123 comes back as a genuine HTTP 404 that we log and keep going. Today all three real cities
report Clouds, so nothing qualifies as Delayed."* Then on the mock run: *"This second run uses fixed
sample Rain and Snow payloads, clearly labelled MOCK, so I can show the delay path and the AI apology
end to end."*
Note the timestamps — the weather lines land in the same second, which is the concurrency evidence
for step 5.

**Screenshot 4:** the live summary block, then the mock summary block.

## 5. Concurrent weather fetching (~60 s)
```bash
sed -n '239,255p' src/order_processor.py
python -m pytest tests/test_integration.py::test_all_four_cities_are_fetched_concurrently -v
```
Say: *"All four coroutines are launched together by `asyncio.gather` with `return_exceptions=True` on one
shared `aiohttp.ClientSession` — no per-request session and no artificial delay. The proof is not a
timing guess: the fake weather server only answers a request once all four have reached a
four-participant barrier, and it records the in-flight high-water mark. A sequential implementation
would time out at the barrier and fail this test."*

Then show the assertion output — `max_in_flight == 4` and elapsed well under 4 × the 0.4 s per-request
delay.

**Screenshot 5:** the passing concurrency test with `-v`.

## 6. Weather classification (~40 s)
```bash
sed -n '/def classify_weather/,/^$/p' src/weather_client.py
python -m pytest tests/test_weather_client.py -k "delay_conditions or uses_main_not_description" -v
```
Say: *"Delay is decided by `weather[0]['main']` only — Rain, Snow or Extreme, compared case-insensitively.
Everything else keeps its status. This test proves a payload whose description says 'heavy rain with
thunder' but whose main condition is Clouds is NOT delayed."*

**Screenshot 6:** `classify_weather` plus those passing tests.

## 7. Invalid city error handling (~40 s)
```bash
grep -n "InvalidCity123" logs/execution.log | tail -3
python -m pytest tests/test_order_processor.py::test_invalid_city_is_isolated_and_other_orders_finish -v
```
Say: *"Order 1004 gets an HTTP 404, logged at ERROR as `CityNotFoundError`. The failure is isolated to
that order: the other three still finish, order 1004 stays `Pending`, receives no apology, and the error
is counted separately as 'Failed weather requests'. Same handling for 401, 429, 5xx, timeouts, network
faults and malformed bodies."*

**Screenshot 7:** the ERROR log line and the passing isolation test.

## 8. Personalized apology generation (~50 s)
```bash
grep -n "apology" logs/execution.log | tail -4
sed -n '/def build_apology_prompt/,/^def /p' src/notification.py
python -m pytest tests/test_notifications.py -v
```
Say: *"For delayed orders only. The prompt hands Gemini only three facts — customer name, city, and the
actual condition — and forbids invented forecasts, severities or dates. Model is `gemini-3.5-flash-lite`,
key sent in a header, never printed. If the AI is unavailable for any reason the code logs a warning and
uses the deterministic template, so the customer still gets a message; the persisted
`apology_source` field says which path produced it."*

Read the message aloud: `Hi Alice Smith, your order to New York is delayed due to moderate rain. We
apologize for the inconvenience and appreciate your patience!`

**Screenshot 8:** the prompt function and an apology line.

## 9. Updated JSON (~60 s)
```bash
cat orders.json               # live output: real Clouds weather, zero Delayed
cat orders.mock.json          # mock weather: two Delayed orders with Gemini apologies
```
Say: *"First the live result — all four records preserved with their original fields, the real condition
recorded as Clouds for New York, Mumbai and London, so all four are still Pending and order 1004 kept
exactly its four original fields because its lookup failed. Nothing was hand-edited to fake a delay.
Now the mocked result: 1001 and 1002 are `Delayed` with `apology_source: gemini`, 1003 stays `Pending`
with its Clear condition recorded, and 1004 again has no invented weather. Both files were written by a
temp file plus an atomic `os.replace`, only after every order finished."*

**Screenshot 9:** both JSON files on screen.

## 10. Final summary and tests (~50 s)
```bash
tail -12 logs/execution.log
python -m pytest -q
```
Say: *"Total orders 4, successfully processed 3, delayed 2, still pending 2, failed weather requests 1,
plus total execution time. The whole suite is 89 tests against a local fake API — no key, no quota — and
they all pass."*

**Screenshot 10:** the summary block and the green test run.

---

## Still screenshots (optional, nothing is fabricated)
`screenshots/` currently holds three real text captures of actual runs — `terminal-output.txt`
(mock weather + live Gemini), `live-run-attempt.txt` (genuine live run) and
`gemini-integration-test.txt` (live AI + live fallback). No image was invented for this submission.
For per-step stills on this machine use flameshot, already installed:
```bash
flameshot gui          # select the terminal region, then Ctrl+S to save
```
Name them after the step: `screenshots/04-run-summary.png`, `05-concurrency-test.png`, …

Type commands manually rather than using up-arrow history, so no old key can flash on screen.

## What to submit (per the Assignment 2 PDF, section "3. Submission Requirements")

The PDF asks for exactly four things:

| # | Deliverable | Where it is |
|---|---|---|
| 1 | Source code (GitHub or ZIP) | `weather-delivery-automation.zip` — contains README, AI Log, tests, requirements, `.env.example` and the evidence files |
| 2 | Updated `orders.json` showing which orders were marked `Delayed` | Inside the ZIP: `orders.json` (live run) **and** `orders.mock.json` (labelled mock weather with two `Delayed` orders and live Gemini apologies) |
| 3 | Short "AI Log" with the prompts used to build the **Parallel Fetching** and **Error Handling** logic | Inside the ZIP: `AI_LOG.md` (Prompt 1 parallel fetching, Prompt 2 error handling, Prompt 3 apology generation) |
| 4 | Demo recording | the `.mp4` produced from this script |

If the portal wants individual files as well as the ZIP, upload: `orders.json`, `orders.mock.json`,
`README.md`, `AI_LOG.md`, `logs/execution.log`, `screenshots/live-run-attempt.txt`,
`screenshots/gemini-integration-test.txt`.

Do **not** submit `.env`, `.venv/`, `__pycache__/`, `.pytest_cache/`, or any frame containing a key.
If you push to GitHub instead, run `git status` first and confirm `.env` is untracked/ignored.

Because the live weather had no Rain/Snow/Extreme, the live `orders.json` legitimately shows zero
`Delayed` orders. Do not hand-edit it to fake a delay — show both files on camera and explain why.

## Closing checklist for the recording
1. State the assignment name and that Python + OpenWeatherMap + Gemini are used.
2. Show input dataset, then run, then updated dataset, then summary.
3. Say explicitly which runs are live and which are mocked.
4. Show `.env.example` only; state that `.env` is git-ignored.
5. End on the green `89 passed` line.
6. Watch the playback once: readable font, audible mic, no key visible anywhere.
7. After submitting, rotate both API keys if they were ever pasted anywhere outside `.env`.
