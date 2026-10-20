"""
app.py - Fast-Food HTTP API (Assignment 2)

POST /order    place an order (shared rules in order_core.py)
GET  /metrics  Prometheus scrape endpoint (not rate limited, not counted)

Run:  py app.py            (port 5000, override with env PORT)
"""
import math
import os
import threading
import time

from flask import Flask, g, jsonify, request
from prometheus_client import (CONTENT_TYPE_LATEST, Counter, Gauge, Histogram,
                               Summary, generate_latest)
from werkzeug.exceptions import HTTPException

import order_core

SERVICE = "food-api"
logger = order_core.get_logger("fast-food-api")
app = Flask(__name__)

# ==========================================
# METRICS
# ==========================================
# --- kept from A1 (dashboards from A1 still work) ---
ORDERS_PLACED = Counter("orders_placed_total", "Total number of orders placed")
ORDERS_WAITING = Gauge("orders_waiting", "Number of orders currently being prepared")
COOK_TIME_HIST = Histogram("cook_time_seconds", "Time taken to prepare an order",
                           buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 15.0, 20.0, 30.0])
COOK_TIME_SUM = Summary("cook_time_summary_seconds", "Summary of cook times")
# (A1's demo_requests_total{request_id} was REMOVED: unbounded label, one series per order)

# --- new in A2: calls by method/route/status and end-to-end duration ---
# Labels are bounded: method (POST/GET...), route (the Flask rule, never the raw path), status (HTTP code)
HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests handled",
                        ["method", "route", "status"])
HTTP_DURATION = Histogram("http_request_duration_seconds",
                          "Server-side time to handle an HTTP request (includes cooking)",
                          ["method", "route"],
                          buckets=[0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 6.0, 7.0,
                                   8.0, 9.0, 10.0, 15.0, 20.0, 30.0])

KITCHEN = order_core.KitchenMetrics(placed=ORDERS_PLACED, waiting=ORDERS_WAITING,
                                    durations=[COOK_TIME_HIST, COOK_TIME_SUM])

# Pre-create expected series at 0 so rate() sees the very first error, not just later ones
for _status in ("200", "400", "429", "500"):
    HTTP_REQUESTS.labels("POST", "/order", _status)
order_core.IDEMPOTENT_REPLAYS.labels(channel="http")


# ==========================================
# RATE LIMITER - fixed window, single instance, shared by ALL callers
# ==========================================
class FixedWindowRateLimiter:
    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window = window_seconds
        self._lock = threading.Lock()  # Flask serves requests on several threads
        self.reset()

    def reset(self):
        self.window_start = time.monotonic()
        self.count = 0

    def try_acquire(self):
        """Returns (allowed, retry_after_seconds)."""
        with self._lock:
            now = time.monotonic()
            if now - self.window_start >= self.window:
                self.window_start, self.count = now, 0
            if self.count >= self.max_requests:
                remaining = self.window - (now - self.window_start)
                return False, max(1, math.ceil(remaining))  # never "Retry-After: 0"
            self.count += 1
            return True, 0


RATE_LIMITER = FixedWindowRateLimiter(max_requests=5, window_seconds=10)


# ==========================================
# REQUEST HOOKS - trace id + metrics for every request
# ==========================================
def _route_label() -> str:
    return request.url_rule.rule if request.url_rule else "unmatched"


@app.before_request
def start_request():
    g.start = time.perf_counter()
    incoming = request.headers.get("X-Request-ID", "")
    g.trace_id = incoming if 0 < len(incoming) <= 64 else order_core.new_trace_id()


@app.after_request
def finish_request(response):
    response.headers["X-Request-ID"] = g.get("trace_id", "")
    route = _route_label()
    if route != "/metrics":  # don't count Prometheus' own scrapes
        HTTP_REQUESTS.labels(request.method, route, str(response.status_code)).inc()
        HTTP_DURATION.labels(request.method, route).observe(time.perf_counter() - g.get("start", time.perf_counter()))
    return response


# ==========================================
# CONSISTENT ERROR FORMAT: every error is {"error": "<message>"}
# ==========================================
def error(message: str, status: int, headers=None):
    return jsonify({"error": message}), status, headers or {}


@app.errorhandler(HTTPException)
def handle_http_error(exc):          # 404, 405, 415, ...
    return error(exc.description or exc.name, exc.code)


@app.errorhandler(Exception)
def handle_unexpected(exc):          # anything else -> 500, never an HTML page
    logger.exception("Unhandled error", extra={"trace_id": g.get("trace_id"), "service": SERVICE,
                                               "severity": "ERROR"})
    return error("Internal server error", 500)


# ==========================================
# POST /order
# ==========================================
@app.route("/order", methods=["POST"])
def place_order():
    # 1. Rate limit first, so floods are rejected before any work is done
    allowed, retry_after = RATE_LIMITER.try_acquire()
    if not allowed:
        logger.warning("Rate limit exceeded", extra={"trace_id": g.trace_id, "service": SERVICE,
                                                     "severity": "WARNING"})
        return error("Too Many Requests", 429, {"Retry-After": str(retry_after)})

    # 2. Body must be a JSON object
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return error("Request body must be a JSON object", 400)

    # 3. Shared business rules (validation, idempotency, kitchen)
    try:
        result = order_core.place_order(data.get("idempotency_key"), channel="http",
                                        trace_id=g.trace_id, logger=logger,
                                        service=SERVICE, metrics=KITCHEN)
    except order_core.InvalidOrder as exc:
        return error(str(exc), 400)

    return jsonify({"status": result.status, "order_id": result.order_id}), 200


@app.route("/metrics", methods=["GET"])
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    logger.info("Server started", extra={"service": SERVICE, "severity": "INFO", "port": port})
    app.run(port=port, threaded=True)
