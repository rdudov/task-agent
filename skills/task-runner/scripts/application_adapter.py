#!/usr/bin/env python3
"""Versioned application integration for the public task engine.

The engine owns task lifecycle semantics.  An installation may add resource
policy, native standard-session arguments, event transport, and completion
checks through this deliberately small contract.  The registration value is a
Python ``module:attribute`` reference; no installation value is persisted by
this module unless the application returns it as non-secret session state.
"""

from __future__ import annotations

import importlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable


APPLICATION_API_VERSION = 1
APPLICATION_SPEC_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_.]*$"
)


class ApplicationAdapterError(ValueError):
    """The registered application cannot satisfy the public v1 contract."""


@dataclass(frozen=True)
class LaunchRequestV1:
    """One launch the engine is deciding.

    ``committing`` is false when the caller only asked what this launch would
    decide (``start --dry-run``). The engine answers that question without
    recording anything about the launch in the task, and an application that
    keeps durable per-task state must do the same: the task being asked about is
    usually not the task anyone meant to run.
    """

    task_dir: Path
    runner: str
    workflow: str
    operation: str
    destination: str | None
    requested_memory_limit_bytes: int | None
    role: str = "author"
    committing: bool = True


@dataclass(frozen=True)
class LaunchPolicyV1:
    memory_limit_bytes: int | None = None


@dataclass(frozen=True)
class StandardSessionRequestV1:
    """The native session a standard launch would use.

    ``committing`` carries the same fact as on :class:`LaunchRequestV1`: a dry
    run still needs the session arguments in order to report the command it
    would have run, but nothing about that session may outlive the answer.
    """

    task_dir: Path
    runner: str
    operation: str
    destination: str | None
    previous_state: Mapping[str, Any] = field(default_factory=dict)
    committing: bool = True


@dataclass(frozen=True)
class StandardSessionV1:
    command_arguments: tuple[str, ...] = ()
    state: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StandardRunResultV1:
    task_dir: Path
    runner: str
    operation: str
    return_code: int
    log_path: Path
    session_state: Mapping[str, Any]
    destination: str | None


@dataclass(frozen=True)
class StandardRunDispositionV1:
    state: str
    current_step: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApplicationEventV1:
    task_dir: Path
    kind: str
    workflow: str
    payload: Mapping[str, Any]
    destination: str | None = None
    event_id: str | None = None


@dataclass(frozen=True)
class TransportRecoveryV1:
    task_dir: Path
    workflow: str
    event_log_path: Path
    destination: str | None = None
    active_attempt_id: str | None = None


@dataclass(frozen=True)
class DeliveryResultV1:
    delivered: bool
    detail: str


@dataclass(frozen=True)
class CompletionRequestV1:
    task_dir: Path
    workflow: str | None


@dataclass(frozen=True)
class CompletionPreparationRequestV1:
    """One pre-finalization action requested by an installation.

    The public engine calls this only after every completion condition except
    the evidence ids declared by the application is already satisfied.  The
    application must persist those facts before returning; the engine then
    evaluates the complete predicate normally.
    """

    task_dir: Path
    workflow: str
    event_id: str
    destination: str | None
    evidence_ids: tuple[str, ...] = ()


@runtime_checkable
class ApplicationAdapterV1(Protocol):
    api_version: int

    def launch_policy(self, request: LaunchRequestV1) -> LaunchPolicyV1: ...

    def standard_session(self, request: StandardSessionRequestV1) -> StandardSessionV1: ...

    def standard_run_finished(
        self, result: StandardRunResultV1
    ) -> StandardRunDispositionV1 | None: ...

    def deliver_event(self, event: ApplicationEventV1) -> DeliveryResultV1: ...

    def recover_transport(self, request: TransportRecoveryV1) -> None: ...

    def completion_problems(self, request: CompletionRequestV1) -> list[str]: ...


def completion_preparation_evidence_ids(adapter: ApplicationAdapterV1) -> tuple[str, ...]:
    """Return the optional evidence ids an application can establish at the boundary.

    This is an additive v1 capability: existing applications that do not
    declare it retain the original ordering and remain valid.
    """
    raw = getattr(adapter, "completion_preparation_evidence_ids", ())
    method = getattr(adapter, "prepare_completion", None)
    if raw in (None, ()) and method is None:
        return ()
    if not isinstance(raw, (tuple, list)) or not raw or not all(
        isinstance(item, str) and item.strip() for item in raw
    ):
        raise ApplicationAdapterError(
            "completion_preparation_evidence_ids must be a non-empty list of ids"
        )
    normalized = tuple(item.strip() for item in raw)
    if len(set(normalized)) != len(normalized):
        raise ApplicationAdapterError(
            "completion_preparation_evidence_ids must not contain duplicates"
        )
    if not callable(method):
        raise ApplicationAdapterError(
            "an application declaring completion preparation evidence must implement "
            "prepare_completion(request)"
        )
    return normalized


