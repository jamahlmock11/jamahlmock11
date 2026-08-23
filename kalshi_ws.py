"""Authenticated Kalshi WebSocket subscriber for live order book deltas."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from kalshi_signer import KalshiRequestSigner

logger = logging.getLogger("KalshiWS")

WS_URL_PROD = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
WS_URL_DEMO = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"

OrderBookCallback = Callable[[dict[str, Any]], Awaitable[None]]


class KalshiWebSocketSubscriber:
    """
    Connects to Kalshi's authenticated WebSocket and streams orderbook deltas.

    Auth is via handshake headers (not URL query params). See:
    https://docs.kalshi.com/getting_started/quick_start_websockets
    """

    def __init__(self, signer: KalshiRequestSigner, *, is_demo: bool = False) -> None:
        self.signer = signer
        self.ws_url = WS_URL_DEMO if is_demo else WS_URL_PROD
        self.logger = logging.getLogger("KalshiWS")
        self.keep_alive = True
        self._message_id = 1

    def _auth_headers(self) -> dict[str, str]:
        if not self.signer.ready:
            raise RuntimeError("Kalshi signer is not configured")
        return self.signer.websocket_headers()

    async def stream_order_book(self, ticker: str, callback_func: OrderBookCallback) -> None:
        """
        Subscribe to orderbook_delta (+ ticker fallback) and route messages to callback.
        Reconnects automatically with exponential backoff.
        """
        try:
            import websockets
            from websockets.exceptions import ConnectionClosed
        except ImportError as exc:
            raise RuntimeError("websockets package is required: pip install websockets") from exc

        backoff = 3.0
        while self.keep_alive:
            try:
                self.logger.info("Connecting to Kalshi order book stream for %s", ticker)
                async with websockets.connect(
                    self.ws_url,
                    additional_headers=self._auth_headers(),
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    self.logger.info("WebSocket handshake complete")
                    backoff = 3.0
                    await self._subscribe(ws, ticker)

                    async for message in ws:
                        if not self.keep_alive:
                            break
                        try:
                            data = json.loads(message)
                        except json.JSONDecodeError:
                            continue
                        msg_type = data.get("type")
                        if msg_type in {"orderbook_snapshot", "orderbook_delta", "ticker", "subscribed"}:
                            await callback_func(data)
                        elif msg_type == "error":
                            self.logger.error("Kalshi WS error: %s", data)

            except ConnectionClosed as exc:
                self.logger.warning("WebSocket closed: %s — reconnecting in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.error("WebSocket handler exception: %s — reconnecting in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _subscribe(self, ws, ticker: str) -> None:
        payload = {
            "id": self._message_id,
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta", "ticker"],
                "market_tickers": [ticker],
            },
        }
        self._message_id += 1
        await ws.send(json.dumps(payload))
        self.logger.info("Subscribed to orderbook_delta + ticker for %s", ticker)

    def stop(self) -> None:
        self.keep_alive = False
