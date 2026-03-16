import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

log = logging.getLogger("roostoo_client")


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
        log.debug(
            "RoostooClient initialized | base_url=%s  api_key=%s...  timeout=%d",
            self.base_url,
            self.api_key[:6] if self.api_key else "<empty>",
            self.timeout,
        )

    def _timestamp(self) -> int:
        return int(time.time() * 1000)

    def _encode(self, params: dict[str, Any]) -> str:
        """Sort keys alphabetically and URL-encode into a query string."""
        return urlencode({k: params[k] for k in sorted(params)})

    def _sign(self, params: dict[str, Any]) -> str:
        encoded = self._encode(params)
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            encoded.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        log.debug("SIGN  string=%r  sig=%s...%s", encoded, signature[:8], signature[-8:])
        return signature

    def _signed_headers(self, params: dict[str, Any]) -> dict[str, str]:
        return {
            "RST-API-KEY": self.api_key,
            "MSG-SIGNATURE": self._sign(params),
        }

    def _log_response(self, method: str, path: str, resp: requests.Response) -> None:
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        log.debug(
            "API RESPONSE  %s %s  status=%d  body=%s",
            method,
            path,
            resp.status_code,
            json.dumps(body, indent=2, default=str),
        )

    def _get(
        self, path: str, params: dict[str, Any] | None = None, signed: bool = False
    ) -> dict[str, Any]:
        payload = dict(params or {})
        if signed or "timestamp" in payload:
            payload.setdefault("timestamp", self._timestamp())
        headers = self._signed_headers(payload) if signed else None
        log.debug(
            "API REQUEST   GET %s  signed=%s  params=%s",
            path,
            signed,
            json.dumps(payload, default=str),
        )
        response = requests.get(
            f"{self.base_url}{path}",
            params=payload,
            headers=headers,
            timeout=self.timeout,
        )
        self._log_response("GET", path, response)
        response.raise_for_status()
        return response.json()

    def _post(
        self, path: str, params: dict[str, Any], signed: bool = True
    ) -> dict[str, Any]:
        payload = dict(params)
        if signed:
            payload.setdefault("timestamp", self._timestamp())
        encoded_body = self._encode(payload)
        headers = self._signed_headers(payload) if signed else {}
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        log.debug(
            "API REQUEST   POST %s  signed=%s  body=%s",
            path,
            signed,
            encoded_body,
        )
        response = requests.post(
            f"{self.base_url}{path}",
            data=encoded_body,
            headers=headers,
            timeout=self.timeout,
        )
        self._log_response("POST", path, response)
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
        log.info(
            "PLACE ORDER   pair=%s  side=%s  qty=%.8f  price=%.2f  notional=%.2f",
            pair,
            side.upper(),
            quantity,
            price,
            quantity * price,
        )
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
        log.info("CANCEL ORDER  order_id=%s  pair=%s", order_id, pair)
        payload: dict[str, Any] = {}
        if order_id is not None:
            payload["order_id"] = order_id
        elif pair is not None:
            payload["pair"] = pair
        return self._post("/v3/cancel_order", payload)
