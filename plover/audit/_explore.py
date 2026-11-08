# Copyright (c) 2026 The Plover Authors.
# See LICENSE.txt for details.

"""Exhaustive product-state exploration for dictionary compatibility.

The audit explores the synchronous product of the two native pipelines over
the finite stroke alphabet. Concrete continuation states are kept exact for
replay; a structural state signature with a proven suffix horizon is used for
join (widening), so unbounded attach/glue loops converge instead of blowing up
the state space.

A "compatible" verdict is only returned when the visited product graph is
fully closed: every reachable semantic state and every alphabet successor has
been covered. Running out of budget always yields "inconclusive", never a
false "compatible".
"""

import time
from collections import deque
from dataclasses import dataclass, field

from plover import system
from plover.audit._pipeline import (
    AuditScopeError,
    FormattingSettings,
    NativePipeline,
    derive_horizon,
)
from plover.config import DEFAULT_SYSTEM_NAME
from plover.formatting import _LookAheadAction
from plover.steno import Stroke

STATUS_COMPATIBLE = "compatible"
STATUS_INCOMPATIBLE = "incompatible"
STATUS_INCONCLUSIVE = "inconclusive"

FINDING_TEXT_DIFFERENCE = "text_difference"
FINDING_NEW_SIDE_OUT_OF_DOMAIN = "new_side_out_of_domain"

_OOD_EXAMPLE_LIMIT = 25


@dataclass(frozen=True)
class AuditBudget:
    """Resource limits for the exploration.

    ``None`` means unlimited. Exhausting any limit makes the audit return
    an inconclusive verdict with partial coverage evidence.
    """

    max_states: int | None = 100_000
    max_transitions: int | None = 2_000_000
    timeout_seconds: float | None = 600.0


@dataclass
class Finding:
    kind: str
    path: tuple
    divergence_index: int
    old_text: str | None = None
    new_text: str | None = None
    reasons: tuple = ()
    verification: dict = field(default_factory=dict)

    @property
    def stroke_count(self):
        return len(self.path)

    def as_dict(self):
        data = {
            "kind": self.kind,
            "shortest_input": list(self.path),
            "stroke_count": self.stroke_count,
            "divergence_stroke_index": self.divergence_index,
            "verification": self.verification,
        }
        if self.kind == FINDING_TEXT_DIFFERENCE:
            data["old_text"] = self.old_text
            data["new_text"] = self.new_text
        else:
            data["new_side_reasons"] = [list(reason) for reason in self.reasons]
        return data


@dataclass
class AuditResult:
    status: str
    alphabet: tuple
    undo_levels: int
    settings: FormattingSettings
    horizon: int
    horizon_inputs: dict
    stats: dict
    finding: Finding | None = None
    inconclusive_reason: str | None = None
    old_stack_summary: dict = field(default_factory=dict)
    new_stack_summary: dict = field(default_factory=dict)

    def as_dict(self):
        data = {
            "status": self.status,
            "alphabet": [stroke.rtfcre for stroke in self.alphabet],
            "undo_levels": self.undo_levels,
            "formatting": {
                "start_attached": self.settings.start_attached,
                "start_capitalized": self.settings.start_capitalized,
                "space_char": self.settings.space_char,
                "space_placement": self.settings.space_placement,
            },
            "abstraction": {
                "suffix_horizon": self.horizon,
                "horizon_derivation": self.horizon_inputs,
            },
            "coverage": self.stats,
            "old_stack": self.old_stack_summary,
            "new_stack": self.new_stack_summary,
        }
        if self.finding is not None:
            data["finding"] = self.finding.as_dict()
        if self.inconclusive_reason is not None:
            data["inconclusive_reason"] = self.inconclusive_reason
        return data


# ---------------------------------------------------------------------------
# State signatures (structural, with suffix widening)
# ---------------------------------------------------------------------------


def _truncate(value, horizon):
    if isinstance(value, str) and len(value) > horizon:
        # Only the bounded suffix of a long string can influence future
        # in-scope decisions (see derive_horizon), so the unseen prefix and
        # the exact length are abstracted away. This is what lets unbounded
        # attach/glue loops converge instead of growing forever.
        return ("~", value[-horizon:])
    return value


def _action_signature(action, horizon):
    if isinstance(action, _LookAheadAction):
        return (
            "lookahead",
            action.pattern,
            _action_signature(action.action1, horizon),
            _action_signature(action.action2, horizon),
        )
    items = []
    for key, value in sorted(action.__dict__.items()):
        items.append((key, _truncate(value, horizon)))
    return ("action", tuple(items))


def _translation_signature(translation, index_by_id, horizon):
    replaced = []
    for previous in translation.replaced:
        position = index_by_id.get(id(previous))
        if position is None:
            # Defensive: in-domain states always reference stored
            # translations; keep the structure inline if not.
            position = (
                "inline",
                previous.rtfcre,
                previous.english,
                tuple(
                    _action_signature(action, horizon) for action in previous.formatting
                ),
            )
        replaced.append(position)
    return (
        translation.rtfcre,
        translation.english,
        translation.is_retrospective_command,
        tuple(replaced),
        tuple(_action_signature(action, horizon) for action in translation.formatting),
    )


