"""The OpenAPI document is the public contract, so it is pinned rather than described.

Every ``operation_id`` here becomes an MCP tool name. Renaming one is not a refactor, it
is breaking somebody's assistant, so the whole set is written out literally: adding,
removing or renaming an operation fails this file and has to be done on purpose.

The rest of it checks the three things a model reads the document for, and the one thing a
reviewer would otherwise have to check by eye on every change -- that nothing in any
response schema could carry a credential.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from fastapi import FastAPI

UNAUTHENTICATED = frozenset({"/healthy", "/ready"})
"""The probes. A load balancer holds no token, so these two are open and document no 401."""

OPERATIONS = frozenset(
    {
        "get_health",
        "check_readiness",
        "get_user",
        "delete_user",
        "describe_schema",
        "search_user",
        "get_entry",
        "revise_entry",
        "forget_entry",
        "confirm_entry",
        "set_field",
        "get_field",
        "write_note",
        "export_user",
        "read_events",
        "get_settings",
        "update_settings",
    }
)
"""Every tool this service offers. Written out, not derived."""

SECRET_BEARING = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "authorization",
    "credential",
    "passphrase",
)

MINIMUM_DESCRIPTION = 60
"""Short enough to allow a terse endpoint, long enough to fail a placeholder."""


@pytest.fixture
def spec(app: FastAPI) -> dict[str, Any]:
    document: dict[str, Any] = app.openapi()
    return document


def operations(spec: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (method.upper(), path, operation)
        for path, methods in spec["paths"].items()
        for method, operation in methods.items()
    ]


class TestTheOperationSet:
    def test_it_is_exactly_what_was_promised(self, spec: dict[str, Any]) -> None:
        assert {operation["operationId"] for _, _, operation in operations(spec)} == OPERATIONS

    def test_every_operation_id_is_unique(self, spec: dict[str, Any]) -> None:
        # Two routes sharing one would give a bridge two tools with one name, and which
        # one a model got would depend on iteration order.
        ids = [operation["operationId"] for _, _, operation in operations(spec)]

        assert len(ids) == len(set(ids))

    def test_every_operation_id_is_snake_case(self, spec: dict[str, Any]) -> None:
        # They become tool names, and a bridge that has to mangle them produces a name
        # nobody can predict from the document.
        for _, _, operation in operations(spec):
            assert re.fullmatch(r"[a-z][a-z0-9_]*", operation["operationId"])

    def test_every_operation_reads_as_a_verb_on_a_noun(self, spec: dict[str, Any]) -> None:
        verbs = {
            "get",
            "set",
            "write",
            "read",
            "search",
            "describe",
            "export",
            "delete",
            "revise",
            "forget",
            "confirm",
            "update",
            # The probes: `check_readiness` is what every service in the family calls it,
            # and a probe genuinely checks rather than gets.
            "check",
        }

        for _, _, operation in operations(spec):
            assert operation["operationId"].split("_")[0] in verbs


class TestWhatAModelReads:
    def test_every_operation_has_a_summary(self, spec: dict[str, Any]) -> None:
        for _, _, operation in operations(spec):
            assert operation.get("summary")

    def test_every_operation_has_a_real_description(self, spec: dict[str, Any]) -> None:
        # This is the tool documentation. A model decides whether and how to call it from
        # here, so a one-line restatement of the summary is a failure rather than a style
        # preference.
        for _, _, operation in operations(spec):
            assert len(operation.get("description", "")) >= MINIMUM_DESCRIPTION, operation[
                "operationId"
            ]

    def test_every_request_field_is_described(self, spec: dict[str, Any]) -> None:
        for name, schema in _request_models(spec).items():
            for field, definition in schema.get("properties", {}).items():
                assert _described(definition, spec), f"{name}.{field}"

    def test_the_service_description_says_what_it_is_for(self, spec: dict[str, Any]) -> None:
        description = spec["info"]["description"]

        assert "person" in description.lower()
        assert "keyring" in description.lower()


class TestRequestBodies:
    def test_every_request_model_rejects_fields_it_does_not_know(
        self, spec: dict[str, Any]
    ) -> None:
        # A silently dropped field is a model that believes it wrote something it did not,
        # and will go on believing it.
        models = _request_models(spec)

        assert models
        for name, schema in models.items():
            assert schema.get("additionalProperties") is False, name

    def test_no_request_model_accepts_a_provenance_field_the_server_derives(
        self, spec: dict[str, Any]
    ) -> None:
        # asserted_by comes from the verified token. A body that could supply one would
        # make the trustworthy half of provenance a claim like the other half.
        for name, schema in _request_models(spec).items():
            assert "asserted_by" not in schema.get("properties", {}), name


class TestNoResponseCanCarryACredential:
    def test_no_field_in_any_response_schema_is_secret_bearing(self, spec: dict[str, Any]) -> None:
        # The same contract test keyring has. It is cheap and it is the only check that
        # keeps holding while somebody adds a field in a hurry.
        offenders = [
            f"{name}.{field}"
            for name, schema in spec["components"]["schemas"].items()
            for field in schema.get("properties", {})
            if any(marker in field.lower() for marker in SECRET_BEARING)
        ]

        assert offenders == []


class TestErrors:
    def test_every_authenticated_operation_documents_a_401(self, spec: dict[str, Any]) -> None:
        for _, path, operation in operations(spec):
            if path in UNAUTHENTICATED:
                continue
            assert "401" in operation["responses"], operation["operationId"]

    def test_every_authenticated_operation_documents_keyring_being_down(
        self, spec: dict[str, Any]
    ) -> None:
        # A caller that has not been told 503 is possible will treat it as a bug rather
        # than as "come back in a moment".
        for _, path, operation in operations(spec):
            if path in UNAUTHENTICATED:
                continue
            assert "503" in operation["responses"], operation["operationId"]

    def test_the_problem_shape_is_what_failures_reference(self, spec: dict[str, Any]) -> None:
        assert "Problem" in spec["components"]["schemas"]
        referenced = [
            operation["responses"]["401"]
            for _, path, operation in operations(spec)
            if path not in UNAUTHENTICATED
        ]
        assert referenced
        assert all("Problem" in str(response) for response in referenced)


def _request_models(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        name: schema
        for name, schema in spec["components"]["schemas"].items()
        if name.endswith("Request")
    }


def _described(definition: dict[str, Any], spec: dict[str, Any]) -> bool:
    """Whether a field carries a description, following one level of $ref or anyOf."""
    if definition.get("description"):
        return True
    for variant in definition.get("anyOf", []):
        if variant.get("description"):
            return True
    return bool(definition.get("allOf"))
