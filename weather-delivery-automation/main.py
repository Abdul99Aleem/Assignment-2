"""Weather-based delivery automation — CLI entry point.

Reads orders.json, fetches current weather for every destination city concurrently,
marks Rain/Snow/Extreme orders as Delayed, generates weather-aware apologies, and
writes the updated dataset back safely.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.order_processor import (
    OrderFileError,
    PipelineReport,
    load_orders,
    run_pipeline,
    save_orders,
)
from src.weather_client import (
    CityNotFoundError,
    ConfigurationError,
    WeatherClientError,
    load_api_key,
    load_gemini_api_key,
    redact,
)

LOG_FILE = Path("logs/execution.log")

#: Fixed sample payloads used only by --mock so the pipeline can be demonstrated
#: without an API key. They are labelled MOCK everywhere and are never presented
#: as real weather.
MOCK_WEATHER: dict[str, dict[str, Any]] = {
    "New York": {"weather": [{"main": "Rain", "description": "moderate rain"}], "main": {"temp": 12.4}},
    "Mumbai": {"weather": [{"main": "Snow", "description": "light snow"}], "main": {"temp": 31.0}},
    "London": {"weather": [{"main": "Clear", "description": "clear sky"}], "main": {"temp": 9.2}},
}


async def mock_fetch_weather(session: Any, city: str, **_kwargs: Any) -> Mapping[str, Any]:
    """Serve :data:`MOCK_WEATHER` instead of HTTP; InvalidCity123 mimics a real 404."""
    key = str(city).strip()
    if key not in MOCK_WEATHER:
        raise CityNotFoundError(f"[MOCK] Weather API returned 404 for city '{city}'", key)
    return MOCK_WEATHER[key]


def setup_logging(log_file: Path, level: str) -> None:
    """Log to both the console and a timestamped execution log file."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(sys.stdout), logging.FileHandler(log_file, mode="a", encoding="utf-8")]
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Weather-based delivery automation")
    parser.add_argument("--orders", default="orders.json", help="input order dataset (default: orders.json)")
    parser.add_argument("--output", default=None, help="output path (default: overwrite the input file)")
    parser.add_argument("--log-file", default=str(LOG_FILE), help="execution log path")
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING, ERROR")
    parser.add_argument("--timeout", type=float, default=None, help="weather request timeout in seconds")
    parser.add_argument("--env-file", default=".env", help="dotenv file holding the API keys (default: .env)")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="use fixed sample weather payloads; sends no HTTP requests and needs no API key",
    )
    return parser.parse_args(argv)


def print_summary(report: PipelineReport, logger: logging.Logger) -> None:
    """Print per-city results, apologies, and the execution totals."""
    line = "=" * 78
    logger.info(line)
    logger.info("EXECUTION SUMMARY%s", "  [MOCK DATA — NOT LIVE WEATHER]" if report.mode == "mock" else "")
    logger.info(line)
    for result in report.results:
        if result.failed:
            logger.info(
                "%-14s %-13s -> %-8s  FAILED (%s): %s",
                result.order_id,
                result.city,
                result.status,
                result.error_type,
                redact(result.error or ""),
            )
        else:
            logger.info(
                "%-14s %-13s -> %-8s  %s (%s)",
                result.order_id,
                result.city,
                result.status,
                result.condition,
                result.description or "no description",
            )
        if result.apology:
            logger.info("   apology [%s]: %s", result.apology_source, result.apology)

    logger.info(line)
    logger.info("Total orders                : %d", report.total)
    logger.info("Successfully processed      : %d", len(report.successful))
    logger.info("Delayed orders              : %d", len(report.delayed))
    logger.info("Orders remaining Pending    : %d", len(report.pending))
    logger.info("Failed weather requests     : %d", len(report.failed))
    logger.info("Total execution time        : %.3f s", report.elapsed_seconds)
    logger.info(line)


async def async_main(args: argparse.Namespace) -> int:
    logger = logging.getLogger("delivery")
    orders_path = Path(args.orders)
    output_path = Path(args.output) if args.output else orders_path

    try:
        orders = load_orders(orders_path)
    except OrderFileError as exc:
        logger.error("Cannot start: %s", exc)
        return 1

    fetch = None
    api_key = None
    ai_api_key = None
    if args.mock:
        logger.warning("MOCK MODE: using fixed sample weather payloads. No HTTP requests are sent.")
        fetch = mock_fetch_weather
    else:
        try:
            api_key = load_api_key(dotenv_path=args.env_file)
        except ConfigurationError as exc:
            logger.error("Cannot start: %s", exc)
            return 1
        try:
            ai_api_key = load_gemini_api_key(dotenv_path=args.env_file)
            logger.info("Gemini apology generation enabled (key loaded from environment, value not logged).")
        except ConfigurationError as exc:
            logger.warning("Gemini not configured; personalised apologies will use the template fallback: %s", redact(exc))

    report = await run_pipeline(
        orders,
        api_key=api_key,
        ai_api_key=ai_api_key,
        ai_dotenv_path=args.env_file,
        timeout_seconds=args.timeout,
        **({"fetch": fetch} if fetch is not None else {}),
        mode="mock" if args.mock else "live",
    )

    try:
        save_orders(report.updated_orders, output_path)
    except OSError as exc:
        logger.error("Could not write '%s': %s", output_path, redact(exc))
        return 1
    logger.info("Updated order dataset written to '%s' (atomic replace).", output_path)

    print_summary(report, logger)
    return 0


def main() -> int:
    args = parse_args()
    setup_logging(Path(args.log_file), args.log_level)
    logger = logging.getLogger("delivery")
    logger.info("Run started | orders=%s | mode=%s", args.orders, "mock" if args.mock else "live")
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        return 130
    except WeatherClientError as exc:
        logger.error("Fatal weather client failure: %s", redact(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
