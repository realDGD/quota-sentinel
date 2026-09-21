"""Versioned durable-document schema for provider scheduler state (Phase 2).

One ``ProviderState`` maps to exactly one JSON document carrying
``"schema_version": 1``. This module is the pure codec + validator; all
filesystem behaviour lives in the stores.

Materialization rule: EVERY slot key is always present in a v1 document.
Business "unset" is an explicit ``null``; a MISSING key is corruption,
never a defaultable gap. Unknown keys are rejected for the same reason —
schema drift must fail loudly rather than round-trip silently.

Type policy mirrors the FileStateStore persistence boundary exactly:
bool never satisfies an epoch int, epochs are >= 0, ``retry_pending`` is
a plain bool, ``last_triggered_window`` is null or a non-empty
single-line string, and the transient ``reset_candidate`` is null or an
exactly-``{reset_at, observed_at}`` object of epoch ints.

Serialization is deterministic (sorted keys, fixed indent, trailing
newline, ``ensure_ascii=False``) and completes to FINAL UTF-8 BYTES
before any caller may touch the filesystem. Text that cannot be encoded
(lone surrogates) fails HERE — validator first, encode step as backstop —
with a StateStoreError, never mid-publish.
"""
from __future__ import annotations

import json
from typing import Any, Dict

from .models import ProviderState, ResetCandidate
from .store import StateStoreError

SCHEMA_VERSION = 1
VERSION_KEY = "schema_version"

EPOCH_KEYS = (
    "last_attempt_at",
    "last_task_at",
    "next_due_at",
    "last_known_reset",
    "reset_anchor",
)
PENDING_KEY = "retry_pending"
WINDOW_KEY = "last_triggered_window"
CANDIDATE_KEY = "reset_candidate"
CANDIDATE_SUBKEYS = frozenset({"reset_at", "observed_at"})

EXPECTED_KEYS = frozenset(
    {VERSION_KEY, PENDING_KEY, WINDOW_KEY, CANDIDATE_KEY, *EPOCH_KEYS}
)


class SchemaError(StateStoreError):
    """A parsed document violates the v1 schema (types, keys, version)."""


class DocumentCorruptError(StateStoreError):
    """The document bytes are not decodable UTF-8 JSON at all."""


class MissingStateDocumentError(StateStoreError):
    """No document exists for this provider yet: run the migration
    bootstrap first. Absence is NOT business state and never reads as
    an all-unset default."""


def _is_plain_int(value: object) -> bool:
    return type(value) is int  # bool is excluded: type(True) is bool


def _has_surrogate(text: str) -> bool:
    return any("\ud800" <= ch <= "\udfff" for ch in text)


# ---------------------------------------------------------------------------
# Shared field rules. BOTH the domain-object boundary (validate_state) and the
# persisted-document boundary (validate_document) call these, so the two can
# never drift into two independent rule sets (§8). Only the label differs:
# callers pass "field X" (domain) or "document key X" (persisted).
# ---------------------------------------------------------------------------
def _check_epoch(provider: str, label: str, value: object) -> None:
    if value is None:
        return
    if not _is_plain_int(value) or value < 0:
        raise SchemaError(
            f"{provider}: {label} must be null or a plain non-negative int, "
            f"got {value!r}"
        )


def _check_bool(provider: str, label: str, value: object) -> None:
    if type(value) is not bool:
        raise SchemaError(
            f"{provider}: {label} must be a plain bool, got {value!r}"
        )


def _check_window(provider: str, label: str, value: object) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value:
        raise SchemaError(
            f"{provider}: {label} must be null or a non-empty string, "
            f"got {value!r}"
        )
    if "\n" in value or "\r" in value:
        raise SchemaError(
            f"{provider}: {label} must be single-line (raw slot round-trip), "
            f"got {value!r}"
        )
    if _has_surrogate(value):
        raise SchemaError(
            f"{provider}: {label} contains surrogates and is not UTF-8 "
            "representable"
        )


def _check_candidate_times(
    provider: str, reset_at: object, observed_at: object
) -> None:
    # Candidate halves are never null (a candidate either fully exists or is
    # absent), unlike the standalone epoch slots.
    for name, value in (("reset_at", reset_at), ("observed_at", observed_at)):
        if not _is_plain_int(value) or value < 0:
            raise SchemaError(
                f"{provider}: {CANDIDATE_KEY}.{name} must be a plain "
                f"non-negative int, got {value!r}"
            )


def validate_state(state: object, provider: str) -> ProviderState:
    """Domain-object boundary: a runtime ProviderState, before it is ever
    projected onto a document, must already be well-typed.

    Future Python scheduler transitions will CONSTRUCT ProviderState
    directly (not read it back through the file loader), so a lookalike
    reset_candidate (dict / tuple / SimpleNamespace / str) or a mistyped
    field must fail HERE with a typed SchemaError — never as a raw
    AttributeError deep inside state_to_document, and never by duck-typing
    its way into a persisted document. This is the Phase 3A P2-1 fix; the
    file/JSON stores call it before touching the filesystem, so a bad
    domain object means zero filesystem mutation.
    """
    if not isinstance(state, ProviderState):
        raise SchemaError(
            f"{provider}: state must be a ProviderState, "
            f"got {type(state).__name__}"
        )
    for attr in (
        "last_attempt_at",
        "last_task_at",
        "next_due_at",
        "last_known_reset",
        "reset_anchor",
    ):
        _check_epoch(provider, f"field {attr}", getattr(state, attr))
    _check_bool(provider, f"field {PENDING_KEY}", state.retry_pending)
    _check_window(provider, f"field {WINDOW_KEY}", state.last_triggered_window)
    candidate = state.reset_candidate
    if candidate is not None:
        if not isinstance(candidate, ResetCandidate):
            raise SchemaError(
                f"{provider}: field {CANDIDATE_KEY} must be null or a "
                f"ResetCandidate, got {type(candidate).__name__}"
            )
        _check_candidate_times(
            provider, candidate.reset_at, candidate.observed_at
        )
    return state


