"""Provider scheduler state models.

The models are the Python-side view of the shell scheduler's authoritative
state files (see ARCHITECTURE.md). Field semantics deliberately mirror the
shell getters for every value the project's writers can produce:

* a missing or unparsable slot reads as ``None`` — a load never repairs;
* ``retry_pending`` is ``False`` both when the file is absent and when it
  holds ``"0"`` (the shell only treats the literal ``1`` as pending);
* ``reset_candidate`` is transient: ``None`` means "definitely no
  candidate", the same meaning the file's absence carries in the shell. A
  future JSON backend must encode that as an explicit ``null``; a
  *missing key* is a schema error, never business state.

The compound parser accepts only the canonical ``reset:observed`` form.
The shell reader is looser on pathological content (it slices first:last
of any colon-bearing string); that content cannot be produced by project
writers, and the stricter behavior here is a registered, deliberate
divergence (see tests/state-store-parity-regression.zsh), not an
accidental drift.

Models are plain carriers: they do not re-validate what commit() checks
at the persistence boundary.

Models know nothing about file names or layout — that is store.py's job.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ResetCandidate:
    """A far-later reset awaiting independent confirmation.

    Both timestamps come from the shell's compound ``reset:observed`` slot;
    the pair changes as one atomic unit by design, so it is one value here.
    """

    reset_at: int
    observed_at: int

    def as_compound(self) -> str:
        return f"{self.reset_at}:{self.observed_at}"


@dataclass(frozen=True)
class ProviderState:
    """The eight scheduler-state slots of one provider, as loaded from disk."""

    last_attempt_at: Optional[int] = None
    last_task_at: Optional[int] = None
    next_due_at: Optional[int] = None
    retry_pending: bool = False
    last_known_reset: Optional[int] = None
    last_triggered_window: Optional[str] = None
    reset_anchor: Optional[int] = None
    reset_candidate: Optional[ResetCandidate] = None
