import requests

from src.config import ROOSTOO_API_KEY, ROOSTOO_SECRET_KEY, ROOSTOO_BASE_URL
from src.roostoo.auth import generate_signature
from src.utils import now_ms


class RoostooClient:
    def __init__(self, api_key=None, secret_key=None, base_url=None):
        self.api_key = api_key or ROOSTOO_API_KEY
        self.secret_key = secret_key or ROOSTOO_SECRET_KEY
        self.base_url = (base_url or ROOSTOO_BASE_URL).rstrip("/")

    def _signed_headers(self, params: dict) -> dict:
        signature = generate_signature(params, self.secret_key)
        return {
            "RST-API-KEY": self.api_key,
            "MSG-SIGNATURE": signature,
        }

    def get_server_time(self):
        r = requests.get(f"{self.base_url}/v3/serverTime", timeout=10)
        r.raise_for_status()
        return r.json()

    def get_exchange_info(self):
        r = requests.get(f"{self.base_url}/v3/exchangeInfo", timeout=10)
        r.raise_for_status()
        return r.json()

    def get_ticker(self, pair=None):
        params = {"timestamp": now_ms()}
        if pair:
            params["pair"] = pair

        r = requests.get(f"{self.base_url}/v3/ticker", params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def get_balance(self):
        params = {"timestamp": now_ms()}
        r = requests.get(
            f"{self.base_url}/v3/balance",
            params=params,
            headers=self._signed_headers(params),
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def pending_count(self):
        params = {"timestamp": now_ms()}
        r = requests.get(
            f"{self.base_url}/v3/pending_count",
            params=params,
            headers=self._signed_headers(params),
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def place_order(self, pair: str, side: str, quantity: float, price: float | None = None):
        payload = {
            "timestamp": now_ms(),
            "pair": pair,
            "side": side.upper(),
            "quantity": quantity,
        }

        if price is None:
            payload["type"] = "MARKET"
        else:
            payload["type"] = "LIMIT"
            payload["price"] = price

        r = requests.post(
            f"{self.base_url}/v3/place_order",
            data=payload,
            headers={
                **self._signed_headers(payload),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def query_order(self, order_id=None, pair=None, pending_only=None):
        payload = {"timestamp": now_ms()}
        if order_id is not None:
            payload["order_id"] = order_id
        if pair is not None:
            payload["pair"] = pair
        if pending_only is not None:
            payload["pending_only"] = str(pending_only).lower()

        r = requests.post(
            f"{self.base_url}/v3/query_order",
            data=payload,
            headers={
                **self._signed_headers(payload),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def cancel_order(self, pair: str, order_id=None):
        payload = {
            "timestamp": now_ms(),
            "pair": pair,
        }
        if order_id is not None:
            payload["order_id"] = order_id

        r = requests.post(
            f"{self.base_url}/v3/cancel_order",
            data=payload,
            headers={
                **self._signed_headers(payload),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json()