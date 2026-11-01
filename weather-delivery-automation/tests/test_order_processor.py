"""Tests for order loading, delay classification, per-order error isolation and persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.order_processor import (
    DELAYED_STATUS,
    PENDING_STATUS,
    OrderFileError,
    load_orders,
    process_order,
    process_orders,
    save_orders,
)
from tests.conftest import GEMINI_KEY, WEATHER_KEY


def test_load_orders_reads_the_four_mandatory_records(tmp_path: Path):
    dataset = tmp_path / "orders.json"
    dataset.write_text(
        json.dumps(
            [
                {"order_id": "1001", "customer": "Alice Smith", "city": "New York", "status": "Pending"},
                {"order_id": "1002", "customer": "Bob Jones", "city": "Mumbai", "status": "Pending"},
                {"order_id": "1003", "customer": "Charlie Green", "city": "London", "status": "Pending"},
                {"order_id": "1004", "customer": "InvalidCity123", "city": "InvalidCity123", "status": "Pending"},
            ]
        ),
        encoding="utf-8",
    )
    orders = load_orders(dataset)
    assert len(orders) == 4
    assert [order["city"] for order in orders] == ["New York", "Mumbai", "London", "InvalidCity123"]
    assert all(order["status"] == PENDING_STATUS for order in orders)


@pytest.mark.parametrize(
    ("content", "reason"),
    [("{not json", "invalid JSON"), ('{"orders": []}', "must contain a JSON list"), ('[{"order_id": "1"}]', "missing")],
)
def test_load_orders_reports_bad_datasets(tmp_path: Path, content: str, reason: str):
    dataset = tmp_path / "orders.json"
    dataset.write_text(content, encoding="utf-8")
    with pytest.raises(OrderFileError, match=r"\S"):
        load_orders(dataset)


def test_load_orders_missing_file_raises(tmp_path: Path):
    with pytest.raises(OrderFileError):
        load_orders(tmp_path / "absent.json")


def test_project_orders_file_matches_assignment_dataset():
    orders = load_orders(Path(__file__).resolve().parents[1] / "orders.original.json")
    assert [(o["order_id"], o["customer"], o["city"], o["status"]) for o in orders] == [
        ("1001", "Alice Smith", "New York", "Pending"),
        ("1002", "Bob Jones", "Mumbai", "Pending"),
        ("1003", "Charlie Green", "London", "Pending"),
        ("1004", "InvalidCity123", "InvalidCity123", "Pending"),
    ]


@pytest.mark.parametrize(("condition", "description"), [("Rain", "light rain"), ("Snow", "light snow"), ("Extreme", None)])
async def test_delay_conditions_mark_order_delayed_with_apology(fake_api, session, condition, description):
    api = await fake_api(
        scenarios={"Paris": {"main": condition, "description": description or ""}}, gemini_text="Apology from Gemini."
    )
    order = {"order_id": "9001", "customer": " Dana White", "city": "Paris", "status": PENDING_STATUS}

    result = await process_order(
        session,
        order,
        api_key=WEATHER_KEY,
        base_url=api.base_url,
        ai_api_key=GEMINI_KEY,
        ai_endpoint=api.gemini_url,
    )

    assert result.status == DELAYED_STATUS
    assert result.delayed is True
    assert result.apology == "Apology from Gemini."
    assert result.apology_source == "gemini"
    assert result.order["status"] == DELAYED_STATUS
    assert result.order["customer"] == " Dana White"


@pytest.mark.parametrize(("condition", "description"), [("Clear", "clear sky"), ("Clouds", "scattered clouds"), ("Mist", "mist")])
async def test_non_delay_conditions_keep_existing_status(fake_api, session, condition, description):
    api = await fake_api(scenarios={"Oslo": {"main": condition, "description": description}})
    order = {"order_id": "9002", "customer": "Eve", "city": "Oslo", "status": PENDING_STATUS}

    result = await process_order(
        session, order, api_key=WEATHER_KEY, base_url=api.base_url, ai_api_key=GEMINI_KEY, ai_endpoint=api.gemini_url
    )

    assert result.status == PENDING_STATUS
    assert result.delayed is False
    assert result.apology is None
    assert "apology_message" not in result.order
    assert api.gemini_requests == []


async def test_original_status_is_preserved_for_other_statuses(fake_api, session):
    api = await fake_api(scenarios={"Tokyo": {"main": "Clear"}})
    order = {"order_id": "9003", "customer": "Ken", "city": "Tokyo", "status": "Shipped"}
    result = await process_order(session, order, api_key=WEATHER_KEY, base_url=api.base_url)
    assert result.status == "Shipped"


async def test_invalid_city_is_isolated_and_other_orders_finish(fake_api, session):
    api = await fake_api(
        scenarios={
            "New York": {"main": "Rain", "description": "moderate rain"},
            "Mumbai": {"main": "Clear", "description": "clear sky"},
            "London": {"main": "Snow", "description": "light snow"},
        }
    )
    orders = [
        {"order_id": "1001", "customer": "Alice Smith", "city": "New York", "status": PENDING_STATUS},
        {"order_id": "1002", "customer": "Bob Jones", "city": "Mumbai", "status": PENDING_STATUS},
        {"order_id": "1003", "customer": "Charlie Green", "city": "London", "status": PENDING_STATUS},
        {"order_id": "1004", "customer": "InvalidCity123", "city": "InvalidCity123", "status": PENDING_STATUS},
    ]

    results = await process_orders(
        orders, session, api_key=WEATHER_KEY, base_url=api.base_url, ai_api_key=GEMINI_KEY, ai_endpoint=api.gemini_url
    )

    by_id = {result.order_id: result for result in results}
    assert len(results) == 4
    assert by_id["1004"].failed is True
    assert by_id["1004"].status == PENDING_STATUS
    assert by_id["1004"].error_type == "CityNotFoundError"
    assert by_id["1004"].apology is None
    assert "weather_condition" not in by_id["1004"].order
    assert by_id["1001"].status == DELAYED_STATUS
    assert by_id["1002"].status == PENDING_STATUS
    assert by_id["1003"].status == DELAYED_STATUS


async def test_one_server_error_does_not_stop_the_other_three(fake_api, session):
    api = await fake_api(
        scenarios={
            "New York": {"main": "Rain", "description": "moderate rain"},
            "Mumbai": {"main": "Clear"},
            "London": {"status": 500},
        }
    )
    orders = [
        {"order_id": f"100{i}", "customer": f"C{i}", "city": city, "status": PENDING_STATUS}
        for i, city in enumerate(["New York", "Mumbai", "London", "InvalidCity123"], start=1)
    ]
    results = await process_orders(orders, session, api_key=WEATHER_KEY, base_url=api.base_url)

    assert len(results) == 4
    assert {result.order_id for result in results if result.failed} == {"1003", "1004"}
    assert all(result.status == PENDING_STATUS for result in results if result.failed)
    assert [result.status for result in results if not result.failed] == [DELAYED_STATUS, PENDING_STATUS]


async def test_malformed_api_response_is_recorded_not_delayed(fake_api, session):
    api = await fake_api(scenarios={"Cairo": {"payload": {"cod": 200, "main": {"temp": 30}}}})
    order = {"order_id": "9004", "customer": "Salma", "city": "Cairo", "status": PENDING_STATUS}
    result = await process_order(session, order, api_key=WEATHER_KEY, base_url=api.base_url)

    assert result.failed is True
    assert result.error_type == "MalformedResponseError"
    assert result.status == PENDING_STATUS
    assert result.order["status"] == PENDING_STATUS


def test_save_orders_writes_valid_json_and_preserves_fields(tmp_path: Path):
    orders = [
        {"order_id": "1001", "customer": "Alice Smith", "city": "New York", "status": DELAYED_STATUS},
        {"order_id": "1002", "customer": "Bob Jones", "city": "Mumbai", "status": PENDING_STATUS},
    ]
    target = tmp_path / "nested" / "orders.json"

    save_orders(orders, target)

    assert json.loads(target.read_text(encoding="utf-8")) == orders
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(target.parent.glob(".*.tmp"))


def test_save_orders_leaves_file_untouched_on_unserialisable_data(tmp_path: Path):
    target = tmp_path / "orders.json"
    original = [{"order_id": "1001", "status": PENDING_STATUS}]
    save_orders(original, target)

    with pytest.raises(TypeError):
        save_orders([{"order_id": object()}], target)

    assert json.loads(target.read_text(encoding="utf-8")) == original
    assert not list(tmp_path.glob(".*.tmp"))
