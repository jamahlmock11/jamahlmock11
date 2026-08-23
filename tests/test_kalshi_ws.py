"""Tests for Kalshi WebSocket subscriber and live order book."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_signer import KalshiRequestSigner
from kalshi_ws import KalshiWebSocketSubscriber, WS_URL_PROD
from orderbook_live import LiveOrderBook


def test_signer_websocket_headers_use_ws_path(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_path = tmp_path / "kalshi.pem"
    pem_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    signer = KalshiRequestSigner("test-key-id", str(pem_path))
    headers = signer.websocket_headers()
    assert headers["KALSHI-ACCESS-KEY"] == "test-key-id"
    assert headers["KALSHI-ACCESS-SIGNATURE"]
    assert headers["KALSHI-ACCESS-TIMESTAMP"]


def test_subscriber_uses_header_auth_not_query_params():
    signer = MagicMock(spec=KalshiRequestSigner)
    signer.ready = True
    signer.websocket_headers.return_value = {
        "KALSHI-ACCESS-KEY": "k",
        "KALSHI-ACCESS-TIMESTAMP": "1",
        "KALSHI-ACCESS-SIGNATURE": "sig",
    }
    sub = KalshiWebSocketSubscriber(signer, is_demo=False)
    assert sub.ws_url == WS_URL_PROD
    headers = sub._auth_headers()
    assert "api_key" not in headers
    assert headers["KALSHI-ACCESS-SIGNATURE"] == "sig"


@pytest.mark.asyncio
async def test_live_orderbook_snapshot_and_delta():
    book = LiveOrderBook(ticker="KXBTC15M-TEST")
    snap = {
        "type": "orderbook_snapshot",
        "msg": {
            "market_ticker": "KXBTC15M-TEST",
            "orderbook_fp": {
                "yes_dollars": [["0.45", "10"], ["0.44", "5"]],
                "no_dollars": [["0.55", "8"]],
            },
        },
    }
    top = await book.apply_message(snap)
    assert top.yes_bid == pytest.approx(0.45)
    assert top.yes_ask == pytest.approx(0.45)
    assert top.updated is True

    delta = {
        "type": "orderbook_delta",
        "msg": {
            "market_ticker": "KXBTC15M-TEST",
            "side": "yes",
            "price_dollars": "0.46",
            "delta": 12,
        },
    }
    top = await book.apply_message(delta)
    assert 0.46 in book.yes_bids
    assert book.yes_bids[0.46] == pytest.approx(12)


@pytest.mark.asyncio
async def test_subscriber_routes_orderbook_messages():
    signer = MagicMock(spec=KalshiRequestSigner)
    signer.ready = True
    signer.websocket_headers.return_value = {
        "KALSHI-ACCESS-KEY": "k",
        "KALSHI-ACCESS-TIMESTAMP": "1",
        "KALSHI-ACCESS-SIGNATURE": "s",
    }

    received: list[dict] = []

    async def callback(payload):
        received.append(payload)

    sub = KalshiWebSocketSubscriber(signer)

    messages = [
        json.dumps({"type": "subscribed", "msg": {}}),
        json.dumps({"type": "orderbook_snapshot", "msg": {"market_ticker": "T"}}),
        json.dumps({"type": "heartbeat"}),
    ]

    class FakeWS:
        def __init__(self):
            self.sent: list[str] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def send(self, payload):
            self.sent.append(payload)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not messages:
                sub.keep_alive = False
                raise StopAsyncIteration
            return messages.pop(0)

    fake = FakeWS()

    with patch("websockets.connect", return_value=fake):
        await sub.stream_order_book("KXBTC15M-TEST", callback)

    assert any(item["type"] == "orderbook_snapshot" for item in received)
    subscribe = json.loads(fake.sent[0])
    assert subscribe["params"]["channels"] == ["orderbook_delta", "ticker"]
    assert subscribe["params"]["market_tickers"] == ["KXBTC15M-TEST"]
