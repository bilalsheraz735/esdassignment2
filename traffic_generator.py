"""
traffic_generator.py - repeatable success + failure traffic for the Grafana dashboard.

Every round uses fresh keys, so results are the same on every run (no stale cache hits).

Per round:
  HTTP (5 requests = exactly the rate limit, so none are rejected by accident)
    3 new orders at once  -> 200 (cooked, in-flight gauge > 1)
    1 repeat              -> 200 (idempotent replay)
    1 invalid body        -> 400
  gRPC
    3 new orders at once  -> OK
    1 repeat              -> OK (idempotent replay)
    1 empty key           -> INVALID_ARGUMENT
    1 deadline 2 s        -> DEADLINE_EXCEEDED
Finally (unless --no-burst): 7 HTTP calls at once -> 5 x 200 + 2 x 429

Usage:  py traffic_generator.py [--rounds 3] [--no-burst]
"""
import argparse
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import grpc
import requests

import order_pb2
import order_pb2_grpc

HTTP_URL = "http://localhost:5000/order"
GRPC_TARGET = "localhost:50051"
WINDOW = 10  # HTTP rate-limit window (seconds)


def http_call(body):
    try:
        r = requests.post(HTTP_URL, json=body, timeout=15)
        return str(r.status_code)
    except requests.RequestException as exc:
        return type(exc).__name__


def grpc_call(stub, key, deadline=15.0):
    try:
        stub.PlaceOrder(order_pb2.OrderRequest(idempotency_key=key), timeout=deadline)
        return "OK"
    except grpc.RpcError as err:
        return err.code().name


def run_round(n, stub, pool):
    tag = uuid.uuid4().hex[:6]
    start = time.monotonic()

    # HTTP: 3 new orders in parallel + 1 invalid, then a repeat of the first
    http_jobs = [pool.submit(http_call, {"idempotency_key": f"tg-{tag}-h{i}"}) for i in range(3)]
    http_jobs.append(pool.submit(http_call, {"wrong_field": 1}))
    http = [j.result() for j in http_jobs]
    http.append(http_call({"idempotency_key": f"tg-{tag}-h0"}))

    # gRPC: 3 new orders + 1 invalid + 1 short deadline in parallel, then a repeat
    grpc_jobs = [pool.submit(grpc_call, stub, f"tg-{tag}-g{i}") for i in range(3)]
    grpc_jobs.append(pool.submit(grpc_call, stub, ""))
    grpc_jobs.append(pool.submit(grpc_call, stub, f"tg-{tag}-slow", 2.0))
    grpcs = [j.result() for j in grpc_jobs]
    grpcs.append(grpc_call(stub, f"tg-{tag}-g0"))

    print(f"Round {n}: HTTP {http} | gRPC {grpcs}")
    # Make sure the next round starts in a fresh rate-limit window
    time.sleep(max(0.0, WINDOW + 0.5 - (time.monotonic() - start)))


def burst(pool):
    tag = uuid.uuid4().hex[:6]
    jobs = [pool.submit(http_call, {"idempotency_key": f"tg-{tag}-burst{i}"}) for i in range(7)]
    print(f"Burst: HTTP {sorted(j.result() for j in jobs)}  (expect 5 x 200, 2 x 429)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--no-burst", action="store_true")
    args = parser.parse_args()

    print(f"=== Traffic generator: {args.rounds} round(s) ===")
    with grpc.insecure_channel(GRPC_TARGET) as channel, ThreadPoolExecutor(max_workers=8) as pool:
        stub = order_pb2_grpc.OrderServiceStub(channel)
        for n in range(1, args.rounds + 1):
            run_round(n, stub, pool)
        if not args.no_burst:
            burst(pool)
    print("Done. Look at the last 15 minutes in Grafana.")


if __name__ == "__main__":
    main()
