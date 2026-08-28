"""Tests for Kalshi WebSocket URL and auth headers."""

from __future__ import annotations

from kalshi_bot.venues.kalshi import KalshiClient
from kalshi_bot.venues.kalshi_ws import websocket_headers, websocket_url


def test_websocket_url_from_rest_base():
    url = websocket_url("https://api.elections.kalshi.com/trade-api/v2")
    assert url == "wss://api.elections.kalshi.com/trade-api/ws/v2"


def test_websocket_headers_require_auth():
    client = KalshiClient(
        base_url="https://api.elections.kalshi.com/trade-api/v2",
        api_key_id="",
        private_key_path="",
    )
    try:
        websocket_headers(client)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "required" in str(exc).lower()
