import hashlib
import hmac
import os
import time
from typing import Any
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv


class RoostooClient:
    def __init__(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        base_url: str | None = None,
        timeout: int = 15,
    ) -> None:
        load_dotenv()
        self.api_key = api_key or os.getenv("ROOSTOO_API_KEY", "")
        self.secret_key = secret_key or os.getenv("ROOSTOO_SECRET_KEY", "")
        self.base_url = (
            base_url or os.getenv("ROOSTOO_BASE_URL") or "https://mock-api.roostoo.com"
        ).rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _timestamp(self) -> str:
        return str(int(time.time() * 1000))

    def _encode(self, params: dict[str, Any]) -> str:
        ordered = {key: params[key] for key in sorted(params)}
        return urlencode(ordered)

    def _headers(self, encoded_params: str) -> dict[str, str]:
        signature = hmac.new(
            self.secret_key.encode(), encoded_params.encode(), hashlib.sha256
        ).hexdigest()
        return {
            "RST-API-KEY": self.api_key,
            "MSG-SIGNATURE": signature,
        }

    def _get(
        self, path: str, params: dict[str, Any] | None = None, signed: bool = False
    ) -> dict[str, Any]:
        payload = dict(params or {})
        if signed or "timestamp" in payload:
            payload.setdefault("timestamp", self._timestamp())
        headers = self._headers(self._encode(payload)) if signed else None
        response = self.session.get(
            f"{self.base_url}{path}",
            params=payload,
            headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def _post(
        self, path: str, params: dict[str, Any], signed: bool = True
    ) -> dict[str, Any]:
        payload = dict(params)
        if signed:
            payload.setdefault("timestamp", self._timestamp())
        encoded = self._encode(payload)
        headers = self._headers(encoded) if signed else {}
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        response = self.session.post(
            f"{self.base_url}{path}",
            data=encoded,
            headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def server_time(self) -> dict[str, Any]:
        return self._get("/v3/serverTime")

    def exchange_info(self) -> dict[str, Any]:
        return self._get("/v3/exchangeInfo")

    def ticker(self, pair: str) -> dict[str, Any]:
        return self._get("/v3/ticker", {"pair": pair, "timestamp": self._timestamp()})

    def balance(self) -> dict[str, Any]:
        return self._get("/v3/balance", signed=True)

    def pending_count(self) -> dict[str, Any]:
        return self._get("/v3/pending_count", signed=True)

    def place_limit_order(
        self, pair: str, side: str, quantity: float, price: float
    ) -> dict[str, Any]:
        return self._post(
            "/v3/place_order",
            {
                "pair": pair,
                "side": side.upper(),
                "type": "LIMIT",
                "quantity": quantity,
                "price": price,
            },
        )

    def query_orders(
        self, pair: str, pending_only: bool = False, limit: int = 100
    ) -> dict[str, Any]:
        return self._post(
            "/v3/query_order",
            {
                "pair": pair,
                "pending_only": "TRUE" if pending_only else "FALSE",
                "limit": limit,
            },
        )

    def query_order(self, order_id: int) -> dict[str, Any]:
        return self._post("/v3/query_order", {"order_id": order_id})

    def cancel_order(
        self, order_id: int | None = None, pair: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if order_id is not None:
            payload["order_id"] = order_id
        elif pair is not None:
            payload["pair"] = pair
        return self._post("/v3/cancel_order", payload)
