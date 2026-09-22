"""Durable backend-authority protocol (Phase 3B).

THE PROBLEM
-----------
Phases 1-2 left two durable representations of the same scheduler state:
the shell's per-slot files (legacy, authoritative) and the v1 JSON
documents (shadow). Moving ownership to JSON is easy; the hard part is
the question every process must answer after a crash or a restart:

    which backend is authoritative RIGHT NOW?

Inventing that answer per call site is how a system ends up with a
reader that believes JSON and a writer that believes legacy. This module
therefore defines exactly ONE durable fact and forbids every other
signal.

THE FACT
--------
``<state_dir>/backend-authority.json`` — a single versioned document:

    {"backend": "json", "epoch": 1, "schema_version": 1}

Properties, each one deliberate:

* ATOMIC. Published through the same temp-write/fsync/rename primitive
  as every provider document, so a reader observes the complete old
  document or the complete new one, never a torn mix.

* VERSIONED. ``schema_version`` is validated strictly; an unknown version
  is a loud error, never a guess. ``epoch`` increments by one on every
  durable ownership switch and exists so a reader can tell "the same
  authority" from "a different authority that happens to look similar"
  without comparing state payloads.

* UNIQUE. Nothing else decides ownership. Not an in-memory flag, not a
  process-local cache, not a mtime, not "does the JSON exist", not "we
  probably cut over already". See ``read_authority``.

* ALWAYS PRESENT once the deployment has an owner. The manifest is
  materialized by exactly ONE thing: an operator asserting that this
  deployment predates the authority protocol
  (``bootstrap-authority --assume-legacy``, whose library primitive is
  ``bootstrap_legacy_authority``). From then on absence is a LOUD FAILURE
  that reports the owner as UNKNOWN — never a default, never a repair
  opportunity. The earlier protocol treated an absent manifest as "never
  cut over" and even let the installer write one; that made a deleted
  manifest silently resurrect stale legacy state (retry debt, deadlines,
  candidates and anchors included), so absence is now only ever resolved
  by the explicit operator decision above.

* RECOVERABLE. The document is self-contained and has no dependency on
  any provider document: it can be restored from a backup or from the
  operator's own knowledge without reading scheduler state. What it is
  NOT is inferable — not from the JSON documents (Phase 2 shadows may
  predate the protocol), not from mtimes, not from the legacy files.

* NOT PROVIDER STATE. Different file name, different schema, different
  key set. No provider name can produce it: provider documents are
  ``<provider>-state.json`` and provider names are validated against
  ``[a-z0-9][a-z0-9_-]*``, so ``backend-authority.json`` is unreachable
  as a provider document and vice versa.

FAILURE POLICY
--------------
An unreadable, invalid or ABSENT manifest is NEVER defaulted. Ownership
that cannot be determined mechanically must stop the process, not pick a
side: defaulting to legacy while JSON is authoritative would let an old
writer produce a second, diverging source of truth, and defaulting after
a JSON cutover would resurrect state the authoritative document has long
since moved past. Mutations fail closed; readers fail loudly.

The one function allowed to act on absence is
``bootstrap_legacy_authority``, and it does not infer: it records a LEGACY
ownership fact that an operator explicitly asserted. It is a lifecycle
step for a confirmed pre-protocol deployment; runtime code never calls it,
and neither does the installer, which is precisely what makes a later
disappearance detectable instead of self-healing.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .store import StateStoreError, _publish_atomic

AUTHORITY_FILENAME = "backend-authority.json"
AUTHORITY_SCHEMA_VERSION = 1

BACKEND_LEGACY = "legacy"
BACKEND_JSON = "json"
SUPPORTED_BACKENDS = (BACKEND_LEGACY, BACKEND_JSON)

# The bootstrap authority of a deployment that has never been cut over.
# Spelled out as a constructor (not a magic literal sprinkled around) so
# "absent manifest" has exactly one representation.
BOOTSTRAP_EPOCH = 0


class AuthorityError(StateStoreError):
    """Base class for authority-protocol failures."""


class AuthorityMissingError(AuthorityError):
    """No manifest exists in an initialized-or-unknown state directory.

    Raised by every ownership read. It is deliberately NOT a synonym for
    "legacy": a deployment that predates the protocol has that fact
    recorded explicitly by an operator (see
    ``bootstrap_legacy_authority``), and after that a missing manifest
    means the single ownership fact was lost. Guessing legacy there would
    silently roll the scheduler back to state the authoritative document
    has already superseded.
    """


class AuthorityCorruptError(AuthorityError):
    """The manifest bytes are unreadable, not UTF-8, or not JSON."""


class AuthoritySchemaError(AuthorityError):
    """The manifest parses but violates the versioned schemas."""


class ConcurrentAuthorityChangeError(AuthorityError):
    """Authority changed while a single logical operation was in flight.

    Raised by the router's generation-guarded read/write wrappers. It
    means another process completed a cutover/rollback concurrently, so
    the value this call was about to return (or just wrote) may belong to
    the wrong backend. Loud by design: the alternative is a silent
    mixed-backend read.
    """


@dataclass(frozen=True)
class BootstrapResult:
    """Outcome of ``bootstrap_legacy_authority``: the authority, and whether
    this call created it (False means a manifest was already there, so the
    call was a reported no-op)."""

    authority: "BackendAuthority"
    created: bool


# The old name, kept ONLY so an out-of-tree caller fails with a clear
# message instead of a bare AttributeError (see ``__getattr__`` below). It
# is deliberately not exported and deliberately not a working alias:
# quietly keeping two ways to perform the one destructive authority
# operation is how it gets performed by accident.
_OLD_INITIALIZE_NAME = "initialize_authority"


@dataclass(frozen=True)
class BackendAuthority:
    """The single durable ownership fact."""

    backend: str
    epoch: int

    def __post_init__(self) -> None:
        if self.backend not in SUPPORTED_BACKENDS:
            raise AuthoritySchemaError(
                f"unsupported authoritative backend {self.backend!r}; "
                f"this build understands {list(SUPPORTED_BACKENDS)}"
            )
        if type(self.epoch) is not int or self.epoch < 0:
            raise AuthoritySchemaError(
                f"authority epoch must be a plain non-negative int, "
                f"got {self.epoch!r}"
            )

    @property
    def is_json(self) -> bool:
        return self.backend == BACKEND_JSON

    def as_document(self) -> Dict[str, Any]:
        return {
            "schema_version": AUTHORITY_SCHEMA_VERSION,
            "backend": self.backend,
            "epoch": self.epoch,
        }

    def successor(self, backend: str) -> "BackendAuthority":
        """The authority produced by one durable ownership switch."""
        return BackendAuthority(backend, self.epoch + 1)


def bootstrap_authority() -> BackendAuthority:
    """The authority ``bootstrap_legacy_authority`` materializes.

    Named for what it is: the authority a deployment has BEFORE the
    protocol exists on disk, not a default that reads may fall back to.
    """
    return BackendAuthority(BACKEND_LEGACY, BOOTSTRAP_EPOCH)


def authority_path(state_dir: Path) -> Path:
    return Path(state_dir) / AUTHORITY_FILENAME


def serialize_authority(authority: BackendAuthority) -> bytes:
    """Authority → FINAL document bytes (deterministic, preflighted).

    Encoding happens here, before any caller may touch the filesystem, so
    a publish can only fail on filesystem errors.
    """
    text = json.dumps(
        authority.as_document(), ensure_ascii=False, sort_keys=True, indent=2
    )
    try:
        return (text + "\n").encode("utf-8", errors="strict")
    except UnicodeError as exc:  # unreachable: backend/epoch are ASCII
        raise AuthorityError(
            f"authority document is not UTF-8 representable: {exc}"
        ) from exc


def parse_authority(raw: bytes) -> BackendAuthority:
    """FINAL document bytes → validated authority. Strict by policy."""
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise AuthorityCorruptError(
            f"authority manifest is not valid UTF-8: {exc}"
        ) from exc
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise AuthorityCorruptError(
            f"authority manifest is not valid JSON: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise AuthoritySchemaError(
            f"authority manifest must be a JSON object, "
            f"got {type(document).__name__}"
        )
    expected = {"schema_version", "backend", "epoch"}
    keys = set(document)
    missing = sorted(expected - keys)
    if missing:
        raise AuthoritySchemaError(
            f"authority manifest is missing required keys {missing}"
        )
    unknown = sorted(keys - expected)
    if unknown:
        raise AuthoritySchemaError(
            f"authority manifest has unknown keys {unknown}; schema drift "
            "must fail loudly"
        )
    version = document["schema_version"]
    if type(version) is not int or version != AUTHORITY_SCHEMA_VERSION:
        raise AuthoritySchemaError(
            f"unsupported authority schema_version {version!r}; this build "
            f"understands exactly {AUTHORITY_SCHEMA_VERSION} — refusing to guess"
        )
    backend = document["backend"]
    if not isinstance(backend, str):
        raise AuthoritySchemaError(
            f"authority backend must be a string, got {type(backend).__name__}"
        )
    return BackendAuthority(backend, document["epoch"])


def read_authority(state_dir: Path) -> BackendAuthority:
    """The one and only ownership decision.

    * valid manifest   -> that authority
    * absent manifest  -> AuthorityMissingError (NEVER a default)
    * damaged manifest -> loud AuthorityError, never a default

    Pure: creates nothing, repairs nothing, chmods nothing. Callers that
    need a stable answer across a following operation must re-read and
    compare (see ``AuthoritativeStateStore``); this function deliberately
    offers no caching hook.
    """
    path = authority_path(state_dir)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise AuthorityMissingError(
            f"backend authority manifest is missing at {path}: refusing to "
            "guess between the legacy and JSON backends. The owner of this "
            "state is UNKNOWN — a deployment that cut over and then lost the "
            "manifest is indistinguishable from one that never had it, so "
            "nothing here will infer legacy. Restore the manifest from "
            "backup. ONLY if this deployment is confirmed to predate the "
            "authority protocol, assert it once with "
            "`quota-sentinel bootstrap-authority --assume-legacy`, an "
            "explicit operator decision the installer never makes for you."
        ) from exc
    except IsADirectoryError as exc:
        raise AuthorityCorruptError(
            f"authority manifest path is a directory: {exc}"
        ) from exc
    except OSError as exc:
        raise AuthorityCorruptError(
            f"authority manifest unreadable: {exc}"
        ) from exc
    return parse_authority(raw)


def write_authority(state_dir: Path, authority: BackendAuthority) -> None:
    """Durably publish the ownership fact in ONE atomic step.

    This is the cutover/rollback commit point: before it returns, every
    process sees the previous authority; after it returns, every process
    sees the new one.
    """
    directory = Path(state_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    except OSError as exc:
        raise AuthorityError(
            f"cannot prepare state dir {directory}: {exc}"
        ) from exc
    payload = serialize_authority(authority)
    try:
        _publish_atomic(authority_path(directory), payload)
    except OSError as exc:
        raise AuthorityError(
            f"failed to publish authority manifest in {directory}: {exc}"
        ) from exc


def read_authority_if_present(state_dir: Path) -> Optional[BackendAuthority]:
    """None when the manifest is absent, the authority when it is valid.

    For DIAGNOSTICS ONLY — reporting "this state dir has no manifest yet".
    Ownership decisions must use ``read_authority``, whose missing-manifest
    answer is a loud error rather than this None.
    """
    if not authority_path(state_dir).exists():
        return None
    return read_authority(state_dir)


def bootstrap_legacy_authority(state_dir: Path) -> BootstrapResult:
    """Assert, on an operator's explicit behalf, that this deployment's
    authoritative backend is legacy — and record it as epoch 0.

    THIS FUNCTION DOES NOT REPAIR ANYTHING, and the name says so on
    purpose. A missing manifest means the system does not know who owns
    the state; it does NOT mean "legacy". Those two situations are
    indistinguishable from the state directory alone:

      A. a deployment that predates the authority protocol, so no fact was
         ever written;
      B. a deployment that already cut over and whose manifest was lost.

    In case B, writing ``legacy`` epoch 0 would silently re-legitimize
    stale legacy deadlines and retry debt that the authoritative JSON
    documents have long since moved past. No amount of inspecting the
    directory can tell A from B — the JSON documents may predate the
    protocol too — so this is the one decision that cannot be inferred and
    must be ASSERTED.

    PRECONDITIONS (both the caller's responsibility; neither is verifiable
    here):

    * the caller holds the scheduler's ``run.lock``;
    * the caller has ESTABLISHED that this is case A — a confirmed
      pre-protocol deployment. The operator CLI requires an explicit
      ``--assume-legacy`` for exactly this reason, and the installer never
      calls this function at all.

    Contract:

    * absent manifest  -> publish ``legacy`` epoch 0 through the same
      atomic temp-write/fsync/rename primitive as every other authority
      write, then return it with ``created=True``;
    * valid manifest   -> return it UNCHANGED with ``created=False``. A
      JSON manifest is never rewritten as legacy, so an operator who runs
      this by mistake on a cut-over deployment gets a report, not a
      downgrade;
    * damaged manifest -> loud error; NEVER overwritten. Writing over
      corruption would destroy the only ownership fact.

    Scheduler state is not read, written or inferred here, and the
    presence of Phase 2 JSON documents is deliberately ignored.
    """
    existing = read_authority_if_present(state_dir)
    if existing is not None:
        return BootstrapResult(existing, created=False)
    authority = bootstrap_authority()
    write_authority(state_dir, authority)
    confirmed = read_authority(state_dir)
    if confirmed != authority:
        raise AuthorityError(
            f"authority bootstrap published {authority} but re-reading "
            f"it yielded {confirmed}; refusing to report success"
        )
    return BootstrapResult(confirmed, created=True)


def __getattr__(name: str):
    """Fail the RETIRED name loudly, with the reason attached.

    Removing ``initialize_authority`` is not a rename for tidiness: the old
    name invited every caller to materialize an ownership fact, including
    callers that had no business making that decision (an installer, an
    update path, a repair helper). The replacement cannot be called by
    accident because it is not named like a repair, and any survivor of the
    old call site now stops here with an explanation rather than silently
    acquiring a different behaviour.
    """
    if name == _OLD_INITIALIZE_NAME:
        raise AttributeError(
            "initialize_authority() has been REMOVED. Creating the authority "
            "manifest is an explicit operator decision, not a routine "
            "initialization: use bootstrap_legacy_authority() only from a "
            "command whose caller asserted --assume-legacy and holds run.lock. "
            "Runtime, installer and update paths must never call it."
        )
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
