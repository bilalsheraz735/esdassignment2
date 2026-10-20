"""gRPC tests: success, invalid input, deadline, repeat, shared source of truth, trace id."""
import time

import grpc
import pytest
from prometheus_client import REGISTRY

import grpc_server
import order_pb2
import order_pb2_grpc

PORT = 50999


@pytest.fixture(scope="module")
def stub():
    server = grpc_server.create_server(PORT)
    server.start()
    channel = grpc.insecure_channel(f"localhost:{PORT}")
    yield order_pb2_grpc.OrderServiceStub(channel)
    channel.close()
    server.stop(None)


def handled(code):
    return REGISTRY.get_sample_value("grpc_server_handled_total",
                                     {"grpc_method": "PlaceOrder", "grpc_code": code}) or 0


def test_success(stub):
    r = stub.PlaceOrder(order_pb2.OrderRequest(idempotency_key="g-ok"), timeout=5)
    assert r.status == "Order ready!" and r.order_id.startswith("ord_")


def test_empty_key_is_invalid_argument(stub):
    before = handled("INVALID_ARGUMENT")
    with pytest.raises(grpc.RpcError) as exc:
        stub.PlaceOrder(order_pb2.OrderRequest(idempotency_key=""), timeout=5)
    assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert handled("INVALID_ARGUMENT") - before == 1


def test_deadline_exceeded_is_seen_by_client_and_server(stub):
    before = handled("DEADLINE_EXCEEDED")
    with pytest.raises(grpc.RpcError) as exc:
        stub.PlaceOrder(order_pb2.OrderRequest(idempotency_key="g-slow"), timeout=0.01)
    assert exc.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
    time.sleep(0.3)  # server finishes cooking, then records the outcome
    assert handled("DEADLINE_EXCEEDED") - before == 1


def test_repeat_returns_same_order(stub):
    first = stub.PlaceOrder(order_pb2.OrderRequest(idempotency_key="g-rep"), timeout=5)
    second = stub.PlaceOrder(order_pb2.OrderRequest(idempotency_key="g-rep"), timeout=5)
    assert second.order_id == first.order_id
    assert second.status == "Order already processed (safe repeat)"


def test_same_key_over_http_and_grpc_is_one_order(stub, http):
    """Shared source of truth: the gRPC service sees the order the HTTP API created."""
    http_order = http.post("/order", json={"idempotency_key": "cross-1"}).get_json()
    grpc_order = stub.PlaceOrder(order_pb2.OrderRequest(idempotency_key="cross-1"), timeout=5)
    assert grpc_order.order_id == http_order["order_id"]
    assert grpc_order.status == "Order already processed (safe repeat)"


def test_trace_id_crosses_the_boundary(stub):
    _, call = stub.PlaceOrder.with_call(order_pb2.OrderRequest(idempotency_key="g-trace"),
                                        timeout=5, metadata=(("x-request-id", "trace-xyz"),))
    assert dict(call.trailing_metadata())["x-request-id"] == "trace-xyz"
