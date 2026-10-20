"""
client.py - Python caller for POST /order, acting like another service.

Reliability settings (state these in the report):
    TIMEOUT_SECONDS        = 12    per attempt (cook time is 5-10 s, so a healthy call fits)
    MAX_ATTEMPTS           = 3     1 try + 2 retries
    BACKOFF                = 0.5 s, then 1 s (exponential, base 0.5 s, no wait after last try)
    Retried only on        : timeout, connection error, 502/503/504 - transient failures.
                             Never on 4xx. Retrying is safe because every attempt sends the
                             SAME idempotency_key, so the server can never cook twice.
    CIRCUIT BREAKER        : opens after 2 consecutive failed calls (retries exhausted),
                             blocks calls for 10 s, then HALF-OPEN lets 1 trial call through:
                             success -> CLOSED, failure -> OPEN again.

Modes:
    py client.py demo        reliability demo (success, retry, breaker open, recovery).
                             Starts its OWN copy of app.py on port 5050, stops it to cause a
                             real server failure, then restarts it. Your app.py on :5000 is untouched.
    py client.py ratelimit   rate-limit demo against http://localhost:5000 (7 calls at once)
    py client.py order --key my-key [--timeout 3]   one call through the full caller
"""
import argparse
import os
import socket
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import requests

TIMEOUT_SECONDS = 12
MAX_ATTEMPTS = 3
BACKOFF_BASE = 0.5
BREAKER_FAILURE_THRESHOLD = 2
BREAKER_RECOVERY_SECONDS = 10
RETRYABLE_STATUS = {502, 503, 504}


def ts() -> str:
    return time.strftime("%H:%M:%S")


# ==========================================
# CIRCUIT BREAKER
# ==========================================
class CircuitBreaker:
    CLOSED, OPEN, HALF_OPEN = "CLOSED", "OPEN", "HALF_OPEN"

    def __init__(self, failure_threshold: int, recovery_seconds: float):
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self.state = self.CLOSED
        self.failures = 0
        self.opened_at = 0.0

    def allow(self) -> bool:
        if self.state == self.OPEN:
            if time.monotonic() - self.opened_at >= self.recovery_seconds:
                self.state = self.HALF_OPEN
                print(f"[{ts()}] 🔄 Breaker HALF-OPEN: letting one trial call through")
                return True
            left = self.recovery_seconds - (time.monotonic() - self.opened_at)
            print(f"[{ts()}] 🛑 Breaker OPEN: call blocked without touching the network ({left:.1f}s left)")
            return False
        return True

    def record_success(self):
        if self.state != self.CLOSED:
            print(f"[{ts()}] ✅ Breaker CLOSED: service recovered")
        self.state, self.failures = self.CLOSED, 0

    def record_failure(self):
        self.failures += 1
        if self.state == self.HALF_OPEN or self.failures >= self.failure_threshold:
            self.state, self.opened_at = self.OPEN, time.monotonic()
            print(f"[{ts()}] 💥 Breaker OPEN after {self.failures} consecutive failed call(s); "
                  f"blocking for {self.recovery_seconds}s")
        else:
            print(f"[{ts()}] ⚠️  Failed calls in a row: {self.failures}/{self.failure_threshold}")


# ==========================================
# THE CALLER
# ==========================================
class OrderClient:
    def __init__(self, base_url: str, timeout: float = TIMEOUT_SECONDS):
        self.url = f"{base_url}/order"
        self.timeout = timeout
        self.breaker = CircuitBreaker(BREAKER_FAILURE_THRESHOLD, BREAKER_RECOVERY_SECONDS)

    def place_order(self, key: str):
        """Returns the JSON body on success, None if the call failed or was blocked."""
        if not self.breaker.allow():
            return None
        trace_id = uuid.uuid4().hex
        for attempt in range(1, MAX_ATTEMPTS + 1):
            print(f"[{ts()}] 📡 POST /order key={key} attempt {attempt}/{MAX_ATTEMPTS}")
            try:
                resp = requests.post(self.url, json={"idempotency_key": key}, timeout=self.timeout,
                                     headers={"X-Request-ID": trace_id})
                if resp.status_code == 200:
                    print(f"[{ts()}] ✅ 200 {resp.json()}")
                    self.breaker.record_success()
                    return resp.json()
                if resp.status_code == 429:
                    # Not a server fault: report it and let the caller decide (no breaker failure).
                    print(f"[{ts()}] ⚠️  429 rate limited, Retry-After={resp.headers.get('Retry-After')}s")
                    return None
                if resp.status_code not in RETRYABLE_STATUS:
                    # 4xx (or other non-transient) -> retrying cannot help.
                    print(f"[{ts()}] ❌ {resp.status_code} {resp.json()} (not retried)")
                    return None
                print(f"[{ts()}] ❌ {resp.status_code} server error (transient)")
            except requests.exceptions.Timeout:
                print(f"[{ts()}] ⏳ timed out after {self.timeout}s")
            except requests.exceptions.ConnectionError:
                print(f"[{ts()}] 🔌 connection refused (server down)")

            if attempt < MAX_ATTEMPTS:
                backoff = BACKOFF_BASE * 2 ** (attempt - 1)
                print(f"[{ts()}]    retrying in {backoff}s")
                time.sleep(backoff)

        print(f"[{ts()}] ❌ call failed after {MAX_ATTEMPTS} attempts")
        self.breaker.record_failure()
        return None


