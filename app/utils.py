import hashlib
import json
import unicodedata
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from urllib.parse import urlparse


def canonical_json(value: Any) -> str:
    def convert(item: Any) -> Any:
        if isinstance(item, dict):
            return {str(k): convert(v) for k, v in sorted(item.items())}
        if isinstance(item, (list, tuple)):
            return [convert(v) for v in item]
        if isinstance(item, uuid.UUID):
            return str(item)
        if isinstance(item, Decimal):
            return str(item)
        return item

    return json.dumps(convert(value), ensure_ascii=False, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def text_hash(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    return hashlib.sha256(normalized.encode()).hexdigest()


def normalize_label(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).strip().lower().split())


def decimal6(value: Decimal | float | int) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def canonical_host(value: str | None) -> str:
    """Host used for duplicate detection: no scheme, no `www.`, no port or dot.

    The same site arrives as `https://example.com/`, `example.com` and
    `https://www.example.com` — all three must collapse to `example.com`,
    otherwise the S-register shows two entries for one domain.
    """
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        host = (urlparse(raw).hostname or "").strip(".").lower()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    # нестандартный порт — это другой сайт (частый случай локальных проверок)
    try:
        port = urlparse(raw).port
    except ValueError:
        port = None
    if port and port not in (80, 443):
        host = f"{host}:{port}"
    return host