def _state_signature(snapshot, horizon):
    state = snapshot.translator_state
    index_by_id = {
        id(translation): index for index, translation in enumerate(state.translations)
    }
    translations = tuple(
        _translation_signature(translation, index_by_id, horizon)
        for translation in state.translations
    )
    tail = None
    if state.tail is not None:
        tail = _translation_signature(state.tail, {}, horizon)
    return (snapshot.formatter_config, translations, tail)


# ---------------------------------------------------------------------------
# Independent native replay verification
# ---------------------------------------------------------------------------


def _replay(stack, stroke_path, undo_levels, settings):
    """Replay a stroke sequence from empty state on a fresh native pipeline."""
    pipeline = NativePipeline(stack, undo_levels, settings)
    per_stroke = []
    for rtfcre in stroke_path:
        outcome = pipeline.step(Stroke(rtfcre))
        per_stroke.append(outcome)
    return per_stroke


def _verify_finding(finding, stacks, undo_levels, settings):
    old_stack, new_stack = stacks
    old_replay = _replay(old_stack, finding.path, undo_levels, settings)
    new_replay = _replay(new_stack, finding.path, undo_levels, settings)
    index = finding.divergence_index - 1
    verification = {
        "method": "isolated native Translator/Formatter replay",
        "external_operations": 0,
        "replayed_strokes": list(finding.path),
    }
    if finding.kind == FINDING_TEXT_DIFFERENCE:
        for earlier in range(index):
            assert old_replay[earlier].in_domain
            assert new_replay[earlier].in_domain
            assert old_replay[earlier].text == new_replay[earlier].text
        assert old_replay[index].in_domain
        assert new_replay[index].in_domain
        assert old_replay[index].text != new_replay[index].text
        verification["confirmed"] = True
        verification["old_text"] = old_replay[index].text
        verification["new_text"] = new_replay[index].text
    else:
        for earlier in range(index):
            assert old_replay[earlier].in_domain
            assert new_replay[earlier].in_domain
            assert old_replay[earlier].text == new_replay[earlier].text
        assert old_replay[index].in_domain
        assert not new_replay[index].in_domain
        verification["confirmed"] = True
        verification["new_side_reasons"] = [
            list(reason) for reason in new_replay[index].reasons
        ]
    return verification


# ---------------------------------------------------------------------------
# Product exploration
# ---------------------------------------------------------------------------


@dataclass
class _Node:
    old_snapshot: object
    new_snapshot: object
    depth: int
    path: tuple


def _stack_summary(stack):
    return {
        "label": stack.label,
        "dictionaries": [
            {
                "path": dictionary.path,
                "enabled": dictionary.enabled,
                "entries": len(dictionary),
                "longest_key": dictionary.longest_key,
            }
            for dictionary in stack.dictionaries
        ],
        "longest_key": stack.longest_key,
    }


class _CoverageStats:
    def __init__(self):
        self.visited_states = 1
        self.expanded_nodes = 0
        self.transitions = 0
        self.merged_transitions = 0
        self.max_depth = 0
        self.old_side_out_of_domain = 0
        self.new_side_out_of_domain = 0
        self.ood_examples = []

    def record_ood(self, side, path, reasons):
        if side == "old":
            self.old_side_out_of_domain += 1
        else:
            self.new_side_out_of_domain += 1
        if len(self.ood_examples) < _OOD_EXAMPLE_LIMIT:
            self.ood_examples.append(
                {
                    "side": side,
                    "path": list(path),
                    "reasons": [list(reason) for reason in reasons],
                }
            )

    def as_dict(self):
        return {
            "visited_product_states": self.visited_states,
            "expanded_product_nodes": self.expanded_nodes,
            "explored_transitions": self.transitions,
            "merged_transitions": self.merged_transitions,
            "max_reached_depth": self.max_depth,
            "old_side_out_of_domain_branches": self.old_side_out_of_domain,
            "new_side_out_of_domain_branches": self.new_side_out_of_domain,
            "out_of_domain_examples": self.ood_examples,
        }