# ==========================================
# DEMO 1 - reliability (real server failure)
# ==========================================
class ManagedServer:
    """Starts/stops a private copy of app.py so the demo can cause a real outage."""

    def __init__(self, port: int):
        self.port, self.proc = port, None

    def start(self):
        env = {**os.environ, "PORT": str(self.port)}
        here = os.path.dirname(os.path.abspath(__file__))
        self.proc = subprocess.Popen([sys.executable, os.path.join(here, "app.py")], cwd=here,
                                     env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    print(f"[{ts()}] 🟢 test server UP on port {self.port}")
                    return
            except OSError:
                time.sleep(0.3)
        raise RuntimeError("test server did not start")

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=10)
            self.proc = None
            print(f"[{ts()}] 🔴 test server DOWN (process stopped)")


def demo_reliability():
    port, run = 5050, uuid.uuid4().hex[:6]
    print(f"Settings: timeout={TIMEOUT_SECONDS}s, attempts={MAX_ATTEMPTS}, backoff={BACKOFF_BASE}s x2, "
          f"breaker opens after {BREAKER_FAILURE_THRESHOLD} failed calls, recovery={BREAKER_RECOVERY_SECONDS}s\n")
    server = ManagedServer(port)
    client = OrderClient(f"http://127.0.0.1:{port}")
    server.start()
    try:
        print("\n=== 1. SUCCESS ===")
        client.place_order(f"{run}-a")

        print("\n=== 2. SERVER FAILURE -> RETRY WITH BACKOFF ===")
        server.stop()
        client.place_order(f"{run}-b")

        print("\n=== 3. REPEATED FAILURE -> BREAKER OPENS ===")
        client.place_order(f"{run}-c")

        print("\n=== 4. BREAKER OPEN -> CALLS BLOCKED ===")
        client.place_order(f"{run}-d")

        print("\n=== 5. SERVER BACK, WAIT FOR RECOVERY PERIOD -> HALF-OPEN -> CLOSED ===")
        server.start()
        wait = max(0.0, BREAKER_RECOVERY_SECONDS - (time.monotonic() - client.breaker.opened_at))
        print(f"[{ts()}] waiting {wait:.1f}s for the recovery period to end")
        time.sleep(wait)
        client.place_order(f"{run}-d")

        print("\n=== 6. NORMAL TRAFFIC AGAIN ===")
        client.place_order(f"{run}-e")
    finally:
        server.stop()


# ==========================================
# DEMO 2 - rate limit (against your running app.py)
# ==========================================
def demo_ratelimit(base_url: str):
    run = uuid.uuid4().hex[:6]
    print(f"Limit: 5 requests / 10 s on POST /order, shared by ALL callers. Sending 7 at once to {base_url}\n")

    def one(i):
        start = time.perf_counter()
        r = requests.post(f"{base_url}/order", json={"idempotency_key": f"{run}-rl-{i}"}, timeout=15)
        return i, r, time.perf_counter() - start

    with ThreadPoolExecutor(max_workers=7) as pool:
        results = list(pool.map(one, range(1, 8)))
    retry_after = 0
    for i, r, took in results:
        if r.status_code == 429:
            retry_after = max(retry_after, int(r.headers["Retry-After"]))
            print(f"call {i}: 429 in {took:.2f}s {r.json()} Retry-After={r.headers['Retry-After']}s")
        else:
            print(f"call {i}: {r.status_code} in {took:.2f}s {r.json()}")

    if retry_after:
        print(f"\n[{ts()}] waiting Retry-After={retry_after}s, then calling again")
        time.sleep(retry_after)
        r = requests.post(f"{base_url}/order", json={"idempotency_key": f"{run}-rl-after"}, timeout=15)
        print(f"[{ts()}] call after waiting: {r.status_code} {r.json()}")


def main():
    parser = argparse.ArgumentParser(description="HTTP caller for the Fast-Food API")
    parser.add_argument("mode", choices=["demo", "ratelimit", "order"], nargs="?", default="demo")
    parser.add_argument("--url", default="http://localhost:5000")
    parser.add_argument("--key", default=None)
    parser.add_argument("--timeout", type=float, default=TIMEOUT_SECONDS)
    args = parser.parse_args()

    if args.mode == "demo":
        demo_reliability()
    elif args.mode == "ratelimit":
        demo_ratelimit(args.url)
    else:
        OrderClient(args.url, timeout=args.timeout).place_order(args.key or f"order-{uuid.uuid4().hex[:8]}")


if __name__ == "__main__":
    main()
