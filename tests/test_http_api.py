"""HTTP API tests: valid, invalid, repeated, rate limit, error format, metrics labels."""
import app
from prometheus_client import REGISTRY


def post(http, body=None, **kw):
    return http.post("/order", json=body, **kw) if body is not None else http.post("/order", **kw)


def test_valid_request_places_order(http):
    r = post(http, {"idempotency_key": "valid-1"})
    assert r.status_code == 200
    assert r.get_json()["status"] == "Order ready!"
    assert r.get_json()["order_id"].startswith("ord_")


def test_repeated_request_returns_same_order_and_cooks_once(http):
    before = REGISTRY.get_sample_value("orders_placed_total") or 0
    first = post(http, {"idempotency_key": "repeat-1"}).get_json()
    second = post(http, {"idempotency_key": "repeat-1"})
    assert second.status_code == 200
    assert second.get_json()["order_id"] == first["order_id"]
    assert second.get_json()["status"] == "Order already processed (safe repeat)"
    assert (REGISTRY.get_sample_value("orders_placed_total") or 0) - before == 1


def test_missing_key_is_400(http):
    r = post(http, {"something_else": 1})
    assert r.status_code == 400 and r.get_json() == {"error": "idempotency_key is required"}


def test_wrong_key_type_is_400_not_500(http):
    for bad in ({"x": 1}, ["a"], 123):
        r = post(http, {"idempotency_key": bad})
        assert r.status_code == 400
        assert "error" in r.get_json()
        app.RATE_LIMITER.reset()


def test_key_with_bad_characters_is_400(http):
    assert post(http, {"idempotency_key": "has spaces!"}).status_code == 400


def test_missing_or_non_json_body_is_400(http):
    assert post(http).status_code == 400
    assert http.post("/order", data="not json", content_type="text/plain").status_code == 400
    assert post(http, ["a", "list"]).status_code == 400


def test_rate_limit_returns_429_with_retry_after(http):
    codes = [post(http, {"wrong": 1}).status_code for _ in range(5)]
    assert codes == [400] * 5                       # every call counts toward the limit
    r = post(http, {"idempotency_key": "limited"})
    assert r.status_code == 429
    assert r.get_json() == {"error": "Too Many Requests"}
    assert 1 <= int(r.headers["Retry-After"]) <= 10


def test_errors_are_json_everywhere(http):
    for r, code in ((http.get("/order"), 405), (http.post("/does-not-exist"), 404)):
        assert r.status_code == code
        assert r.is_json and "error" in r.get_json()


def test_trace_id_is_echoed(http):
    r = post(http, {"idempotency_key": "trace-1"}, headers={"X-Request-ID": "abc123"})
    assert r.headers["X-Request-ID"] == "abc123"
    assert post(http, {"idempotency_key": "trace-2"}).headers["X-Request-ID"]  # generated if absent


def test_metrics_have_status_labels_and_no_id_labels(http):
    post(http, {"idempotency_key": "m-1"})
    post(http, {"bad": 1})
    text = http.get("/metrics").get_data(as_text=True)
    assert 'http_requests_total{method="POST",route="/order",status="200"}' in text
    assert 'http_requests_total{method="POST",route="/order",status="400"}' in text
    assert "request_id" not in text and "demo_requests_total" not in text
