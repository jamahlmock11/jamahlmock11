"""Kalshi WebSocket client with RSA-PSS handshake auth."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

import websockets
from websockets.asyncio.client import ClientConnection

from kalshi_bot.venues.kalshi import KalshiClient

logger = logging.getLogger(__name__)

MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


def websocket_url(rest_base_url: str) -> str:
    parsed = urlparse(rest_base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/trade-api/ws/v2"


def websocket_headers(kalshi: KalshiClient) -> dict[str, str]:
    if not kalshi.authenticated:
        raise RuntimeError("Kalshi API key and private key required for WebSocket auth")
    return kalshi._headers("GET", "/trade-api/ws/v2")


class KalshiWebSocketClient:
    def __init__(self, kalshi: KalshiClient):
        self.kalshi = kalshi
        self.ws_url = websocket_url(kalshi.base_url)
        self._ws: ClientConnection | None = None
        self._msg_id = 0

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def connect(self) -> ClientConnection:
        headers = websocket_headers(self.kalshi)
        self._ws = await websockets.connect(
            self.ws_url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=20,
        )
        logger.info("WebSocket connected to %s", self.ws_url)
        return self._ws

    async def subscribe_orderbook(self, market_tickers: list[str]) -> None:
        if not self._ws or not market_tickers:
            return
        payload = {
            "id": self._next_id(),
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": market_tickers,
            },
        }
        await self._ws.send(json.dumps(payload))
        logger.info("Subscribed to orderbook_delta for %d markets", len(market_tickers))

    async def subscribe_ticker(self, market_tickers: list[str]) -> None:
        if not self._ws or not market_tickers:
            return
        payload = {
            "id": self._next_id(),
            "cmd": "subscribe",
            "params": {
                "channels": ["ticker"],
                "market_tickers": market_tickers,
            },
        }
        await self._ws.send(json.dumps(payload))
        logger.info("Subscribed to ticker for %d markets", len(market_tickers))

    async def listen(self, handler: MessageHandler) -> None:
        if not self._ws:
            raise RuntimeError("WebSocket not connected")
        async for raw in self._ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Ignoring non-JSON WebSocket payload")
                continue
            await handler(data)

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def run(
        self,
        market_tickers: list[str],
        handler: MessageHandler,
        *,
        reconnect_delay_sec: float = 5.0,
    ) -> None:
        while True:
            try:
                await self.connect()
                await self.subscribe_orderbook(market_tickers)
                await self.subscribe_ticker(market_tickers)
                await self.listen(handler)
            except asyncio.CancelledError:
                await self.close()
                raise
            except Exception as exc:
                logger.error("WebSocket error: %s", exc)
                await self.close()
                await asyncio.sleep(reconnect_delay_sec)
