"""Provider -> transport dispatch: one runner surface for two mechanisms.

``Application`` asks a single runner to prepare and attempt every provider. Two
mechanisms exist: the Pi agent (``runtime.models.ModelRunner``) for the providers
whose subscription is OAuth or a local service, and the vendor's own HTTP API
(``runtime.direct.DirectRunner``) for the ones whose whole credential is an API
key. Routing lives here, and only here, so the coordinator, the retry debt, the
locks and the cards stay unaware of which one answered.

The provider -> transport mapping is not decided here: it is the ``transport``
capability on each provider's adapter, passed in by the composition root.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

__all__ = ["TransportRouter"]


class TransportRouter:
    """Delegate one provider's prepare/run to the runner that owns its transport."""

    def __init__(
        self, runners: Mapping[str, object], transports: Mapping[str, str]
    ) -> None:
        self._runners = dict(runners)
        self._transports = dict(transports)

    def transport(self, provider: str) -> str:
        try:
            return self._transports[provider]
        except KeyError:
            raise ValueError("unknown provider: %s" % provider) from None

    def runner_for(self, provider: str):
        name = self.transport(provider)
        runner = self._runners.get(name)
        if runner is None:
            raise ValueError(
                "provider %r needs transport %r, which is not configured"
                % (provider, name)
            )
        return runner

    def prepare(self, provider: str, workspace: Path):
        return self.runner_for(provider).prepare(provider, workspace)

    def run(self, provider: str, workspace: Path, phase: str, attempt: int, limit: int):
        return self.runner_for(provider).run(
            provider, workspace, phase, attempt, limit
        )
