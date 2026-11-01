"""Unit tests for the weather client: HTTP failures, payload validation, classification."""

from __future__ import annotations

from typing import Any

import aiohttp
import pytest

from src.weather_client import (
    AuthenticationError,
    CityNotFoundError,
    ConfigurationError,
    MalformedResponseError,
    NetworkError,
    NetworkTimeoutError,
    RateLimitError,
    WeatherServerError,
    classify_weather,
    fetch_weather,
    load_api_key,
    redact,
    validate_payload,
)
from tests.conftest import WEATHER_KEY


async def test_fetch_weather_returns_payload_and_sends_required_params(fake_api, session):
    api = await fake_api(scenarios={"London": {"main": "Clear", "description": "clear sky"}})
    payload = await fetch_weather(session, "London", api_key=WEATHER_KEY, base_url=api.base_url)

    assert payload["weather"][0]["main"] == "Clear"
    sent = api.weather_requests[0]
    assert sent["q"] == "London"
    assert sent["units"] == "metric"
    assert sent["appid"] == WEATHER_KEY


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (404, CityNotFoundError),
        (401, AuthenticationError),
        (429, RateLimitError),
        (500, WeatherServerError),
        (503, WeatherServerError),
    ],
)
async def test_http_status_codes_map_to_specific_errors(fake_api, session, status, expected):
    api = await fake_api(scenarios={"Bad": {"status": status}})
    with pytest.raises(expected) as caught:
        await fetch_weather(session, "Bad", api_key=WEATHER_KEY, base_url=api.base_url)
    assert caught.value.city == "Bad"


async def test_unknown_city_raises_city_not_found(fake_api, session):
    api = await fake_api(scenarios={})
    with pytest.raises(CityNotFoundError):
        await fetch_weather(session, "InvalidCity123", api_key=WEATHER_KEY, base_url=api.base_url)


async def test_request_timeout_is_isolated_error(fake_api, session):
    api = await fake_api(scenarios={"Slow": {"main": "Rain", "description": "heavy rain"}}, delay=2.0)
    timeout = aiohttp.ClientTimeout(total=0.3)
    with pytest.raises(NetworkTimeoutError):
        await fetch_weather(session, "Slow", api_key=WEATHER_KEY, base_url=api.base_url, timeout=timeout)


async def test_connection_failure_raises_network_error(session):
    dead_port = "http://127.0.0.1:1/data/2.5/weather"
    with pytest.raises(NetworkError):
        await fetch_weather(session, "New York", api_key=WEATHER_KEY, base_url=dead_port)


async def test_invalid_json_body_is_malformed_not_network(fake_api, session):
    api = await fake_api(scenarios={"Broken": {"raw": "<html>gateway error</html>"}})
    with pytest.raises(MalformedResponseError):
        await fetch_weather(session, "Broken", api_key=WEATHER_KEY, base_url=api.base_url)


async def test_wrong_api_key_is_authentication_error(fake_api, session):
    api = await fake_api(scenarios={"London": {"main": "Clear"}})
    with pytest.raises(AuthenticationError):
        await fetch_weather(session, "London", api_key="wrong-key", base_url=api.base_url)


@pytest.mark.parametrize("city", ["", "   ", None])
async def test_blank_city_is_handled_without_crashing(session, city):
    with pytest.raises(MalformedResponseError):
        await fetch_weather(session, city, api_key=WEATHER_KEY)  # type: ignore[arg-type]


def test_missing_api_key_raises_configuration_error(monkeypatch):
    monkeypatch.delenv("OPENWEATHER_API_KEY", raising=False)
    with pytest.raises(ConfigurationError) as caught:
        load_api_key(dotenv_path="/nonexistent/.env")
    assert "OPENWEATHER_API_KEY" in str(caught.value)


def test_placeholder_api_key_is_rejected(monkeypatch):
    monkeypatch.setenv("OPENWEATHER_API_KEY", "your_api_key_here")
    with pytest.raises(ConfigurationError):
        load_api_key(dotenv_path="/nonexistent/.env")


def test_redact_strips_keys_from_urls():
    message = "Request to https://api.openweathermap.org/data/2.5/weather?appid=SECRET123&q=London failed"
    assert "SECRET123" not in redact(message)
    assert "REDACTED" in redact(message)


@pytest.mark.parametrize("payload", [None, [], {}, "text", 42])
def test_non_object_payloads_are_malformed(payload: Any):
    with pytest.raises(MalformedResponseError):
        validate_payload(payload, "Atlantis")


@pytest.mark.parametrize("payload", [{"weather": []}, {"weather": "Rain"}, {"cod": 200}, {"weather": [{}]}])
def test_incomplete_weather_payloads_are_malformed(payload: dict[str, Any]):
    with pytest.raises(MalformedResponseError):
        validate_payload(payload, "Atlantis")


def test_payload_cod_404_is_city_not_found():
    with pytest.raises(CityNotFoundError):
        validate_payload({"cod": "404", "message": "city not found"}, "InvalidCity123")


@pytest.mark.parametrize("condition", ["Rain", "Snow", "Extreme", "rain", "SNOW", "eXtReMe"])
def test_delay_conditions_classify_as_delayed(condition: str):
    result = classify_weather({"weather": [{"main": condition, "description": "whatever"}], "main": {"temp": 3.0}})
    assert result.delayed is True
    assert result.condition == condition.title()


@pytest.mark.parametrize("condition", ["Clear", "Clouds", "Mist", "Haze", "Thunderstorm", "Drizzle", "Fog"])
def test_other_conditions_do_not_delay(condition: str):
    assert classify_weather({"weather": [{"main": condition}], "main": {}}).delayed is False


def test_classification_uses_main_not_description():
    result = classify_weather({"weather": [{"main": "Clouds", "description": "heavy rain with thunder"}], "main": {}})
    assert result.delayed is False
    assert result.condition == "Clouds"


def test_classification_reads_temperature_and_description():
    result = classify_weather({"weather": [{"main": "Rain", "description": "Moderate Rain"}], "main": {"temp": 12.5}})
    assert (result.temperature_c, result.description) == (12.5, "moderate rain")


def test_classify_rejects_unusable_payload():
    with pytest.raises(MalformedResponseError):
        classify_weather({"main": {"temp": 1}})