def audit_stacks(
    old_stack,
    new_stack,
    alphabet,
    undo_levels,
    settings=None,
    budget=None,
):
    """Audit two dictionary stacks for per-stroke text equivalence.

    Returns an :class:`AuditResult`. The exploration is exhaustive over the
    finite alphabet and terminates either with a shortest counterexample, a
    full-coverage compatibility verdict, or an explicit inconclusive verdict
    when the budget is exhausted.
    """
    if settings is None:
        settings = FormattingSettings()
    if budget is None:
        budget = AuditBudget()
    # The audit scope is pinned to the native English Stenotype system and
    # its wordlist; system overrides and plugin-provided systems are out of
    # scope by construction.
    if system.NAME != DEFAULT_SYSTEM_NAME:
        raise AuditScopeError(
            f"unsupported system {system.NAME!r}; the audit requires "
            f"{DEFAULT_SYSTEM_NAME!r}"
        )
    horizon, horizon_inputs = derive_horizon(
        old_stack, new_stack, alphabet, undo_levels
    )
    old_pipeline = NativePipeline(old_stack, undo_levels, settings)
    new_pipeline = NativePipeline(new_stack, undo_levels, settings)
    old_root = old_pipeline.snapshot()
    new_root = new_pipeline.snapshot()
    root_signature = (
        _state_signature(old_root, horizon),
        _state_signature(new_root, horizon),
    )
    visited = {root_signature}
    queue = deque([_Node(old_root, new_root, depth=0, path=())])
    stats = _CoverageStats()
    started = time.monotonic()

    def elapsed():
        return time.monotonic() - started

    def inconclusive(reason):
        return AuditResult(
            status=STATUS_INCONCLUSIVE,
            alphabet=alphabet,
            undo_levels=undo_levels,
            settings=settings,
            horizon=horizon,
            horizon_inputs=horizon_inputs,
            stats=_final_stats(stats, elapsed()),
            inconclusive_reason=reason,
            old_stack_summary=_stack_summary(old_stack),
            new_stack_summary=_stack_summary(new_stack),
        )

    while queue:
        if budget.timeout_seconds is not None and elapsed() > budget.timeout_seconds:
            return inconclusive("timeout_seconds exceeded")
        node = queue.popleft()
        stats.expanded_nodes += 1
        for stroke in alphabet:
            stats.transitions += 1
            if (
                budget.max_transitions is not None
                and stats.transitions > budget.max_transitions
            ):
                return inconclusive("max_transitions exceeded")
            path = node.path + (stroke.rtfcre,)
            old_pipeline.restore(node.old_snapshot)
            new_pipeline.restore(node.new_snapshot)
            old_outcome = old_pipeline.step(stroke)
            new_outcome = new_pipeline.step(stroke)
            # An old-side domain exit excludes the branch from the guarantee:
            # the new side's behavior there is irrelevant and the two domains
            # must not be intersected to hide the change.
            if not old_outcome.in_domain:
                stats.record_ood("old", path, old_outcome.reasons)
                continue
            if not new_outcome.in_domain:
                stats.record_ood("new", path, new_outcome.reasons)
                finding = Finding(
                    kind=FINDING_NEW_SIDE_OUT_OF_DOMAIN,
                    path=path,
                    divergence_index=len(path),
                    reasons=new_outcome.reasons,
                )
                finding.verification = _verify_finding(
                    finding,
                    (old_stack, new_stack),
                    undo_levels,
                    settings,
                )
                return _incompatible(
                    finding,
                    alphabet,
                    undo_levels,
                    settings,
                    horizon,
                    horizon_inputs,
                    stats,
                    elapsed(),
                    old_stack,
                    new_stack,
                )
            if old_outcome.text != new_outcome.text:
                finding = Finding(
                    kind=FINDING_TEXT_DIFFERENCE,
                    path=path,
                    divergence_index=len(path),
                    old_text=old_outcome.text,
                    new_text=new_outcome.text,
                )
                finding.verification = _verify_finding(
                    finding,
                    (old_stack, new_stack),
                    undo_levels,
                    settings,
                )
                return _incompatible(
                    finding,
                    alphabet,
                    undo_levels,
                    settings,
                    horizon,
                    horizon_inputs,
                    stats,
                    elapsed(),
                    old_stack,
                    new_stack,
                )
            old_snapshot = old_pipeline.snapshot()
            new_snapshot = new_pipeline.snapshot()
            signature = (
                _state_signature(old_snapshot, horizon),
                _state_signature(new_snapshot, horizon),
            )
            if signature in visited:
                stats.merged_transitions += 1
                continue
            visited.add(signature)
            stats.visited_states += 1
            stats.max_depth = max(stats.max_depth, len(path))
            if (
                budget.max_states is not None
                and stats.visited_states > budget.max_states
            ):
                return inconclusive("max_states exceeded")
            queue.append(_Node(old_snapshot, new_snapshot, len(path), path))

    # The queue drained: every reachable product state and every alphabet
    # successor was covered, and no divergence was found.
    return AuditResult(
        status=STATUS_COMPATIBLE,
        alphabet=alphabet,
        undo_levels=undo_levels,
        settings=settings,
        horizon=horizon,
        horizon_inputs=horizon_inputs,
        stats=_final_stats(stats, elapsed()),
        old_stack_summary=_stack_summary(old_stack),
        new_stack_summary=_stack_summary(new_stack),
    )


def _final_stats(stats, elapsed_seconds):
    data = stats.as_dict()
    data["elapsed_seconds"] = round(elapsed_seconds, 6)
    return data


def _incompatible(
    finding,
    alphabet,
    undo_levels,
    settings,
    horizon,
    horizon_inputs,
    stats,
    elapsed_seconds,
    old_stack,
    new_stack,
):
    return AuditResult(
        status=STATUS_INCOMPATIBLE,
        alphabet=alphabet,
        undo_levels=undo_levels,
        settings=settings,
        horizon=horizon,
        horizon_inputs=horizon_inputs,
        stats=_final_stats(stats, elapsed_seconds),
        finding=finding,
        old_stack_summary=_stack_summary(old_stack),
        new_stack_summary=_stack_summary(new_stack),
    )