class DefaultApplicationV1:
    """The public template's inert, policy-neutral installation."""

    api_version = APPLICATION_API_VERSION

    def launch_policy(self, request: LaunchRequestV1) -> LaunchPolicyV1:
        return LaunchPolicyV1(request.requested_memory_limit_bytes)

    def standard_session(self, request: StandardSessionRequestV1) -> StandardSessionV1:
        if request.operation != "start":
            raise ApplicationAdapterError(
                "The standard workflow needs a registered application to define "
                f"native {request.operation} semantics."
            )
        return StandardSessionV1()

    def deliver_event(self, event: ApplicationEventV1) -> DeliveryResultV1:
        return DeliveryResultV1(False, "no application event transport configured")

    def standard_run_finished(
        self, result: StandardRunResultV1
    ) -> StandardRunDispositionV1 | None:
        return None

    def recover_transport(self, request: TransportRecoveryV1) -> None:
        return None

    def completion_problems(self, request: CompletionRequestV1) -> list[str]:
        return []


def _validate_adapter(candidate: object, spec: str) -> ApplicationAdapterV1:
    required = (
        "launch_policy",
        "standard_session",
        "standard_run_finished",
        "deliver_event",
        "recover_transport",
        "completion_problems",
    )
    if isinstance(candidate, type):
        adapter = candidate()
    elif all(callable(getattr(candidate, name, None)) for name in required):
        adapter = candidate
    elif callable(candidate):
        adapter = candidate()
    else:
        adapter = candidate
    if getattr(adapter, "api_version", None) != APPLICATION_API_VERSION:
        raise ApplicationAdapterError(
            f"Application {spec!r} does not declare api_version "
            f"{APPLICATION_API_VERSION}."
        )
    missing = [name for name in required if not callable(getattr(adapter, name, None))]
    if missing:
        raise ApplicationAdapterError(
            f"Application {spec!r} is missing v1 methods: {', '.join(missing)}"
        )
    return adapter  # type: ignore[return-value]


def load_application(spec: str | None) -> ApplicationAdapterV1:
    if not spec:
        return DefaultApplicationV1()
    if not APPLICATION_SPEC_PATTERN.fullmatch(spec):
        raise ApplicationAdapterError(
            "--application must be a Python module:attribute reference"
        )
    module_name, attribute_path = spec.split(":", 1)
    try:
        value: object = importlib.import_module(module_name)
        for component in attribute_path.split("."):
            value = getattr(value, component)
    except (ImportError, AttributeError) as exc:
        raise ApplicationAdapterError(
            f"Cannot load application {spec!r}: {exc}"
        ) from exc
    return _validate_adapter(value, spec)


def parse_memory_limit(value: str | int | None) -> int | None:
    if value is None or isinstance(value, int):
        result = value
    else:
        text = value.strip().lower()
        if text in {"none", "off", "unlimited", "0"}:
            return None
        match = re.fullmatch(r"([0-9]+)([kmgt]?)", text)
        if not match:
            raise ApplicationAdapterError(
                "--memory-limit must be bytes, an integer with K/M/G/T suffix, or none"
            )
        result = int(match.group(1)) * {
            "": 1,
            "k": 1024,
            "m": 1024**2,
            "g": 1024**3,
            "t": 1024**4,
        }[match.group(2)]
    if result is not None and result <= 0:
        raise ApplicationAdapterError("--memory-limit must be positive or none")
    return result


def json_session_state(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a detached JSON object and refuse secret-looking state keys."""
    state = json.loads(json.dumps(dict(value)))
    forbidden: list[str] = []

    def inspect(candidate: object, prefix: str = "") -> None:
        if isinstance(candidate, dict):
            for key, nested in candidate.items():
                rendered = f"{prefix}.{key}" if prefix else str(key)
                if any(
                    part in str(key).lower()
                    for part in ("token", "secret", "password", "destination")
                ):
                    forbidden.append(rendered)
                inspect(nested, rendered)
        elif isinstance(candidate, list):
            for index, nested in enumerate(candidate):
                inspect(nested, f"{prefix}[{index}]")

    inspect(state)
    if forbidden:
        raise ApplicationAdapterError(
            "Application session state contains secret-bearing keys: " + ", ".join(forbidden)
        )
    return state
