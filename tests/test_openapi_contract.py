"""Uses openapi.yaml to TEST the endpoint: real responses must match the documented schemas."""
import os

import jsonschema
import yaml

SPEC = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "..", "openapi.yaml"), encoding="utf-8"))
OP = SPEC["paths"]["/order"]["post"]


def resolve(schema):
    if "$ref" in schema:
        name = schema["$ref"].split("/")[-1]
        return SPEC["components"]["schemas"][name]
    return schema


def check(response):
    documented = OP["responses"]
    code = str(response.status_code)
    assert code in documented, f"status {code} is not documented in openapi.yaml"
    schema = resolve(documented[code]["content"]["application/json"]["schema"])
    jsonschema.validate(response.get_json(), schema)
    for header in documented[code].get("headers", {}):
        assert header in response.headers, f"documented header {header} missing"


def test_request_example_matches_request_schema():
    body = OP["requestBody"]["content"]["application/json"]
    jsonschema.validate(body["example"], resolve(body["schema"]))


def test_200_new_and_repeat_match_contract(http):
    check(http.post("/order", json={"idempotency_key": "contract-1"}))
    check(http.post("/order", json={"idempotency_key": "contract-1"}))


def test_400_matches_contract(http):
    check(http.post("/order", json={"idempotency_key": ""}))


def test_429_matches_contract(http):
    for _ in range(5):
        http.post("/order", json={})
    check(http.post("/order", json={"idempotency_key": "contract-2"}))
