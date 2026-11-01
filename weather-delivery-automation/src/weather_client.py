"""Asynchronous OpenWeatherMap Current Weather client and response parsing."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

import aiohttp
from dotenv import load_dotenv

API_BASE_URL = "https://api.openweathermap.org/data/2.5/weather"
API_KEY_ENV_VAR = "OPENWEATHER_API_KEY"
GEMINI_API_KEY_ENV_VAR = "GEMINI_API_KEY"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0

#: Weather ``main`` conditions that make a delivery late. Compared case-insensitively.
DELAY_CONDITIONS = frozenset({"rain", "snow", "extreme"})

_SECRET_QUERY_RE = re.compile(r"((?:appid|api_key|key)=)[^&\s'\"]+", re.IGNORECASE)


class WeatherClientError(RuntimeError):
    """Base class for every weather failure, carrying the city it happened for."""

    def __init__(self, message: str, city: str | None = None) -> None:
        super().__init__(message)
        self.city = city


class ConfigurationError(WeatherClientError):
    """The API key is missing or unusable."""


class AuthenticationError(WeatherClientError):
    """HTTP 401/403 — the API key was rejected."""


class CityNotFoundError(WeatherClientError):
    """HTTP 404 — OpenWeatherMap does not know this city."""


class RateLimitError(WeatherClientError):
    """HTTP 429 — too many requests for the current plan."""


class WeatherServerError(WeatherClientError):
    """HTTP 5xx — OpenWeatherMap failed to answer."""


class NetworkTimeoutError(WeatherClientError):
    """The request exceeded its timeout."""


class NetworkError(WeatherClientError):
    """DNS, connection or transport failure."""


class MalformedResponseError(WeatherClientError):
    """The payload was not JSON, or lacked the fields we need to classify weather."""


@dataclass(frozen=True)
class WeatherClassification:
    """Interpreted weather for one destination city."""

    condition: str
    description: str
    delayed: bool
    temperature_c: float | None = None


def load_api_key(env_var: str = API_KEY_ENV_VAR, dotenv_path: str | os.PathLike[str] | None = None) -> str:
    """Read the weather API key from the environment, never returning a placeholder.

    Loads ``.env`` first when present; the real process environment always wins.
    """
    load_dotenv(dotenv_path, override=False)
    value = os.environ.get(env_var, "").strip()
    if not value or value.startswith("your_") or value == "replace_me":
        service = "OpenWeatherMap" if env_var == API_KEY_ENV_VAR else "Gemini"
        raise ConfigurationError(
            f"{env_var} is not configured. Copy .env.example to .env and set a valid "
            f"{service} API key, or run with --mock to use sample weather data."
        )
    return value


def load_gemini_api_key(env_var: str = GEMINI_API_KEY_ENV_VAR, dotenv_path: str | os.PathLike[str] | None = None) -> str:
    """Read the Gemini API key from the environment (never hardcoded or logged)."""
    return load_api_key(env_var=env_var, dotenv_path=dotenv_path)


def redact(text: str) -> str:
    """Strip API keys that third-party exception messages may have embedded in a URL."""
    return _SECRET_QUERY_RE.sub(r"\1***REDACTED***", str(text))


def build_timeout(
    total_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    connect_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
) -> aiohttp.ClientTimeout:
    """Return the request/connection timeout budget shared by every weather call."""
    return aiohttp.ClientTimeout(total=total_seconds, connect=connect_seconds)


async def fetch_weather(
    session: aiohttp.ClientSession,
    city: str,
    *,
    api_key: str | None = None,
    base_url: str = API_BASE_URL,
    timeout: aiohttp.ClientTimeout | None = None,
) -> dict[str, Any]:
    """Fetch current weather for ``city`` using the shared ``session``.

    The caller owns the session so one connection pool serves every order.
    Raises a :class:`WeatherClientError` subclass for any failure; the caller
    isolates it to the single order being processed.
    """
    if not isinstance(city, str) or not city.strip():
        raise MalformedResponseError(f"Destination city {city!r} is not a usable city name", city=None)

    api_key = api_key or load_api_key()
    params = {"q": city.strip(), "appid": api_key, "units": "metric"}

    try:
        async with session.get(base_url, params=params, timeout=timeout) as response:
            if response.status == 404:
                raise CityNotFoundError(f"Weather API returned 404 for city '{city}'", city)
            if response.status in (401, 403):
                raise AuthenticationError(
                    f"Weather API rejected the configured key (HTTP {response.status}) for city '{city}'", city
                )
            if response.status == 429:
                raise RateLimitError(f"Weather API rate limit reached (HTTP 429) for city '{city}'", city)
            if response.status >= 500:
                raise WeatherServerError(
                    f"Weather API server error (HTTP {response.status}) for city '{city}'", city
                )
            if response.status != 200:
                raise WeatherClientError(
                    f"Unexpected weather API response (HTTP {response.status}) for city '{city}'", city
                )
            payload = await parse_json(response, city)
    except asyncio.TimeoutError as exc:
        raise NetworkTimeoutError(
            f"Weather request for city '{city}' timed out after {timeout.total if timeout else DEFAULT_TIMEOUT_SECONDS}s",
            city,
        ) from exc
    except aiohttp.ClientError as exc:
        raise NetworkError(f"Network failure while fetching weather for city '{city}': {redact(exc)}", city) from exc

    return validate_payload(payload, city)


async def parse_json(response: aiohttp.ClientResponse, city: str) -> Any:
    """Decode a JSON body, reporting unusable payloads as malformed rather than network faults."""
    try:
        return await response.json(content_type=None)
    except (aiohttp.ClientError, json.JSONDecodeError, ValueError) as exc:
        raise MalformedResponseError(
            f"Weather API returned a body that is not valid JSON for city '{city}'", city
        ) from exc


def validate_payload(payload: Any, city: str | None = None) -> dict[str, Any]:
    """Check the API response is a usable Current Weather payload."""
    target = city if city is not None else (payload.get("name") if isinstance(payload, dict) else None)
    if not isinstance(payload, Mapping) or not payload:
        raise MalformedResponseError(f"Weather API returned a non-object payload for city '{target}'", target)

    cod = str(payload.get("cod", "200"))
    if cod not in {"200", "OK"}:
        if cod == "404":
            raise CityNotFoundError(f"Weather API reported city '{target}' as not found (cod 404)", target)
        if cod in {"401", "403"}:
            raise AuthenticationError(f"Weather API rejected the configured key (cod {cod})", target)
        if cod == "429":
            raise RateLimitError(f"Weather API rate limit reported (cod {cod})", target)
        raise WeatherClientError(f"Weather API reported error code {cod} for city '{target}'", target)

    weather = payload.get("weather")
    if not isinstance(weather, list) or not weather or not isinstance(weather[0], Mapping):
        raise MalformedResponseError(
            f"Weather payload for city '{target}' is missing the 'weather' condition list", target
        )
    if not str(weather[0].get("main", "")).strip():
        raise MalformedResponseError(
            f"Weather payload for city '{target}' is missing weather[0]['main']", target
        )
    return dict(payload)


def classify_weather(weather_data: Mapping[str, Any]) -> WeatherClassification:
    """Classify weather from ``weather[0]['main']`` only, never from the description.

    An order is delayed when the main condition is Rain, Snow or Extreme
    (compared case-insensitively); every other condition leaves status untouched.
    """
    payload = validate_payload(weather_data)
    first = payload["weather"][0]
    condition = str(first["main"]).strip().title()
    description = str(first.get("description", "") or "").strip().lower()
    temperature = payload.get("main", {}).get("temp") if isinstance(payload.get("main"), Mapping) else None
    temperature_c = float(temperature) if isinstance(temperature, (int, float)) else None

    return WeatherClassification(
        condition=condition,
        description=description,
        delayed=condition.lower() in DELAY_CONDITIONS,
        temperature_c=temperature_c,
    )