def state_to_document(state: ProviderState) -> Dict[str, Any]:
    """Project an ALREADY-validated domain model onto a complete v1
    document. Callers must run validate_state() first; validate_document()
    then re-checks the projected shape independently."""
    candidate = state.reset_candidate
    return {
        VERSION_KEY: SCHEMA_VERSION,
        "last_attempt_at": state.last_attempt_at,
        "last_task_at": state.last_task_at,
        "next_due_at": state.next_due_at,
        PENDING_KEY: state.retry_pending,
        "last_known_reset": state.last_known_reset,
        WINDOW_KEY: state.last_triggered_window,
        "reset_anchor": state.reset_anchor,
        CANDIDATE_KEY: None
        if candidate is None
        else {"reset_at": candidate.reset_at, "observed_at": candidate.observed_at},
    }


def validate_document(doc: object, provider: str) -> Dict[str, Any]:
    """Strict v1 validation. Returns the doc typed as a dict; raises
    SchemaError with provider + key context for any violation."""
    if not isinstance(doc, dict):
        raise SchemaError(
            f"{provider}: state document must be a JSON object, "
            f"got {type(doc).__name__}"
        )
    keys = set(doc)
    missing = sorted(EXPECTED_KEYS - keys)
    if missing:
        raise SchemaError(
            f"{provider}: state document is missing required keys "
            f"{missing} (every slot must be materialized; absent keys "
            "are corruption, never business state)"
        )
    unknown = sorted(keys - EXPECTED_KEYS)
    if unknown:
        raise SchemaError(
            f"{provider}: state document has unknown keys {unknown}; "
            "schema drift must fail loudly"
        )

    version = doc[VERSION_KEY]
    if not _is_plain_int(version) or version != SCHEMA_VERSION:
        raise SchemaError(
            f"{provider}: unsupported {VERSION_KEY} {version!r}; this "
            f"build understands exactly {SCHEMA_VERSION} — refusing to guess"
        )

    for key in EPOCH_KEYS:
        _check_epoch(provider, f"document key {key}", doc[key])

    _check_bool(provider, f"document key {PENDING_KEY}", doc[PENDING_KEY])
    _check_window(provider, f"document key {WINDOW_KEY}", doc[WINDOW_KEY])

    candidate = doc[CANDIDATE_KEY]
    if candidate is None:
        return doc
    if not isinstance(candidate, dict):
        raise SchemaError(
            f"{provider}: document key {CANDIDATE_KEY} must be null or an "
            f"object, got {type(candidate).__name__}"
        )
    candidate_keys = set(candidate)
    if candidate_keys != set(CANDIDATE_SUBKEYS):
        raise SchemaError(
            f"{provider}: {CANDIDATE_KEY} must carry exactly "
            f"{sorted(CANDIDATE_SUBKEYS)}, got {sorted(candidate_keys)}"
        )
    _check_candidate_times(
        provider, candidate["reset_at"], candidate["observed_at"]
    )
    return doc


def document_to_state(doc: Dict[str, Any], provider: str) -> ProviderState:
    """Validated decode: v1 document dict → domain model."""
    validate_document(doc, provider)
    candidate = doc[CANDIDATE_KEY]
    return ProviderState(
        last_attempt_at=doc["last_attempt_at"],
        last_task_at=doc["last_task_at"],
        next_due_at=doc["next_due_at"],
        retry_pending=doc[PENDING_KEY],
        last_known_reset=doc["last_known_reset"],
        last_triggered_window=doc[WINDOW_KEY],
        reset_anchor=doc["reset_anchor"],
        reset_candidate=None
        if candidate is None
        else ResetCandidate(candidate["reset_at"], candidate["observed_at"]),
    )


def serialize_state(state: ProviderState, provider: str) -> bytes:
    """ProviderState → FINAL document bytes, fully preflighted.

    Pipeline (single rule source, no drift):
      validate_state → state_to_document → validate_document →
      dumps → strict UTF-8 encode — all before returning. The caller can
      publish the result without any further value-level failure being
      possible.
    """
    validate_state(state, provider)
    document = state_to_document(state)
    validate_document(document, provider)
    text = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2)
    try:
        return (text + "\n").encode("utf-8", errors="strict")
    except UnicodeError as exc:  # backstop: validator rejects surrogates first
        raise StateStoreError(
            f"{provider}: state document is not UTF-8 representable: {exc}"
        ) from exc


def parse_document_bytes(raw: bytes, provider: str) -> Dict[str, Any]:
    """FINAL document bytes → validated dict. Decoding damage is
    DocumentCorruptError; schema damage is SchemaError. Neither is ever
    silently defaulted."""
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise DocumentCorruptError(
            f"{provider}: state document is not valid UTF-8: {exc}"
        ) from exc
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise DocumentCorruptError(
            f"{provider}: state document is not valid JSON: {exc}"
        ) from exc
    return validate_document(doc, provider)


def deserialize_state(raw: bytes, provider: str) -> ProviderState:
    """FINAL document bytes → validated domain model."""
    return document_to_state(parse_document_bytes(raw, provider), provider)
