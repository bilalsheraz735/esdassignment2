"""
grpc_server.py - Fast-Food gRPC service (Assignment 2, Part B)

fastfood.OrderService/PlaceOrder on :50051, Prometheus metrics on :5001.
Uses the SAME business rules and store as the HTTP API (order_core.py);
it does not call the HTTP endpoint.

Run:  py grpc_server.py      (override ports with env GRPC_PORT / METRICS_PORT)
"""
import os
import time
from concurrent import futures

import grpc
from prometheus_client import Counter, Gauge, Histogram, start_http_server

import order_core
import order_pb2
import order_pb2_grpc

SERVICE = "grpc-food-api"
METHOD = "PlaceOrder"
TRACE_HEADER = "x-request-id"
logger = order_core.get_logger("grpc-fast-food-api")

# ==========================================
# METRICS
# ==========================================
# --- kitchen metrics (mirror the HTTP ones) ---
ORDERS_PLACED = Counter("grpc_orders_placed_total", "Total number of gRPC orders placed")
ORDERS_WAITING = Gauge("grpc_orders_waiting", "Number of gRPC orders currently being prepared")
COOK_TIME_HIST = Histogram("grpc_cook_time_seconds", "Time taken to prepare a gRPC order",
                           buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 15.0, 20.0, 30.0])

# --- RPC metrics: calls by method and status code, and duration ---
# Labels are bounded: grpc_method (one per RPC), grpc_code (17 canonical codes)
RPC_HANDLED = Counter("grpc_server_handled_total", "gRPC calls completed on the server",
                      ["grpc_method", "grpc_code"])
RPC_DURATION = Histogram("grpc_server_handling_seconds",
                         "Server-side time to handle a gRPC call (includes cooking)",
                         ["grpc_method"],
                         buckets=[0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 6.0, 7.0,
                                  8.0, 9.0, 10.0, 15.0, 20.0, 30.0])

KITCHEN = order_core.KitchenMetrics(placed=ORDERS_PLACED, waiting=ORDERS_WAITING,
                                    durations=[COOK_TIME_HIST])

# Pre-create expected series at 0 so rate() sees the very first error, not just later ones
for _code in ("OK", "INVALID_ARGUMENT", "DEADLINE_EXCEEDED", "CANCELLED", "INTERNAL"):
    RPC_HANDLED.labels(METHOD, _code)
order_core.IDEMPOTENT_REPLAYS.labels(channel="grpc")


def _trace_id_from(context) -> str:
    for key, value in context.invocation_metadata():
        if key == TRACE_HEADER and 0 < len(value) <= 64:
            return value
    return order_core.new_trace_id()


# ==========================================
# SERVICE
# ==========================================
class OrderServiceServicer(order_pb2_grpc.OrderServiceServicer):
    def PlaceOrder(self, request, context):
        start = time.perf_counter()
        trace_id = _trace_id_from(context)
        context.set_trailing_metadata(((TRACE_HEADER, trace_id),))  # echo back to the caller
        code = grpc.StatusCode.OK
        try:
            try:
                result = order_core.place_order(request.idempotency_key, channel="grpc",
                                                trace_id=trace_id, logger=logger,
                                                service=SERVICE, metrics=KITCHEN)
            except order_core.InvalidOrder as exc:
                code = grpc.StatusCode.INVALID_ARGUMENT
                logger.warning("Invalid order", extra={"trace_id": trace_id, "service": SERVICE,
                                                       "severity": "WARNING", "reason": str(exc)})
                context.abort(code, str(exc))

            # The caller may have given up while we were cooking (deadline or cancel).
            # The order still exists (key is stored), so a retry gets it back.
            if not context.is_active():
                remaining = context.time_remaining()
                code = (grpc.StatusCode.DEADLINE_EXCEEDED
                        if remaining is not None and remaining <= 0 else grpc.StatusCode.CANCELLED)
                logger.warning("Caller gave up before the response", extra={
                    "request_id": result.order_id, "trace_id": trace_id, "service": SERVICE,
                    "severity": "WARNING", "grpc_code": code.name})

            return order_pb2.OrderResponse(status=result.status, order_id=result.order_id)
        except Exception:
            if code == grpc.StatusCode.OK:      # not one of our own aborts -> real bug
                code = grpc.StatusCode.INTERNAL
                logger.exception("Unhandled error", extra={"trace_id": trace_id, "service": SERVICE,
                                                           "severity": "ERROR"})
            raise
        finally:
            RPC_HANDLED.labels(METHOD, code.name).inc()
            RPC_DURATION.labels(METHOD).observe(time.perf_counter() - start)


def create_server(port: int) -> grpc.Server:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    order_pb2_grpc.add_OrderServiceServicer_to_server(OrderServiceServicer(), server)
    server.add_insecure_port(f"[::]:{port}")
    return server


def serve():
    grpc_port = int(os.environ.get("GRPC_PORT", "50051"))
    metrics_port = int(os.environ.get("METRICS_PORT", "5001"))
    start_http_server(metrics_port)
    server = create_server(grpc_port)
    server.start()
    logger.info("Server started", extra={"service": SERVICE, "severity": "INFO", "port": grpc_port})
    print(f"gRPC server running on port {grpc_port}. Metrics on {metrics_port}.")
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
