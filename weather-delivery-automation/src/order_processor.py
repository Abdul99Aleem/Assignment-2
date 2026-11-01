"""Order loading, concurrent weather processing, delay classification and persistence."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

import aiohttp

from src.notification import GEMINI_ENDPOINT, generate_apology_with_ai
from src.weather_client import (
    API_BASE_URL,
    WeatherClientError,
    build_timeout,
    classify_weather,
    fetch_weather,
    redact,
)

logger = logging.getLogger(__name__)

ORDER_FIELDS = ("order_id", "customer", "city", "status")
PENDING_STATUS = "Pending"
DELAYED_STATUS = "Delayed"

WeatherFetcher = Callable[..., Awaitable[Mapping[str, Any]]]


class OrderFileError(RuntimeError):
    """orders.json is missing, unreadable, or not shaped as a list of orders."""


@dataclass
class OrderResult:
    """Outcome for one order: either weather data, or an isolated error."""

    order: dict[str, Any]
    order_id: str
    city: str
    status: str
    condition: str | None = None
    description: str = ""
    delayed: bool = False
    apology: str | None = None
    apology_source: str = "none"
    error: str | None = None
    error_type: str | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None


@dataclass
class PipelineReport:
    """Aggregated results of one execution run."""

    results: list[OrderResult] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    mode: str = "live"

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def delayed(self) -> list[OrderResult]:
        return [result for result in self.results if result.delayed]

    @property
    def failed(self) -> list[OrderResult]:
        return [result for result in self.results if result.failed]

    @property
    def successful(self) -> list[OrderResult]:
        return [result for result in self.results if not result.failed]

    @property
    def pending(self) -> list[OrderResult]:
        return [result for result in self.results if result.status == PENDING_STATUS]

    @property
    def updated_orders(self) -> list[dict[str, Any]]:
        return [result.order for result in self.results]


def load_orders(path: os.PathLike[str] | str = "orders.json") -> list[dict[str, Any]]:
    """Read and validate the order dataset, preserving record order and fields."""
    orders_path = Path(path)
    try:
        raw = json.loads(orders_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OrderFileError(f"Orders file '{orders_path}' does not exist") from exc
    except json.JSONDecodeError as exc:
        raise OrderFileError(f"Orders file '{orders_path}' is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise OrderFileError(f"Orders file '{orders_path}' could not be read: {redact(exc)}") from exc

    if not isinstance(raw, list):
        raise OrderFileError(f"Orders file '{orders_path}' must contain a JSON list of orders")

    orders: list[dict[str, Any]] = []
    for index, record in enumerate(raw):
        if not isinstance(record, Mapping):
            raise OrderFileError(f"Order at position {index} is not a JSON object")
        missing = [name for name in ORDER_FIELDS if not str(record.get(name, "")).strip()]
        if missing:
            raise OrderFileError(f"Order at position {index} is missing required field(s): {', '.join(missing)}")
        orders.append(dict(record))
    return orders


def save_orders(orders: Sequence[Mapping[str, Any]], path: os.PathLike[str] | str = "orders.json") -> None:
    """Persist orders atomically: write a temp file in the same directory, then replace.

    Nothing touches the destination until every order has been processed.
    """
    orders_path = Path(path)
    orders_path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=orders_path.parent, prefix=f".{orders_path.name}.", suffix=".tmp", delete=False
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            json.dump(list(orders), handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, orders_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


async def process_order(
    session: aiohttp.ClientSession,
    order: Mapping[str, Any],
    *,
    api_key: str | None = None,
    base_url: str = API_BASE_URL,
    timeout: aiohttp.ClientTimeout | None = None,
    fetch: WeatherFetcher = fetch_weather,
    ai_api_key: str | None = None,
    ai_model: str | None = None,
    ai_endpoint: str = GEMINI_ENDPOINT,
    ai_dotenv_path: str | None = None,
) -> OrderResult:
    """Fetch weather for one order and return its result; never raises to the caller.

    A failed request keeps the order's original status, records the error on the
    result, and produces no apology — a missing weather reading is not evidence
    of bad weather.
    """
    record = dict(order)
    order_id = str(record.get("order_id", ""))
    city = str(record.get("city", ""))
    customer = str(record.get("customer", ""))
    original_status = str(record.get("status", PENDING_STATUS))

    try:
        weather_data = await fetch(
            session, city, api_key=api_key, base_url=base_url, timeout=timeout
        )
        classification = classify_weather(weather_data)
    except WeatherClientError as exc:
        logger.error("Weather lookup failed for order %s (%s): %s", order_id, city, redact(exc))
        return OrderResult(
            order=record,
            order_id=order_id,
            city=city,
            status=original_status,
            error=redact(exc),
            error_type=type(exc).__name__,
        )
    except Exception as exc:  # noqa: BLE001 - one bad city must never stop the run
        logger.exception("Unexpected failure while processing order %s (%s)", order_id, city)
        return OrderResult(
            order=record,
            order_id=order_id,
            city=city,
            status=original_status,
            error=f"Unexpected failure: {redact(exc)}",
            error_type=type(exc).__name__,
        )

    delayed = classification.delayed
    status = DELAYED_STATUS if delayed else original_status
    apology, apology_source = (None, "none")
    if delayed:
        apology, apology_source = await generate_apology_with_ai(
            session,
            customer,
            city,
            weather_data,
            api_key=ai_api_key,
            model=ai_model,
            endpoint=ai_endpoint,
            dotenv_path=ai_dotenv_path,
            timeout=timeout,
        )

    record["status"] = status
    record["weather_condition"] = classification.condition
    record["weather_description"] = classification.description
    if apology:
        record["apology_message"] = apology
        record["apology_source"] = apology_source

    logger.info(
        "Order %s | %s | %s (%s) | status=%s",
        order_id,
        city,
        classification.condition,
        classification.description or "no description",
        status,
    )
    if apology:
        logger.info("Apology (%s) for %s: %s", apology_source, customer, apology)

    return OrderResult(
        order=record,
        order_id=order_id,
        city=city,
        status=status,
        condition=classification.condition,
        description=classification.description,
        delayed=delayed,
        apology=apology,
        apology_source=apology_source,
    )


async def process_orders(
    orders: Sequence[Mapping[str, Any]],
    session: aiohttp.ClientSession,
    **kwargs: Any,
) -> list[OrderResult]:
    """Launch every weather request together with ``asyncio.gather``.

    ``return_exceptions=True`` means a task that escapes its own error handling
    still cannot cancel the other cities.
    """
    tasks = [process_order(session, order, **kwargs) for order in orders]
    gathered = await asyncio.gather(*tasks, return_exceptions=True)

    results: list[OrderResult] = []
    for order, outcome in zip(orders, gathered, strict=True):
        if isinstance(outcome, OrderResult):
            results.append(outcome)
            continue
        logger.error("Order %s failed outside its own error handling: %s", order.get("order_id"), redact(outcome))
        results.append(
            OrderResult(
                order=dict(order),
                order_id=str(order.get("order_id", "")),
                city=str(order.get("city", "")),
                status=str(order.get("status", PENDING_STATUS)),
                error=redact(outcome),
                error_type=type(outcome).__name__,
            )
        )
    return results


async def run_pipeline(
    orders: Sequence[Mapping[str, Any]],
    *,
    api_key: str | None = None,
    base_url: str = API_BASE_URL,
    timeout_seconds: float | None = None,
    fetch: WeatherFetcher = fetch_weather,
    ai_api_key: str | None = None,
    ai_model: str | None = None,
    ai_endpoint: str = GEMINI_ENDPOINT,
    ai_dotenv_path: str | None = None,
    mode: str = "live",
) -> PipelineReport:
    """Run the whole concurrent weather lookup over ``orders`` on one shared session."""
    timeout = build_timeout(timeout_seconds) if timeout_seconds else build_timeout()
    started = time.perf_counter()
    connector = aiohttp.TCPConnector(limit=max(len(orders), 1))
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        results = await process_orders(
            orders,
            session,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            fetch=fetch,
            ai_api_key=ai_api_key,
            ai_model=ai_model,
            ai_endpoint=ai_endpoint,
            ai_dotenv_path=ai_dotenv_path,
        )
    return PipelineReport(results=results, elapsed_seconds=time.perf_counter() - started, mode=mode)
