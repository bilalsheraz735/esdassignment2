"""
order_core.py - the ONE place where the "place an order" business rules live.

Both front doors import this module:
    app.py          (HTTP  POST /order)
    grpc_server.py  (gRPC  OrderService/PlaceOrder)

Shared rules:
    1. Validation      - idempotency_key must be a 1-100 char string of [A-Za-z0-9._:-]
    2. Idempotency     - one key = one order, across BOTH protocols
    3. Order IDs       - ord_<8 hex chars>, generated once per key
    4. Kitchen         - simulated cook time (5-10 s by default)

Shared source of truth:
    SQLite file orders.db. Both processes read and write the same table, so a key
    first used over HTTP is recognised by gRPC and vice versa.
    (In-memory dicts cannot be shared between two processes.)

Reset all orders:   py order_core.py reset
"""
import logging
import os
import random
import re
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from prometheus_client import Counter
from pythonjsonlogger import jsonlogger

DB_PATH = os.environ.get("ORDERS_DB", "orders.db")
LOG_FILE = os.environ.get("LOG_FILE", "app-logs.json")
COOK_TIME_MIN = float(os.environ.get("COOK_TIME_MIN", "5.0"))
COOK_TIME_MAX = float(os.environ.get("COOK_TIME_MAX", "10.0"))

KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
STATUS_READY = "Order ready!"
STATUS_REPLAY = "Order already processed (safe repeat)"

# ------------------------------------------------------------------
# Metric that is the same for both protocols ("other missing metric")
# Label `channel` is bounded: "http" or "grpc".
# ------------------------------------------------------------------
IDEMPOTENT_REPLAYS = Counter(
    "idempotent_replays_total",
    "Requests answered from the idempotency store instead of cooking a new order",
    ["channel"],
)


# ------------------------------------------------------------------
# Logging (same JSON format as A1, same file Filebeat ships)
# ------------------------------------------------------------------
def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        handler.setFormatter(jsonlogger.JsonFormatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
    return logger


def new_trace_id() -> str:
    return uuid.uuid4().hex


# ------------------------------------------------------------------
# Errors and results
# ------------------------------------------------------------------
class InvalidOrder(ValueError):
    """Raised when the request breaks a validation rule (HTTP 400 / gRPC INVALID_ARGUMENT)."""


@dataclass
class OrderResult:
    order_id: str
    status: str
    replayed: bool


@dataclass
class KitchenMetrics:
    """Per-protocol metrics the core updates while cooking (names differ per server)."""
    placed: object               # Counter
    waiting: object              # Gauge
    durations: list = field(default_factory=list)  # Histograms/Summaries to observe


# ------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------
def validate_key(key) -> str:
    if key is None or key == "":
        raise InvalidOrder("idempotency_key is required")
    if not isinstance(key, str):
        raise InvalidOrder("idempotency_key must be a string")
    if not KEY_PATTERN.match(key):
        raise InvalidOrder("idempotency_key must be 1-100 characters of A-Z a-z 0-9 . _ : -")
    return key


# ------------------------------------------------------------------
# Store (SQLite)
# ------------------------------------------------------------------
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)  # autocommit
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                   idempotency_key TEXT PRIMARY KEY,
                   order_id        TEXT NOT NULL UNIQUE,
                   status          TEXT NOT NULL,      -- cooking | ready
                   channel         TEXT NOT NULL,      -- http | grpc (who created it)
                   trace_id        TEXT,
                   created_at      REAL NOT NULL,
                   completed_at    REAL
               )"""
        )


def reset_db() -> None:
    init_db()
    with _connect() as conn:
        conn.execute("DELETE FROM orders")


def _reserve(key: str, channel: str, trace_id: str):
    """Atomically claim the key. Returns (order_id, is_new)."""
    order_id = f"ord_{uuid.uuid4().hex[:8]}"
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO orders (idempotency_key, order_id, status, channel, trace_id, created_at) "
            "VALUES (?, ?, 'cooking', ?, ?, ?)",
            (key, order_id, channel, trace_id, time.time()),
        )
        if cur.rowcount == 1:
            return order_id, True
        row = conn.execute(
            "SELECT order_id FROM orders WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return row[0], False


def _complete(key: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE orders SET status = 'ready', completed_at = ? WHERE idempotency_key = ?",
            (time.time(), key),
        )


def get_order(key: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT order_id, status, channel FROM orders WHERE idempotency_key = ?", (key,)
        ).fetchone()
    return None if row is None else {"order_id": row[0], "status": row[1], "channel": row[2]}


# ------------------------------------------------------------------
# The operation
# ------------------------------------------------------------------
def place_order(key, *, channel: str, trace_id: str, logger: logging.Logger,
                service: str, metrics: KitchenMetrics) -> OrderResult:
    key = validate_key(key)
    order_id, is_new = _reserve(key, channel, trace_id)
    log_extra = {"request_id": order_id, "trace_id": trace_id, "service": service,
                 "channel": channel, "severity": "INFO"}

    if not is_new:
        IDEMPOTENT_REPLAYS.labels(channel=channel).inc()
        logger.info("Duplicate order prevented", extra=log_extra)
        return OrderResult(order_id, STATUS_REPLAY, replayed=True)

    logger.info("Order received", extra=log_extra)
    metrics.placed.inc()
    metrics.waiting.inc()
    start = time.perf_counter()
    try:
        time.sleep(random.uniform(COOK_TIME_MIN, COOK_TIME_MAX))  # the kitchen
        _complete(key)
        logger.info("Order complete", extra=log_extra)
        return OrderResult(order_id, STATUS_READY, replayed=False)
    finally:
        elapsed = time.perf_counter() - start
        for metric in metrics.durations:
            metric.observe(elapsed)
        metrics.waiting.dec()


init_db()

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "reset":
        reset_db()
        print(f"All orders deleted from {DB_PATH}")
    else:
        print("Usage: py order_core.py reset")
