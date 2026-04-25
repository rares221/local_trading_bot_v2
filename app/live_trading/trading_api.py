"""
Trading API — placeholder module.

Defines the interface a real broker integration will implement.
All functions return a placeholder response until a broker is connected.
"""
from __future__ import annotations

_PLACEHOLDER = {
    "status": "placeholder",
    "connected": False,
    "message": "Trading API not connected. Connect a broker to enable order execution.",
}


def get_status() -> dict:
    return _PLACEHOLDER.copy()


def place_order(
    ticker: str,
    direction: str,
    qty: float,
    order_type: str = "market",
    price: float | None = None,
) -> dict:
    return {**_PLACEHOLDER, "action": "place_order", "ticker": ticker, "direction": direction}


def cancel_order(order_id: str) -> dict:
    return {**_PLACEHOLDER, "action": "cancel_order", "order_id": order_id}


def get_open_orders() -> dict:
    return {**_PLACEHOLDER, "orders": []}


def get_account_balance() -> dict:
    return {**_PLACEHOLDER, "balance": None}
