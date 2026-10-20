"""
grpc_client.py - calls the gRPC OrderService directly (no HTTP, no browser).

Single call:
    py grpc_client.py --key my-order-1 --deadline 15
    py grpc_client.py --key my-order-1 --deadline 2      (deadline too short -> DEADLINE_EXCEEDED)
    py grpc_client.py --key ""                           (invalid input -> INVALID_ARGUMENT)

Repeatable demo (fresh keys every run: success, safe repeat, deadline failure, invalid input):
    py grpc_client.py --demo

Trace correlation: the client creates a trace id, sends it as gRPC metadata `x-request-id`,
logs it to app-logs.json (service=grpc-client), and the server logs the same trace_id.
In Kibana:  trace_id : "<id printed below>"
"""
import argparse
import sys
import time
import uuid

import grpc

import order_core
import order_pb2
import order_pb2_grpc

SERVICE = "grpc-client"
TRACE_HEADER = "x-request-id"
logger = order_core.get_logger("grpc-client")


def place_order(stub, key: str, deadline: float) -> bool:
    trace_id = order_core.new_trace_id()
    extra = {"trace_id": trace_id, "service": SERVICE, "severity": "INFO", "deadline_s": deadline}
    print(f"-> PlaceOrder key={key!r} deadline={deadline}s trace_id={trace_id}")
    logger.info("gRPC call started", extra=extra)
    start = time.perf_counter()
    try:
        response, call = stub.PlaceOrder.with_call(
            order_pb2.OrderRequest(idempotency_key=key),
            timeout=deadline,
            metadata=((TRACE_HEADER, trace_id),),
        )
        elapsed = time.perf_counter() - start
        echoed = dict(call.trailing_metadata() or ()).get(TRACE_HEADER)
        print(f"   OK in {elapsed:.2f}s | status={response.status!r} order_id={response.order_id} "
              f"| server echoed trace_id={echoed}")
        logger.info("gRPC call finished", extra={**extra, "grpc_code": "OK",
                                                 "request_id": response.order_id,
                                                 "elapsed_s": round(elapsed, 3)})
        return True
    except grpc.RpcError as err:
        elapsed = time.perf_counter() - start
        print(f"   FAILED in {elapsed:.2f}s | gRPC status={err.code().name} | details={err.details()!r}")
        logger.warning("gRPC call failed", extra={**extra, "severity": "WARNING",
                                                  "grpc_code": err.code().name,
                                                  "elapsed_s": round(elapsed, 3)})
        return False


def run_demo(stub):
    run = uuid.uuid4().hex[:6]
    print(f"=== gRPC demo, run id {run} (fresh keys, so results repeat on every run) ===\n")
    print("TEST 1 - success (deadline 15s > max cook 10s)")
    place_order(stub, f"grpc-{run}-ok", 15.0)
    print("\nTEST 2 - safe repeat (same key again)")
    place_order(stub, f"grpc-{run}-ok", 15.0)
    print("\nTEST 3 - failure: deadline 2s < min cook 5s")
    place_order(stub, f"grpc-{run}-slow", 2.0)
    print("\nTEST 4 - failure: invalid input (empty key)")
    place_order(stub, "", 15.0)


def main():
    parser = argparse.ArgumentParser(description="gRPC client for fastfood.OrderService")
    parser.add_argument("--target", default="localhost:50051", help="host:port of the gRPC server")
    parser.add_argument("--key", default=None, help="idempotency key (default: a new unique key)")
    parser.add_argument("--deadline", type=float, default=15.0, help="deadline in seconds")
    parser.add_argument("--demo", action="store_true", help="run the repeatable success/failure demo")
    args = parser.parse_args()

    with grpc.insecure_channel(args.target) as channel:
        stub = order_pb2_grpc.OrderServiceStub(channel)
        if args.demo:
            run_demo(stub)
            return
        key = args.key if args.key is not None else f"grpc-{uuid.uuid4().hex[:8]}"
        sys.exit(0 if place_order(stub, key, args.deadline) else 1)


if __name__ == "__main__":
    main()
