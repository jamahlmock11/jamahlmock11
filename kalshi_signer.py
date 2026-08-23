"""RSA-PSS request signing shared by REST and WebSocket clients."""

from __future__ import annotations

import base64
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


class KalshiRequestSigner:
    """Sign Kalshi REST and WebSocket requests."""

    WS_SIGN_PATH = "/trade-api/ws/v2"

    def __init__(self, api_key_id: str, private_key_path: str) -> None:
        self.api_key_id = api_key_id
        self._private_key = None
        path = Path(private_key_path).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if path.exists():
            pem = path.read_bytes()
            self._private_key = serialization.load_pem_private_key(pem, password=None)

    @property
    def ready(self) -> bool:
        return bool(self.api_key_id and self._private_key is not None)

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        if self._private_key is None:
            raise RuntimeError("Kalshi private key not loaded")
        message = f"{timestamp_ms}{method.upper()}{path.split('?')[0]}".encode()
        sig = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode()

    def generate_headers(self, *, method: str = "GET", full_path: str) -> dict[str, str]:
        """Return Kalshi auth headers for REST or WebSocket handshake."""
        ts = str(int(time.time() * 1000))
        return {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts, method, full_path),
        }

    def websocket_headers(self) -> dict[str, str]:
        return self.generate_headers(method="GET", full_path=self.WS_SIGN_PATH)
