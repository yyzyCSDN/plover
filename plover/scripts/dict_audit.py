# Copyright (c) 2026 The Plover Authors.
# See LICENSE.txt for details.

"""Offline dictionary upgrade compatibility audit console script.

Usage::

    plover_dict_audit audit_spec.json [--json report.json]

The audit specification is a JSON document::

    {
      "old": [{"path": "old/main.json", "enabled": true}, ...],
      "new": [{"path": "new/main.json", "enabled": true}, ...],
      "strokes": ["S", "T", "E", ...],
      "undo_levels": 100,
      "start_attached": false,
      "start_capitalized": false,
      "space_char": " ",
      "space_placement": "Before Output",
      "budget": {
        "max_states": 100000,
        "max_transitions": 2000000,
        "timeout_seconds": 600
      }
    }

Dictionary entries may also be provided inline with
``{"mappings": {"S": "some text", ...}}`` instead of ``path``.

The command never connects a keyboard, never executes engine/keyboard
operations and never writes to the audited dictionaries.

Exit codes: 0 compatible, 1 incompatible, 2 inconclusive, 3 usage error,
4 internal replay verification failure.
"""

import argparse
import json
import os
import sys

from plover import system
from plover.audit import (
    AuditBudget,
    FormattingSettings,
    audit_stacks,
    load_stack,
)
from plover.audit._explore import (
    STATUS_COMPATIBLE,
    STATUS_INCOMPATIBLE,
)
from plover.audit._pipeline import AuditScopeError, make_alphabet
from plover.config import DEFAULT_SYSTEM_NAME, DEFAULT_UNDO_LEVELS
from plover.formatting import SPACE_PLACEMENT_AFTER, SPACE_PLACEMENT_BEFORE
from plover.registry import registry

EXIT_COMPATIBLE = 0
EXIT_INCOMPATIBLE = 1
EXIT_INCONCLUSIVE = 2
EXIT_USAGE_ERROR = 3
EXIT_VERIFICATION_ERROR = 4


def _build_budget(spec):
    budget_spec = spec.get("budget", {})
    if not isinstance(budget_spec, dict):
        raise AuditScopeError("'budget' must be an object")
    kwargs = {}
    for name in ("max_states", "max_transitions", "timeout_seconds"):
        if name not in budget_spec:
            continue
        value = budget_spec[name]
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise AuditScopeError(
                    f"budget.{name} must be a positive number or null"
                )
            if value <= 0:
                raise AuditScopeError(
                    f"budget.{name} must be a positive number or null"
                )
        kwargs[name] = value
    return AuditBudget(**kwargs)


def _build_settings(spec):
    space_placement = spec.get("space_placement", SPACE_PLACEMENT_BEFORE)
    if space_placement not in (SPACE_PLACEMENT_BEFORE, SPACE_PLACEMENT_AFTER):
        raise AuditScopeError(
            "'space_placement' must be "
            f"{SPACE_PLACEMENT_BEFORE!r} or {SPACE_PLACEMENT_AFTER!r}"
        )
    undo_levels = spec.get("undo_levels", DEFAULT_UNDO_LEVELS)
    if isinstance(undo_levels, bool) or not isinstance(undo_levels, int):
        raise AuditScopeError("'undo_levels' must be a positive integer")
    if undo_levels < 1:
        raise AuditScopeError("'undo_levels' must be a positive integer")
    settings = FormattingSettings(
        start_attached=bool(spec.get("start_attached", False)),
        start_capitalized=bool(spec.get("start_capitalized", False)),
        space_char=spec.get("space_char", " "),
        space_placement=space_placement,
    )
    if not isinstance(settings.space_char, str):
        raise AuditScopeError("'space_char' must be a string")
    return settings, undo_levels


def run_audit(spec_path):
    with open(spec_path, encoding="utf-8") as fp:
        try:
            spec = json.load(fp)
        except json.JSONDecodeError as exc:
            raise AuditScopeError(f"invalid JSON in {spec_path}: {exc}") from exc
    if not isinstance(spec, dict):
        raise AuditScopeError("audit specification must be a JSON object")
    for required in ("old", "new", "strokes"):
        if required not in spec:
            raise AuditScopeError(f"missing required key: {required!r}")
    base_dir = os.path.dirname(os.path.abspath(spec_path))
    old_stack = load_stack(spec["old"], "old", base_dir=base_dir)
    new_stack = load_stack(spec["new"], "new", base_dir=base_dir)
    alphabet = make_alphabet(spec["strokes"])
    settings, undo_levels = _build_settings(spec)
    budget = _build_budget(spec)
    return audit_stacks(
        old_stack,
        new_stack,
        alphabet,
        undo_levels=undo_levels,
        settings=settings,
        budget=budget,
    )


