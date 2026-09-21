"""Shell-facing quota CLI (Phase 4 bridge).

Two verbs, both thin wrappers over :mod:`quota_sentinel.quota`:

* ``quota-plan`` prints the fallback ladder and the capability facts the
  shell needs, so the ladder exists as DATA in one place instead of as three
  near-identical dispatch blocks in the shell.
* ``quota-normalize`` turns one tier's raw payload into the single document
  shape the scheduler and the cards read. It replaces the seven `jq`
  programs the shell used to carry (three Pi snapshots, three CodexBar
  payloads, one generic re-normalisation).

The NATIVE tiers are deliberately not here: their producers
(`antigravity_usage.py`, the inline CodexBar/Codex probes) already emit the
final document, and re-validating a document this process just built would
add a subprocess without adding a guarantee.

Exit codes follow the bridge convention: 0 ok, 1 "this tier produced no
usable quota" (the ladder's normal signal), 3 bad argument, 4 error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence

from ..state.migration import DEFAULT_PROVIDERS
from . import normalize
from .adapters import ADAPTERS, PROVIDERS, tier_plan
from .models import QuotaNormalizationError

# kind -> (normalizer, needs a raw JSON document)
NORMALIZERS: Dict[str, Callable] = {
    "pi-codex": normalize.normalize_pi_codex,
    "pi-antigravity": normalize.normalize_pi_antigravity,
    "pi-opencode": normalize.normalize_pi_opencode,
    "codexbar-codex": normalize.normalize_codexbar_codex,
    "codexbar-antigravity": normalize.normalize_codexbar_antigravity,
    "codexbar-opencode": normalize.normalize_codexbar_opencode,
}

RENORMALIZE = "renormalize"


def cmd_plan(args: argparse.Namespace) -> int:
    """The ladder, in order, with the facts the caller needs to run it."""
    providers: Sequence[str] = args.providers or list(PROVIDERS)
    for provider in providers:
        adapter = ADAPTERS.get(provider)
        if adapter is None:
            print(f"quota_sentinel: unknown provider {provider!r}", file=sys.stderr)
            return 3
        print(f"provider={provider}")
        print(f"title={adapter.title}")
        print(f"monthly_display_only={1 if adapter.monthly_display_only else 0}")
        for tier in tier_plan(provider):
            fresh = 1 if adapter.tier_is_fresh(tier) else 0
            print(f"tier={tier.value}\tfresh={fresh}")
        print(f"end={provider}")
    return 0


def _load_raw(path: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def cmd_normalize(args: argparse.Namespace) -> int:
    raw = _load_raw(Path(args.input))
    if raw is None:
        # An unreadable/undecodable payload is "this tier has nothing",
        # which the ladder is built to step over — not an error.
        return 1
    try:
        if args.kind == RENORMALIZE:
            document = normalize.renormalize_document(raw)
        else:
            normalizer = NORMALIZERS.get(args.kind)
            if normalizer is None:
                print(
                    f"quota_sentinel: unknown normalize kind {args.kind!r}",
                    file=sys.stderr,
                )
                return 3
            document = normalizer(raw).as_document()
    except QuotaNormalizationError as exc:
        if args.explain:
            print(f"quota-normalize[{args.kind}]: {exc}", file=sys.stderr)
        return 1
    output = Path(args.output)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"quota_sentinel: cannot prepare {output.parent}: {exc}", file=sys.stderr)
        return 4
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        output.write_text(payload, encoding="utf-8")
    except OSError as exc:
        print(f"quota_sentinel: cannot write {output}: {exc}", file=sys.stderr)
        return 4
    if args.summary:
        print(f"source={document.get('source', '')}")
        print(f"fresh={1 if document.get('fresh') else 0}")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    plan = sub.add_parser(
        "quota-plan",
        help="print the quota fallback ladder and capability facts",
    )
    plan.add_argument("providers", nargs="*", default=None)
    plan.set_defaults(handler=cmd_plan)

    norm = sub.add_parser(
        "quota-normalize",
        help="normalise one quota tier payload into the canonical document",
    )
    norm.add_argument("--kind", required=True)
    norm.add_argument("--input", required=True)
    norm.add_argument("--output", required=True)
    norm.add_argument("--explain", action="store_true",
                      help="print why a payload was rejected")
    norm.add_argument("--summary", action="store_true",
                      help="print the normalised source and freshness")
    norm.set_defaults(handler=cmd_normalize)


__all__ = ["register", "NORMALIZERS", "RENORMALIZE", "PROVIDERS"]
