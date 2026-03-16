import hashlib
import hmac


def build_query_string(params: dict) -> str:
    return "&".join(f"{k}={params[k]}" for k in sorted(params.keys()))


def generate_signature(params: dict, secret_key: str) -> str:
    query_string = build_query_string(params)
    return hmac.new(
        secret_key.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()