def _format_report(result):
    lines = []
    lines.append(f"Dictionary audit verdict: {result.status.upper()}")
    lines.append(
        f"alphabet: {len(result.alphabet)} strokes, undo window: {result.undo_levels}"
    )
    abstraction = result.as_dict()["abstraction"]
    lines.append(
        "suffix horizon (state widening): "
        f"{abstraction['suffix_horizon']} "
        f"(derived from {abstraction['horizon_derivation']})"
    )
    coverage = result.stats
    lines.append("coverage evidence:")
    for key in (
        "visited_product_states",
        "expanded_product_nodes",
        "explored_transitions",
        "merged_transitions",
        "max_reached_depth",
        "old_side_out_of_domain_branches",
        "new_side_out_of_domain_branches",
        "elapsed_seconds",
    ):
        lines.append(f"  {key}: {coverage[key]}")
    if coverage["out_of_domain_examples"]:
        lines.append(
            "  out-of-domain examples (first "
            f"{len(coverage['out_of_domain_examples'])}):"
        )
        for example in coverage["out_of_domain_examples"]:
            lines.append(
                f"    - {example['side']} side, path={'/'.join(example['path'])}: "
                f"{example['reasons']}"
            )
    if result.inconclusive_reason is not None:
        lines.append(f"inconclusive reason: {result.inconclusive_reason}")
        lines.append(
            "no compatibility claim is made: the explored prefix is covered, "
            "but the full reachable state space was not closed."
        )
    if result.finding is not None:
        finding = result.finding
        lines.append(
            "shortest counterexample "
            f"({finding.stroke_count} stroke(s)): " + "/".join(finding.path)
        )
        lines.append(f"first divergence at stroke #{finding.divergence_index}")
        if finding.kind == "text_difference":
            lines.append(f"  old text: {finding.old_text!r}")
            lines.append(f"  new text: {finding.new_text!r}")
        else:
            lines.append(
                "  new side leaves the pure-text audit domain: "
                f"{[list(reason) for reason in finding.reasons]}"
            )
        lines.append(
            "replay verification: "
            f"{finding.verification.get('confirmed', False)} "
            f"({finding.verification.get('method')})"
        )
    if result.status == STATUS_COMPATIBLE:
        lines.append(
            "all reachable semantic states and alphabet successors are "
            "covered and text-equivalent within the stated domain."
        )
    return os.linesep.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Offline dictionary upgrade compatibility audit."
    )
    parser.add_argument("spec", help="path to the JSON audit specification")
    parser.add_argument(
        "--json",
        dest="json_report",
        default=None,
        help="optional path for a machine-readable JSON report",
    )
    args = parser.parse_args(argv)

    # The audit is pinned to the native English Stenotype system: no plugin
    # metas/macros and no system overrides are part of its scope.
    registry.update()
    system.setup(DEFAULT_SYSTEM_NAME)
    if system.NAME != DEFAULT_SYSTEM_NAME:  # pragma: no cover - defensive
        print(
            f"unsupported system {system.NAME!r}; "
            f"audit requires {DEFAULT_SYSTEM_NAME!r}",
            file=sys.stderr,
        )
        return EXIT_USAGE_ERROR
    try:
        result = run_audit(args.spec)
    except (AuditScopeError, OSError) as exc:
        print(f"audit specification error: {exc}", file=sys.stderr)
        return EXIT_USAGE_ERROR
    except AssertionError as exc:  # replay verification failure
        print(f"internal replay verification failed: {exc!r}", file=sys.stderr)
        return EXIT_VERIFICATION_ERROR

    report = result.as_dict()
    if args.json_report is not None:
        with open(args.json_report, "w", encoding="utf-8") as fp:
            json.dump(report, fp, indent=2, sort_keys=True)
            fp.write("\n")
    print(_format_report(result))
    if result.status == STATUS_COMPATIBLE:
        return EXIT_COMPATIBLE
    if result.status == STATUS_INCOMPATIBLE:
        return EXIT_INCOMPATIBLE
    return EXIT_INCONCLUSIVE


if __name__ == "__main__":
    sys.exit(main())
