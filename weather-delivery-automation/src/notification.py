"""Weather-aware customer apology messages: Gemini-generated with a deterministic fallback."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Mapping

import aiohttp

from src.weather_client import (
    WeatherClassification,
    classify_weather,
    load_gemini_api_key,
    redact,
)

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_API_KEY_ENV_VAR = "GEMINI_API_KEY"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

#: Fallback wording when the API omits a human-readable description.
CONDITION_PHRASES = {
    "Rain": "rain",
    "Snow": "snow",
    "Extreme": "extreme weather",
}

APOLOGY_TEMPLATE = (
    "Hi {customer}, your order to {city} is delayed due to {reason}. "
    "We apologize for the inconvenience and appreciate your patience!"
)

PROMPT_SYSTEM_RULES = (
    "Use only the facts given. Invent nothing: no forecasts, no severity claims, no delivery dates, no refunds."
)



def build_apology_prompt(customer_name: str, city: str, classification: WeatherClassification) -> str:
    """Compose the grounded prompt sent to Gemini for a delayed order."""
    weather = classification.condition
    if classification.description:
        weather = f"{classification.condition} ({classification.description})"
    return (
        "Write exactly one professional, empathetic sentence (maximum 45 words) apologising to a customer "
        "whose delivery is delayed by weather. Use only the facts below and invent nothing: no forecasts, "
        "no severity claims, no delivery dates, no refunds.\n"
        f"Customer name: {customer_name}\n"
        f"Destination city: {city}\n"
        f"Current weather: {weather}\n"
        "The sentence must greet the customer by name, state that the order to that city is delayed because of "
        "that weather, apologise, and thank them for their patience."
    )


def weather_reason(classification: WeatherClassification) -> str:
    """Build the natural-language weather reason used in the fallback apology."""
    fallback = CONDITION_PHRASES.get(classification.condition, classification.condition.lower())
    phrase = classification.description
    if not phrase or classification.condition.lower() in phrase.lower():
        return phrase or fallback
    return f"{fallback} ({phrase})"


def generate_weather_apology(
    customer_name: str,
    city: str,
    weather_data: Mapping[str, Any] | None,
) -> str | None:
    """Return the deterministic template apology, or ``None`` when none applies.

    Messages are only produced for weather that classifies as delay-causing and use
    only the supplied name, city and real API condition.
    """
    if weather_data is None:
        return None

    classification = classify_weather(weather_data)
    if not classification.delayed:
        return None

    return APOLOGY_TEMPLATE.format(
        customer=str(customer_name).strip() or "there",
        city=str(city).strip() or "your address",
        reason=weather_reason(classification),
    )


def _extract_candidate_text(payload: Mapping[str, Any]) -> str:
    """Pull the generated text out of a Gemini REST response, or raise ValueError."""
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError(f"Gemini returned no candidates (block reason: {payload.get('promptFeedback', {}).get('blockReason')})")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = " ".join(str(part.get("text", "")) for part in parts if isinstance(part, Mapping)).strip()
    if not text:
        raise ValueError("Gemini returned an empty text candidate")
    return text


async def generate_apology_with_ai(
    session: aiohttp.ClientSession,
    customer_name: str,
    city: str,
    weather_data: Mapping[str, Any] | None,
    *,
    api_key: str | None = None,
    model: str | None = None,
    endpoint: str = GEMINI_ENDPOINT,
    dotenv_path: str | None = None,
    timeout: aiohttp.ClientTimeout | None = None,
) -> tuple[str | None, str]:
    """Generate a personalised apology with Gemini, falling back to the template.

    Returns ``(message, source)`` where source is ``"gemini"`` or ``"template"``.
    AI failure never raises into the delivery pipeline: it is logged and the
    deterministic message is used instead. No apology is produced without valid
    weather data.
    """
    fallback = generate_weather_apology(customer_name, city, weather_data)
    if fallback is None:
        return None, "none"

    model = model or os.environ.get("GEMINI_MODEL", "").strip() or GEMINI_MODEL
    try:
        api_key = api_key or load_gemini_api_key(dotenv_path=dotenv_path)
        prompt = build_apology_prompt(customer_name, city, classify_weather(weather_data))
        url = endpoint.format(model=model)
        async with session.post(
            url,
            headers={"x-goog-api-key": api_key},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=timeout,
        ) as response:
            if response.status != 200:
                raise aiohttp.ClientResponseError(
                    response.request_info, response.history, status=response.status, message=f"HTTP {response.status}"
                )
            payload = json.loads(await response.text())
        message = _extract_candidate_text(payload)
        logger.info("Gemini (%s) apology generated for order to %s", model, city)
        return message, "gemini"
    except Exception as exc:  # noqa: BLE001 - AI failure must never break delivery processing
        logger.warning("Gemini apology unavailable (%s); using deterministic template message: %s", model, redact(exc))
        return fallback, "template"
