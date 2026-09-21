"""Scheduler state access: models, stores, versioned document schema."""
from __future__ import annotations

from .models import ProviderState, ResetCandidate
from .schema import (
    SCHEMA_VERSION,
    DocumentCorruptError,
    MissingStateDocumentError,
    SchemaError,
    deserialize_state,
    document_to_state,
    parse_document_bytes,
    serialize_state,
    state_to_document,
    validate_document,
)
from .store import (
    FileStateStore,
    ProviderStateStore,
    StaleStateError,
    StateStoreError,
)

__all__ = [
    "ProviderState",
    "ResetCandidate",
    "ProviderStateStore",
    "FileStateStore",
    "StateStoreError",
    "StaleStateError",
    "SCHEMA_VERSION",
    "SchemaError",
    "DocumentCorruptError",
    "MissingStateDocumentError",
    "state_to_document",
    "document_to_state",
    "validate_document",
    "serialize_state",
    "parse_document_bytes",
    "deserialize_state",
]
