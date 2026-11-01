"""Tests for weather-aware apology generation: Gemini path, template fallback, grounding."""

from __future__ import annotations

import json
import pytest

from src.notification import (
    GEMINI_MODEL,
    build_apology_prompt,
    generate_apology_with_ai,
    generate_weather_apology,
    weather_reason,
)
from src.weather_client import classify_weather
from tests.conftest import GEMINI_KEY

RAIN = {"weather": [{"main": "Rain", "description": "moderate rain"}], "main": {"temp": 11.0}}
SNOW = {"weather": [{"main": "Snow", "description": "heavy snow"}], "main": {"temp": -3.0}}
EXTREME = {"weather": [{"main": "Extreme", "description": ""}], "main": {"temp": 4.0}}
CLEAR = {"weather": [{"main": "Clear", "description": "clear sky"}], "main": {"temp": 20.0}}


@pytest.mark.parametrize(
    ("weather", "customer", "city", "condition"),
    [(RAIN, "Alice Smith", "New York", "Rain"), (SNOW, "Bob Jones", "Mumbai", "Snow"), (EXTREME, "Ada", "Oslo", "Extreme")],
)
def test_template_apology_names_customer_city_and_condition(weather, customer, city, condition):
    message = generate_weather_apology(customer, city, weather)
    assert message is not None
    assert customer in message
    assert city in message
    assert condition.lower() in message.lower()
    assert "delay" in message.lower() and "apolog" in message.lower()


def test_template_apology_matches_assignment_example():
    assert generate_weather_apology("Alice Smith", "New York", RAIN) == (
        "Hi Alice Smith, your order to New York is delayed due to moderate rain. "
        "We apologize for the inconvenience and appreciate your patience!"
    )


@pytest.mark.parametrize("weather", [CLEAR, {"weather": [{"main": "Clouds"}]}, None])
def test_no_apology_without_delaying_weather(weather):
    assert generate_weather_apology("Alice Smith", "New York", weather) is None


def test_extreme_condition_without_description_still_reads_naturally():
    message = generate_weather_apology("Ada", "Oslo", EXTREME)
    assert message is not None
    assert "extreme weather" in message
    assert "extreme" in message.lower()


def test_weather_reason_prefers_api_description():
    assert weather_reason(classify_weather(RAIN)) == "moderate rain"
    assert weather_reason(classify_weather({"weather": [{"main": "Rain", "description": "shower"}]})) == "rain (shower)"


def test_prompt_is_grounded_in_supplied_facts():
    prompt = build_apology_prompt("Alice Smith", "New York", classify_weather(RAIN))
    assert "Alice Smith" in prompt and "New York" in prompt and "Rain (moderate rain)" in prompt
    assert "invent nothing" in prompt.lower()


async def test_ai_apology_uses_gemini_response(fake_api, session):
    api = await fake_api(gemini_text="Hi Alice Smith, your New York order is delayed by moderate rain — apologies!")
    message, source = await generate_apology_with_ai(
        session, "Alice Smith", "New York", RAIN, api_key=GEMINI_KEY, endpoint=api.gemini_url
    )
    assert source == "gemini"
    assert "Alice Smith" in message
    assert api.gemini_requests[0]["headers"]["x-goog-api-key"] == GEMINI_KEY
    assert GEMINI_KEY not in json.dumps(api.gemini_requests[0]["body"])


def test_default_model_is_the_configured_gemini_model():
    assert GEMINI_MODEL == "gemini-3.5-flash-lite"


@pytest.mark.parametrize("gemini_status", [500, 403, 429])
async def test_ai_failure_falls_back_to_template(fake_api, session, gemini_status):
    api = await fake_api(gemini_status=gemini_status)
    message, source = await generate_apology_with_ai(
        session, "Bob Jones", "Mumbai", SNOW, api_key=GEMINI_KEY, endpoint=api.gemini_url
    )
    assert source == "template"
    assert message == generate_weather_apology("Bob Jones", "Mumbai", SNOW)


async def test_empty_ai_response_falls_back_to_template(fake_api, session):
    api = await fake_api(gemini_text=None)
    message, source = await generate_apology_with_ai(
        session, "Alice Smith", "New York", RAIN, api_key=GEMINI_KEY, endpoint=api.gemini_url
    )
    assert source == "template"
    assert "moderate rain" in message


async def test_unreachable_ai_endpoint_falls_back_without_raising(fake_api, session):
    message, source = await generate_apology_with_ai(
        session, "Alice Smith", "New York", RAIN, api_key=GEMINI_KEY, endpoint="http://127.0.0.1:1/v1beta/models/x:generateContent"
    )
    assert source == "template"
    assert message is not None


async def test_missing_ai_key_uses_template_and_sends_no_request(fake_api, session, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    api = await fake_api()
    message, source = await generate_apology_with_ai(
        session, "Alice Smith", "New York", RAIN, api_key=None, endpoint=api.gemini_url
    )
    assert source == "template"
    assert api.gemini_requests == []
    assert message is not None


async def test_no_ai_request_for_non_delayed_weather(fake_api, session):
    api = await fake_api()
    message, source = await generate_apology_with_ai(
        session, "Charlie Green", "London", CLEAR, api_key=GEMINI_KEY, endpoint=api.gemini_url
    )
    assert (message, source) == (None, "none")
    assert api.gemini_requests == []
