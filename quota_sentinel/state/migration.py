"""Bootstrap migration: legacy per-slot files -> v1 JSON documents (Phase 2).

Ownership reality during Phase 2: the shell's per-slot files remain the
AUTHORITATIVE runtime state; JSON documents are SHADOW snapshots created
by this module. Consequences, by design:

* seed-if-absent, never overwrite. If ``<provider>-state.json`` exists,
  it WINS — legacy slot files are ignored even if fresher. Re-seeding or
  syncing shadow content is a cutover-phase decision, not a bootstrap
  side effect; a rerun of migrate() is a no-op (idempotent by rule).
* the publish is atomic at the syscall level (link(2)/EEXIST), mirroring
  the shell's seed_provider_state_file: a migration racing a JSON writer
  can only skip, never clobber.
* legacy interpretation is FileStateStore's job — this module parses no
  slot files itself. Whatever FileStateStore.load() reads (including
  unset-on-garbage degradation) is what gets serialized; corrupt legacy
  content is never "repaired" here or on disk.
* legacy slot files are left fully untouched: no move, no delete, no
  truncate. Rollback is therefore automatic (just stop reading JSON).
* directory creation/chmod happens HERE (bootstrap write path), never
  inside any load().
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

from .json_store import JsonStateStore
from .schema import serialize_state
from .store import FileStateStore, StateStoreError

# Must match the shell's readonly PROVIDERS in quota-sentinel.sh; the
# parity regression asserts this roster against the real shell array.
DEFAULT_PROVIDERS: Tuple[str, ...] = ("codex", "antigravity", "opencode")

ACTION_SEEDED = "seeded"
ACTION_EXISTS = "json-exists"


def seed_document_if_absent(json_path: Path, payload: bytes) -> bool:
    """Publish FINAL bytes only when no document exists.

    Returns True when this call seeded the document, False when a
    document already existed (ours was dropped). link(2) makes the
    existence test and the creation one atomic act — no check-then-write
    window against a concurrent writer.
    """
    directory = json_path.parent
    # pid alone is insufficient across threads in one process; add
    # randomness so concurrent seeds never collide on the temp name.
    temp = directory / (
        f"{json_path.name}.tmp.{os.getpid()}.{os.urandom(4).hex()}.seed"
    )
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, json_path)
            seeded = True
        except FileExistsError:
            seeded = False
        except OSError as exc:
            raise StateStoreError(
                f"failed to seed state document {json_path}: {exc}"
            ) from exc
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass
    return seeded


def migrate_provider(state_dir: Path, provider: str) -> str:
    """Materialize <provider>-state.json from current legacy slot state.

    Returns ACTION_SEEDED or ACTION_EXISTS. Raises StateStoreError when
    the interpreted legacy state cannot be serialized into a valid v1
    document (pathological content FileStateStore could read but the
    document schema refuses) — loudly, creating nothing.
    """
    json_store = JsonStateStore(state_dir)
    json_path = json_store.document_path(provider)

    if json_path.exists():
        return ACTION_EXISTS

    # Pure reads only up to this point (load never creates/repairs).
    legacy_state = FileStateStore(state_dir).load(provider)
    payload = serialize_state(legacy_state, provider)  # full preflight

    try:
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        os.chmod(state_dir, 0o700)
    except OSError as exc:
        raise StateStoreError(
            f"cannot prepare state dir {state_dir}: {exc}"
        ) from exc

    return ACTION_SEEDED if seed_document_if_absent(json_path, payload) else ACTION_EXISTS


def migrate_all(
    state_dir: Path, providers: Optional[Sequence[str]] = None
) -> Dict[str, str]:
    roster: Sequence[str] = providers if providers else DEFAULT_PROVIDERS
    return {provider: migrate_provider(Path(state_dir), provider)
            for provider in roster}
