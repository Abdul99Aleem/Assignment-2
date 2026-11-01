"""End-to-end tests: concurrency proof, resilience, persistence, CLI behaviour, secret hygiene."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import aiohttp
import pytest

from src.order_processor import load_orders, process_orders, run_pipeline, save_orders
from src.weather_client import classify_weather
from tests.conftest import GEMINI_KEY, WEATHER_KEY

ROOT = Path(__file__).resolve().parents[1]

FOUR_CITY_SCENARIOS = {
    "New York": {"main": "Rain", "description": "moderate rain"},
    "Mumbai": {"main": "Snow", "description": "light snow"},
    "London": {"main": "Clear", "description": "clear sky"},
    "InvalidCity123": {"status": 404},
}


async def test_all_four_cities_are_fetched_concurrently(fake_api, orders):
    """A 4-party barrier inside the fake API only clears if all requests overlap.

    Sequential fetching would leave the first handler waiting alone, time out, and
    return 500 for every city.
    """
    barrier = asyncio.Barrier(4)
    api = await fake_api(scenarios=FOUR_CITY_SCENARIOS, delay=0.4, barrier=barrier)

    report = await run_pipeline(
        orders, api_key=WEATHER_KEY, base_url=api.base_url, ai_api_key=GEMINI_KEY, ai_endpoint=api.gemini_url
    )

    assert len(api.weather_requests) == 4
    assert api.max_in_flight == 4, "requests did not overlap"
    assert barrier.broken is False
    assert report.elapsed_seconds < 4 * 0.4, "fetched sequentially, not concurrently"
    assert [result for result in report.failed if result.city != "InvalidCity123"] == []


async def test_single_session_serves_every_request(orders, fake_api):
    """One ClientSession is reused for all four cities rather than opened per request."""
    api = await fake_api(scenarios=FOUR_CITY_SCENARIOS)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        results = await process_orders(
            orders,
            session,
            api_key=WEATHER_KEY,
            base_url=api.base_url,
            ai_api_key=GEMINI_KEY,
            ai_endpoint=api.gemini_url,
        )
        assert len(results) == 4
        assert len(api.weather_requests) == 4
        assert len(api.gemini_requests) == 2
        assert session.connector is not None and not session.connector.closed


async def test_pipeline_updates_and_persists_all_four_orders(fake_api, orders, tmp_path):
    api = await fake_api(scenarios=FOUR_CITY_SCENARIOS, gemini_text="Apologising to the customer.")
    report = await run_pipeline(
        orders, api_key=WEATHER_KEY, base_url=api.base_url, ai_api_key=GEMINI_KEY, ai_endpoint=api.gemini_url
    )
    target = tmp_path / "orders.json"
    save_orders(report.updated_orders, target)

    stored = json.loads(target.read_text(encoding="utf-8"))
    assert len(stored) == 4
    assert [order["order_id"] for order in stored] == ["1001", "1002", "1003", "1004"]
    statuses = {order["order_id"]: order["status"] for order in stored}
    assert statuses == {"1001": "Delayed", "1002": "Delayed", "1003": "Pending", "1004": "Pending"}
    assert all(order["customer"] and order["city"] for order in stored)
    assert stored[0]["apology_message"] == "Apologising to the customer."
    assert "apology_message" not in stored[2]
    assert "weather_condition" not in stored[3]
    assert report.mode == "live"


async def test_report_counts_match_the_assignment_summary(fake_api, orders):
    api = await fake_api(scenarios=FOUR_CITY_SCENARIOS)
    report = await run_pipeline(
        orders, api_key=WEATHER_KEY, base_url=api.base_url, ai_api_key=GEMINI_KEY, ai_endpoint=api.gemini_url
    )
    assert report.total == 4
    assert len(report.successful) == 3
    assert len(report.delayed) == 2
    assert len(report.pending) == 2
    assert len(report.failed) == 1
    assert report.elapsed_seconds > 0


async def test_rate_limited_and_server_errors_are_reported_per_order(fake_api, orders):
    scenarios = dict(FOUR_CITY_SCENARIOS, **{"New York": {"status": 429}, "Mumbai": {"status": 503}})
    api = await fake_api(scenarios=scenarios)
    report = await run_pipeline(orders, api_key=WEATHER_KEY, base_url=api.base_url)

    errors = {result.order_id: result.error_type for result in report.failed}
    assert errors == {"1001": "RateLimitError", "1002": "WeatherServerError", "1004": "CityNotFoundError"}
    assert [result.status for result in report.results if result.failed].count("Pending") == 3
    assert report.delayed == []


def test_mock_cli_run_produces_updated_dataset_and_log(tmp_path):
    """Runs main.py --mock: sample data, no HTTP, no credentials required."""
    output = tmp_path / "orders.json"
    log_file = tmp_path / "execution.log"
    run = subprocess.run(
        [
            sys.executable,
            "main.py",
            "--mock",
            "--env-file",
            "/nonexistent.env",
            "--orders",
            "orders.original.json",
            "--output",
            str(output),
            "--log-file",
            str(log_file),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert run.returncode == 0, run.stderr

    stored = json.loads(output.read_text(encoding="utf-8"))
    statuses = {order["order_id"]: order["status"] for order in stored}
    assert statuses == {"1001": "Delayed", "1002": "Delayed", "1003": "Pending", "1004": "Pending"}
    assert stored[0]["apology_source"] == "template"
    assert "New York" in stored[0]["apology_message"] and "Alice Smith" in stored[0]["apology_message"]

    log_text = log_file.read_text(encoding="utf-8")
    assert "MOCK" in log_text
    assert "EXECUTION SUMMARY" in log_text
    assert "Total execution time" in log_text
    assert "CityNotFoundError" in log_text


def test_cli_without_api_key_reports_configuration_problem(tmp_path):
    log_file = tmp_path / "execution.log"
    run = subprocess.run(
        [
            sys.executable,
            "main.py",
            "--env-file",
            "/nonexistent.env",
            "--orders",
            "orders.original.json",
            "--output",
            str(tmp_path / "out.json"),
            "--log-file",
            str(log_file),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert run.returncode == 1
    assert "OPENWEATHER_API_KEY is not configured" in run.stdout
    assert "Traceback" not in run.stdout + run.stderr
    assert not (tmp_path / "out.json").exists()


async def test_no_credentials_written_to_output_or_logs(fake_api, orders, tmp_path, caplog):
    api = await fake_api(scenarios=FOUR_CITY_SCENARIOS, gemini_text="Apologising to the customer.")
    report = await run_pipeline(
        orders, api_key=WEATHER_KEY, base_url=api.base_url, ai_api_key=GEMINI_KEY, ai_endpoint=api.gemini_url
    )
    target = tmp_path / "orders.json"
    save_orders(report.updated_orders, target)

    dumped = target.read_text(encoding="utf-8") + caplog.text
    assert WEATHER_KEY not in dumped
    assert GEMINI_KEY not in dumped
    assert "appid" not in dumped


def test_assignment_dataset_is_unchanged():
    orders = load_orders(ROOT / "orders.original.json")
    assert all(order["status"] == "Pending" for order in orders)
    assert classify_weather({"weather": [{"main": "Rain"}], "main": {}}).delayed is True
