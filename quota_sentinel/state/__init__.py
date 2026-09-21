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
from .json_store import JsonStateStore, document_filename
from .migration import (
    ACTION_EXISTS,
    ACTION_SEEDED,
    DEFAULT_PROVIDERS,
    migrate_all,
    migrate_provider,
    seed_document_if_absent,
)

__all__ = [
    "ProviderState",
    "ResetCandidate",
    "ProviderStateStore",
    "FileStateStore",
    "JsonStateStore",
    "document_filename",
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
    "DEFAULT_PROVIDERS",
    "migrate_provider",
    "migrate_all",
    "seed_document_if_absent",
    "ACTION_SEEDED",
    "ACTION_EXISTS",
]
