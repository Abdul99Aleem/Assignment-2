"""Shared pytest fixtures: a local fake of the OpenWeatherMap and Gemini endpoints.

Every test runs against this in-process HTTP server, so no real API key is used and
no API quota is consumed.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import web

WEATHER_PATH = "/data/2.5/weather"
GEMINI_PATH = "/v1beta/models/{model}:generateContent"

WEATHER_KEY = "FAKE-WEATHER-KEY-DO-NOT-USE"
GEMINI_KEY = "FAKE-GEMINI-KEY-DO-NOT-USE"


def owm_payload(main: str, description: str = "", temp: float = 10.0) -> dict[str, Any]:
    """Build a Current Weather shaped payload with the given condition."""
    return {
        "cod": 200,
        "name": "nowhere",
        "weather": [{"main": main, "description": description, "icon": "00d"}],
        "main": {"temp": temp, "humidity": 70},
    }


@dataclass
class FakeApi:
    """Handle and bookkeeping for a running fake API."""

    base_url: str
    gemini_url: str
    weather_requests: list[dict[str, str]] = field(default_factory=list)
    gemini_requests: list[dict[str, Any]] = field(default_factory=list)
    in_flight: int = 0
    max_in_flight: int = 0


@pytest_asyncio.fixture
async def fake_api():
    """Factory fixture starting a fake API: ``await start(scenarios={...})``."""
    runners: list[web.AppRunner] = []

    async def start(
        *,
        scenarios: dict[str, dict[str, Any]] | None = None,
        delay: float = 0.0,
        barrier: asyncio.Barrier | None = None,
        weather_key: str = WEATHER_KEY,
        gemini_status: int = 200,
        gemini_text: str | None = "Apology under test.",
    ) -> FakeApi:
        scenarios = scenarios or {}
        api = FakeApi(base_url="", gemini_url="")

        async def weather_handler(request: web.Request) -> web.Response:
            api.in_flight += 1
            api.max_in_flight = max(api.max_in_flight, api.in_flight)
            try:
                city = request.query.get("q", "")
                api.weather_requests.append(dict(request.query))
                if barrier is not None:
                    await asyncio.wait_for(barrier.wait(), timeout=5)
                if delay:
                    await asyncio.sleep(delay)
                if request.query.get("appid") != weather_key:
                    return web.json_response({"cod": 401, "message": "Invalid API key"}, status=401)
                spec = scenarios.get(city)
                if spec is None:
                    return web.json_response({"cod": 404, "message": "city not found"}, status=404)
                if "raw" in spec:
                    return web.Response(text=spec["raw"], content_type="text/plain")
                if "payload" in spec:
                    return web.json_response(spec["payload"], status=spec.get("status", 200))
                status = spec.get("status", 200)
                if status != 200:
                    return web.json_response({"cod": status, "message": f"HTTP {status}"}, status=status)
                return web.json_response(
                    owm_payload(spec["main"], spec.get("description", ""), spec.get("temp", 10.0))
                )
            finally:
                api.in_flight -= 1

        async def gemini_handler(request: web.Request) -> web.Response:
            api.gemini_requests.append({"headers": dict(request.headers), "body": await request.text()})
            if request.headers.get("x-goog-api-key") != GEMINI_KEY:
                return web.json_response({"error": "bad key"}, status=403)
            if gemini_status != 200:
                return web.json_response({"error": "upstream failure"}, status=gemini_status)
            if gemini_text is None:
                return web.json_response({"candidates": [{"content": {"parts": []}}]})
            return web.json_response(
                {"candidates": [{"content": {"parts": [{"text": gemini_text}], "role": "model"}, "finishReason": "STOP"}]},
            )

        app = web.Application()
        app.router.add_get(WEATHER_PATH, weather_handler)
        app.router.add_post(GEMINI_PATH, gemini_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        runners.append(runner)
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[index]
        api.base_url = f"http://127.0.0.1:{port}{WEATHER_PATH}"
        api.gemini_url = f"http://127.0.0.1:{port}/v1beta/models/{{model}}:generateContent"
        return api

    yield start
    for runner in runners:
        await runner.cleanup()


@pytest.fixture(autouse=True)
def isolate_credentials(monkeypatch):
    """Keep the suite hermetic: no test can read a real key or reach the real APIs."""
    monkeypatch.delenv("OPENWEATHER_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.setattr("src.weather_client.load_dotenv", lambda *args, **kwargs: False)


@pytest_asyncio.fixture
async def session():
    """One reusable client session, mirroring how the application shares a single pool."""
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as client:
        yield client


@pytest.fixture
def orders() -> list[dict[str, Any]]:
    """The four mandatory assignment orders."""
    dataset = Path(__file__).resolve().parents[1] / "orders.original.json"
    return json.loads(dataset.read_text(encoding="utf-8"))
