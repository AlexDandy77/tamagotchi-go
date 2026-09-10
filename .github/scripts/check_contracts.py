"""Validate the shared contracts, the README catalog and the field dictionary without running services."""

import json
import re
from itertools import count
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from openapi_spec_validator import validate

ROOT = Path(__file__).resolve().parents[2]
HTTP_METHODS = {"get", "put", "post", "delete", "patch", "options", "head", "trace"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def resolve(document, reference):
    require(reference.startswith("#/"), f"Expected local contract reference: {reference}")
    value = document
    for part in reference[2:].split("/"):
        value = value[part.replace("~1", "/").replace("~0", "~")]
    return value


def check_references(document, value):
    if isinstance(value, dict):
        if "$ref" in value:
            resolve(document, value["$ref"])
        for child in value.values():
            check_references(document, child)
    elif isinstance(value, list):
        for child in value:
            check_references(document, child)


def example(schema, document, sequence):
    """Build shape examples for the event and chat variants, with distinct UUIDs."""
    if "$ref" in schema:
        return example(resolve(document, schema["$ref"]), document, sequence)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    if "anyOf" in schema:
        return example(schema["anyOf"][0], document, sequence)
    kind = schema.get("type", "object")
    if kind == "object":
        return {
            key: example(value, document, sequence)
            for key, value in schema.get("properties", {}).items()
            if key in schema.get("required", [])
        }
    if kind == "array":
        return [
            example(schema["items"], document, sequence)
            for _ in range(schema.get("minItems", 1))
        ]
    if kind == "string":
        values = {
            "uuid": f"00000000-0000-4000-8000-{next(sequence):012d}",
            "date-time": "2026-09-09T10:00:00Z",
            "uri": "https://assets.example.invalid/pet.png",
            "email": "player@example.invalid",
        }
        return values.get(schema.get("format"), "example")
    if kind in ("integer", "number"):
        return schema.get("minimum", 0)
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    raise ValueError(f"Unsupported example schema: {schema}")


def rejects(validator, payload, label):
    try:
        validator.validate(payload)
    except ValidationError:
        return
    raise ValueError(f"Invalid payload was accepted: {label}")


def main():
    spec = yaml.safe_load((ROOT / "contracts/openapi.yaml").read_text())
    events = json.loads((ROOT / "contracts/events.schema.json").read_text())
    realtime = json.loads((ROOT / "contracts/realtime.schema.json").read_text())
    readme = (ROOT / "README.md").read_text()
    dictionary = (ROOT / "contracts/field-dictionary.md").read_text()
    validate(spec)
    for document in (spec, events, realtime):
        check_references(document, document)
    for name, schema in spec["components"]["schemas"].items():
        Draft202012Validator.check_schema(schema)
        require(f"#### {name}\n" in dictionary, f"Missing field dictionary type: {name}")
    for document in (events, realtime):
        Draft202012Validator.check_schema(document)

    operation_ids = set()
    catalog = set()
    for path, path_item in spec["paths"].items():
        for method, operation in path_item.items():
            if method not in HTTP_METHODS:
                continue
            identifier = operation["operationId"]
            require(identifier not in operation_ids, f"Duplicate operation ID: {identifier}")
            operation_ids.add(identifier)
            catalog.add((method.upper(), path))
            require(f"`{method.upper()} {path}`" in readme, f"Missing README route: {method} {path}")
            parameters = path_item.get("parameters", []) + operation.get("parameters", [])
            placeholders = set(re.findall(r"\{([^}]+)\}", path))
            declared = {p["name"] for p in parameters if p["in"] == "path" and p.get("required")}
            require(placeholders == declared, f"Path parameters disagree: {path}")
            if path.startswith("/internal/"):
                require(operation.get("security") == [{"serviceTLS": []}], f"Missing service auth: {path}")
                require(operation.get("x-allowed-callers"), f"Missing caller allowlist: {path}")
    documented = set(re.findall(r"^\| `(GET|PUT|POST|DELETE|PATCH|HEAD|OPTIONS|TRACE) ([^`]+)`", readme, re.M))
    require(documented == catalog, "README and OpenAPI endpoint catalogs disagree")

    checker = FormatChecker()
    event_validator = Draft202012Validator(events, format_checker=checker)
    frame_validator = Draft202012Validator(realtime, format_checker=checker)
    sequence = count(1)
    for variant in events["oneOf"]:
        event_validator.validate(example(variant, events, sequence))
        event_type = variant["properties"]["type"]["const"]
        require(f"`{event_type}`" in readme, f"Missing README event: {event_type}")
    for variant in realtime["oneOf"]:
        frame_validator.validate(example(variant, realtime, sequence))

    def schema_validator(name):
        return Draft202012Validator(
            {"$ref": f"#/components/schemas/{name}", "components": spec["components"]},
            format_checker=checker,
        )

    valid_location = {"latitude": 47.01, "longitude": 28.86, "accuracyMeters": 3, "recordedAt": "2026-09-09T10:00:00Z"}
    schema_validator("LocationInput").validate(valid_location)
    schema_validator("CareInput").validate({"actionId": "feed", "expectedVersion": 3})
    schema_validator("BonusRule").validate({"threshold": 80, "direction": "gte", "percent": 10})
    invalid = [
        ("CareInput", {"actionId": "feed", "expectedVersion": 3, "xp": 999}),
        ("CareInput", {"actionId": "feed", "expectedVersion": "3"}),
        ("LocationInput", {**valid_location, "latitude": 91}),
        ("LocationInput", {**valid_location, "recordedAt": "yesterday"}),
        ("BonusRule", {"threshold": 80, "direction": "gte", "percent": 100}),
        ("Coins", 9007199254740992),
        ("Device", {"id": "00000000-0000-4000-8000-000000000001", "packageId": "00000000-0000-4000-8000-000000000002", "platform": "web", "updatedAt": "2026-09-09T10:00:00Z", "token": "must-not-be-returned"}),
    ]
    for name, payload in invalid:
        rejects(schema_validator(name), payload, name)
    bad_event = example(events["oneOf"][0], events, sequence)
    bad_event["producer"] = "not-an-allowed-producer"
    rejects(event_validator, bad_event, "event producer")
    rejects(frame_validator, {"type": "admin.grant", "xp": 1000}, "unknown chat frame")

    for block in re.findall(r"```json\n(.*?)\n```", readme, re.S):
        payload = json.loads(block)
        if "eventId" in payload:
            event_validator.validate(payload)
        elif "error" in payload:
            schema_validator("Error").validate(payload)
        elif "actionId" in payload and "expectedVersion" in payload:
            schema_validator("CareInput").validate(payload)
    for target in re.findall(r"\]\((contracts/[^)]+)\)", readme):
        require((ROOT / target).is_file(), f"Broken contract link: {target}")
    for label, text in (("README", readme), ("field dictionary", dictionary)):
        require(text.count("```") % 2 == 0, f"Unclosed {label} code fence")
    print(f"Validated {len(catalog)} HTTP operations, {len(events['oneOf'])} event types and {len(realtime['oneOf'])} chat types.")
    print("README and field dictionary agreement, references, examples and nine invalid-payload checks passed.")


if __name__ == "__main__":
    main()
