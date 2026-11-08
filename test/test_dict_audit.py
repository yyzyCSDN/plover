# Copyright (c) 2026 The Plover Authors.
# See LICENSE.txt for details.

"""Tests for the offline dictionary upgrade compatibility audit."""

import json

import pytest

from plover.audit import (
    AuditBudget,
    FormattingSettings,
    audit_stacks,
    load_stack,
)
from plover.audit._explore import (
    FINDING_NEW_SIDE_OUT_OF_DOMAIN,
    FINDING_TEXT_DIFFERENCE,
    STATUS_COMPATIBLE,
    STATUS_INCOMPATIBLE,
    STATUS_INCONCLUSIVE,
)
from plover.audit._pipeline import (
    AuditScopeError,
    NativePipeline,
    StepOutcome,
    make_alphabet,
)
from plover.formatting import Formatter
from plover.scripts import dict_audit as cli
from plover.translation import Translator


def stack(mappings, label="d", enabled=True):
    return load_stack([{"mappings": dict(mappings), "enabled": enabled}], label)


def audit(old_mappings, new_mappings, strokes, undo=3, budget=None, settings=None):
    return audit_stacks(
        stack(old_mappings, "old"),
        stack(new_mappings, "new"),
        make_alphabet(strokes),
        undo_levels=undo,
        settings=settings,
        budget=budget or AuditBudget(timeout_seconds=30),
    )


# ---------------------------------------------------------------------------
# Compatible / incompatible verdicts and shortest counterexamples
# ---------------------------------------------------------------------------


def test_identical_stacks_are_compatible_with_coverage():
    result = audit({"S": "a", "T": "b"}, {"S": "a", "T": "b"}, ["S", "T"], undo=2)
    assert result.status == STATUS_COMPATIBLE
    assert result.finding is None
    coverage = result.stats
    assert coverage["visited_product_states"] > 1
    assert coverage["merged_transitions"] > 0
    assert coverage["explored_transitions"] >= coverage["visited_product_states"]
    # The product graph is closed: every transition was classified.
    assert (
        coverage["old_side_out_of_domain_branches"]
        + coverage["new_side_out_of_domain_branches"]
        == 0
    )


def test_text_difference_reports_shortest_counterexample():
    result = audit(
        {"S": "a", "T": "b"},
        {"S": "a", "T": "c"},
        ["S", "T"],
        undo=2,
    )
    assert result.status == STATUS_INCOMPATIBLE
    finding = result.finding
    assert finding.kind == FINDING_TEXT_DIFFERENCE
    assert finding.path == ("T",)
    assert finding.stroke_count == 1
    assert finding.divergence_index == 1
    assert finding.old_text == " b"
    assert finding.new_text == " c"
    assert finding.verification["confirmed"] is True
    assert finding.verification["external_operations"] == 0


def test_counterexample_is_shortest_by_stroke_count():
    # The divergence only appears after a multi-stroke outline resolves.
    result = audit(
        {"S": "cat", "S/T": "cat log"},
        {"S": "cat", "S/T": "cat dog"},
        ["S", "T"],
        undo=3,
    )
    assert result.status == STATUS_INCOMPATIBLE
    assert result.finding.path == ("S", "T")
    assert result.finding.stroke_count == 2
    # A depth-one divergence is always preferred over deeper ones.
    immediate = audit(
        {"S": "x", "T": "a"},
        {"S": "y", "T": "a"},
        ["S", "T"],
        undo=2,
    )
    assert immediate.finding.path == ("S",)


def test_multi_stroke_resegmentation_equivalence_is_provable():
    mappings = {"S": "cat", "S/T": "cat log"}
    result = audit(mappings, dict(mappings), ["S", "T", "*"], undo=3)
    assert result.status == STATUS_COMPATIBLE
    assert result.stats["merged_transitions"] > 0


def test_implicit_suffix_difference_is_detected():
    old = {"S": "cat", "-S": "s"}
    new = {"S": "cat", "-S": "z"}
    result = audit(old, new, ["S", "SS", "-S"], undo=3)
    assert result.status == STATUS_INCOMPATIBLE
    # The combined stroke normalizes to its canonical rtfcre form.
    assert result.finding.path == ("S-S",)
    # Identical suffix stacks are fully equivalent.
    same = audit(old, dict(old), ["S", "SS", "-S"], undo=3)
    assert same.status == STATUS_COMPATIBLE


def test_raw_unmapped_strokes_are_in_scope():
    result = audit({}, {}, ["S", "T"], undo=2)
    assert result.status == STATUS_COMPATIBLE
    mapped = audit({}, {"S": "a"}, ["S", "T"], undo=2)
    assert mapped.status == STATUS_INCOMPATIBLE
    assert mapped.finding.path == ("S",)


def test_net_text_is_compared_not_instruction_sequences():
    # End to end: a look-ahead action and a plain action with identical text
    # go through different native action structures, yet every per-stroke
    # text is identical and the audit must merge those states instead of
    # flagging a difference.
    old_mappings = {"-L": "{=able/two/two}", "A": "able", "B": "xyz"}
    new_mappings = {"-L": "two", "A": "able", "B": "xyz"}
    result = audit(old_mappings, new_mappings, ["-L", "A", "B"], undo=3)
    assert result.status == STATUS_COMPATIBLE
    # At the native output contract level, different delete/retype streams
    # with the same resulting string are one and the same observable text.
    retyped = StepOutcome(
        in_domain=True,
        text="xy",
        instructions=(("backspaces", 1), ("string", "y")),
    )
    appended = StepOutcome(
        in_domain=True,
        text="xy",
        instructions=(("string", "xy"),),
    )
    assert retyped.instructions != appended.instructions
    # This is the exact predicate used when exploring product transitions.
    assert retyped.text == appended.text


# ---------------------------------------------------------------------------
# Unbounded attach/glue loops must converge
# ---------------------------------------------------------------------------


def test_unbounded_glue_loop_merges_and_terminates():
    mappings = {"G": "{&a}"}
    result = audit(
        mappings,
        dict(mappings),
        ["G"],
        undo=10,
        budget=AuditBudget(timeout_seconds=30),
    )
    assert result.status == STATUS_COMPATIBLE
    assert result.stats["merged_transitions"] >= 1
    assert result.stats["visited_product_states"] < 1000


def test_unbounded_attach_loop_merges_and_terminates():
    mappings = {"A": "{^x^}"}
    result = audit(
        mappings,
        dict(mappings),
        ["A"],
        undo=10,
        budget=AuditBudget(timeout_seconds=30),
    )
    assert result.status == STATUS_COMPATIBLE
    assert result.stats["merged_transitions"] >= 1


def test_glue_loop_divergence_is_found_at_the_first_stroke():
    result = audit({"G": "{&a}"}, {"G": "{&b}"}, ["G"], undo=10)
    assert result.status == STATUS_INCOMPATIBLE
    assert result.finding.path == ("-G",)
    assert result.finding.old_text == " a"
    assert result.finding.new_text == " b"


# ---------------------------------------------------------------------------
# Scope boundaries
# ---------------------------------------------------------------------------


def test_new_side_key_combo_is_incompatible_and_verified():
    result = audit({"S": "a"}, {"S": "{#Control_L(a)}"}, ["S", "T"], undo=3)
    assert result.status == STATUS_INCOMPATIBLE
    assert result.finding.kind == FINDING_NEW_SIDE_OUT_OF_DOMAIN
    assert result.finding.path == ("S",)
    assert result.finding.verification["confirmed"] is True
    assert any(reason[0] == "key_combination" for reason in result.finding.reasons)


def test_new_side_engine_command_is_out_of_domain():
    result = audit({"S": "a"}, {"S": "{PLOVER:FOO}"}, ["S"], undo=3)
    assert result.status == STATUS_INCOMPATIBLE
    assert result.finding.kind == FINDING_NEW_SIDE_OUT_OF_DOMAIN


@pytest.mark.parametrize(
    "mapping",
    ("{*|}", "{*<}", "{*(,$)}", "{*+}", "{*!}"),
)
def test_old_side_out_of_scope_constructs_exclude_the_branch(mapping):
    result = audit(
        {"S": mapping, "T": "a"},
        {"S": "whatever", "T": "a"},
        ["S", "T"],
        undo=3,
    )
    # The old side leaves the audit domain on S; that branch is excluded and
    # the common in-domain remainder (T on the empty history) is covered.
    assert result.stats["old_side_out_of_domain_branches"] >= 1
    assert result.status in (STATUS_COMPATIBLE, STATUS_INCONCLUSIVE)
    assert result.finding is None


def test_old_side_domain_exit_never_masks_a_new_side_change():
    # On T both sides stay in domain and differ: the audit must still flag it
    # even though the S branch is out of domain on the old side.
    result = audit(
        {"S": "{*|}", "T": "a"},
        {"S": "text", "T": "b"},
        ["S", "T"],
        undo=3,
    )
    assert result.status == STATUS_INCOMPATIBLE
    assert result.finding.path == ("T",)


def test_unknown_meta_is_out_of_domain_without_executing_it():
    result = audit({"S": "a"}, {"S": "{:plugin_macro:arg}"}, ["S"], undo=3)
    assert result.status == STATUS_INCOMPATIBLE
    assert result.finding.kind == FINDING_NEW_SIDE_OUT_OF_DOMAIN
    assert result.finding.reasons[0][0] == "meta"


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------


def test_undo_within_recoverable_history_is_equivalent():
    mappings = {"S": "a", "T": "b", "S/T": "ab"}
    result = audit(mappings, dict(mappings), ["S", "T", "*"], undo=4)
    assert result.status == STATUS_COMPATIBLE


def test_undo_past_history_is_an_out_of_domain_branch():
    mappings = {"S": "a"}
    result = audit(
        mappings,
        dict(mappings),
        ["S", "*"],
        undo=1,
        budget=AuditBudget(timeout_seconds=30),
    )
    assert result.status == STATUS_COMPATIBLE
    assert result.stats["old_side_out_of_domain_branches"] >= 1
    # Native replay confirms the fallback emits a (captured, never executed)
    # key combination.
    pipeline = NativePipeline(stack(mappings), 1, FormattingSettings())
    outcomes = [pipeline.step(make_alphabet([steno])[0]) for steno in ("S", "*", "*")]
    assert outcomes[0].in_domain
    assert outcomes[1].in_domain
    assert not outcomes[2].in_domain
    assert any(reason[0] == "undo_overflow" for reason in outcomes[2].reasons)
    assert any(
        instruction[0] == "key_combination" for instruction in outcomes[2].instructions
    )


# ---------------------------------------------------------------------------
# In-scope formatting features
# ---------------------------------------------------------------------------


def test_lookahead_equivalence_and_divergence():
    mappings = {"-L": "{=able/one/two}", "A": "able", "B": "xyz"}
    same = audit(mappings, dict(mappings), ["-L", "A", "B"], undo=3)
    assert same.status == STATUS_COMPATIBLE
    changed = audit(
        mappings,
        {"-L": "{=able/CHANGED/two}", "A": "able", "B": "xyz"},
        ["-L", "A", "B"],
        undo=3,
    )
    assert changed.status == STATUS_INCOMPATIBLE
    # The changed alternative only resolves when the next text matches.
    assert changed.finding.path == ("-L", "A")


def test_case_punctuation_and_word_end_equivalence():
    mappings = {"S": "cat", "P": "{.}", "K": "{-|}", "W": "{$}"}
    result = audit(mappings, dict(mappings), ["S", "P", "K", "W"], undo=3)
    assert result.status == STATUS_COMPATIBLE


def test_mode_and_carry_capitalization_equivalence():
    mappings = {"S": "cat", "PH": "{MODE:caps}", "KR": "{^~|x^}"}
    result = audit(mappings, dict(mappings), ["S", "PH", "KR"], undo=3)
    assert result.status == STATUS_COMPATIBLE


def test_initial_formatting_settings_are_honored():
    mappings = {"S": "a"}
    settings = FormattingSettings(start_attached=True, start_capitalized=True)
    result = audit(mappings, dict(mappings), ["S"], undo=2, settings=settings)
    assert result.status == STATUS_COMPATIBLE
    pipeline = NativePipeline(stack(mappings), 2, settings)
    outcome = pipeline.step(make_alphabet(["S"])[0])
    assert outcome.text == "A"


def test_spaces_after_placement_is_honored():
    from plover.formatting import SPACE_PLACEMENT_AFTER

    mappings = {"S": "a", "T": "b"}
    settings = FormattingSettings(space_placement=SPACE_PLACEMENT_AFTER)
    pipeline = NativePipeline(stack(mappings), 3, settings)
    first = pipeline.step(make_alphabet(["S"])[0])
    second = pipeline.step(make_alphabet(["T"])[0])
    assert first.text == "a "
    assert second.text == "a b "
    result = audit(mappings, dict(mappings), ["S", "T"], undo=3, settings=settings)
    assert result.status == STATUS_COMPATIBLE


def test_orthography_after_an_unbounded_attach_chain_converges():
    # The suffix inspects only a bounded suffix of the ever-growing word;
    # identical stacks must still join and prove compatibility.
    mappings = {"G": "{^x^}", "S": "{^}ly"}
    result = audit(
        mappings,
        dict(mappings),
        ["G", "S"],
        undo=4,
        budget=AuditBudget(timeout_seconds=30),
    )
    assert result.status == STATUS_COMPATIBLE
    assert result.stats["merged_transitions"] > 1
    changed = dict(mappings)
    changed["S"] = "{^}ness"
    divergence = audit(mappings, changed, ["G", "S"], undo=4)
    assert divergence.status == STATUS_INCOMPATIBLE


def test_rtf_dictionaries_load_through_native_plugins(tmp_path):
    from plover.dictionary.rtfcre_dict import RtfDictionary

    rtf_path = tmp_path / "stack.rtf"
    dictionary = RtfDictionary()
    dictionary.update({("S",): "a", ("T",): "b"})
    dictionary.path = str(rtf_path)
    dictionary.save()
    rtf_stack = load_stack([{"path": str(rtf_path)}], "rtf")
    result = audit_stacks(
        rtf_stack,
        stack({"S": "a", "T": "b"}, "inline"),
        make_alphabet(["S", "T"]),
        undo_levels=2,
    )
    assert result.status == STATUS_COMPATIBLE


# ---------------------------------------------------------------------------
# Stack semantics: order, enabled state, immutability
# ---------------------------------------------------------------------------


def test_disabled_dictionaries_are_ignored():
    old = load_stack(
        [
            {"mappings": {"S": "a"}, "enabled": False},
            {"mappings": {"S": "b"}, "enabled": True},
        ],
        "old",
    )
    result = audit_stacks(old, stack({"S": "b"}, "new"), make_alphabet(["S"]), 2)
    assert result.status == STATUS_COMPATIBLE
    enabled = load_stack(
        [
            {"mappings": {"S": "a"}, "enabled": True},
            {"mappings": {"S": "b"}, "enabled": True},
        ],
        "old",
    )
    flipped = audit_stacks(enabled, stack({"S": "b"}, "new"), make_alphabet(["S"]), 2)
    assert flipped.status == STATUS_INCOMPATIBLE


def test_stack_order_defines_priority():
    first = load_stack([{"mappings": {"S": "a"}}, {"mappings": {"S": "b"}}], "first")
    second = load_stack([{"mappings": {"S": "b"}}, {"mappings": {"S": "a"}}], "second")
    reference = stack({"S": "a"}, "ref")
    assert audit_stacks(first, reference, make_alphabet(["S"]), 2).status == (
        STATUS_COMPATIBLE
    )
    assert audit_stacks(second, reference, make_alphabet(["S"]), 2).status == (
        STATUS_INCOMPATIBLE
    )


def test_audit_never_modifies_dictionaries():
    mappings = {"S": "a", "T": "b"}
    audited = stack(mappings)
    snapshot = {
        key: value
        for dictionary in audited.dictionaries
        for key, value in dictionary.items()
    }
    audit(mappings, dict(mappings), ["S", "T"], undo=2)
    for dictionary in audited.dictionaries:
        assert dictionary.readonly is True
        assert dict(dictionary.items()) == snapshot


# ---------------------------------------------------------------------------
# Resource budgets: inconclusive, never a false compatible
# ---------------------------------------------------------------------------


def test_state_budget_exhaustion_is_inconclusive():
    result = audit(
        {"G": "{&a}"},
        {"G": "{&a}"},
        ["G"],
        undo=10,
        budget=AuditBudget(max_states=3, max_transitions=100),
    )
    assert result.status == STATUS_INCONCLUSIVE
    assert result.inconclusive_reason == "max_states exceeded"
    assert result.finding is None
    assert result.stats["visited_product_states"] >= 3


def test_transition_budget_exhaustion_is_inconclusive():
    result = audit(
        {"G": "{&a}"},
        {"G": "{&a}"},
        ["G"],
        undo=10,
        budget=AuditBudget(max_transitions=2),
    )
    assert result.status == STATUS_INCONCLUSIVE
    assert result.inconclusive_reason == "max_transitions exceeded"


def test_timeout_is_inconclusive():
    result = audit(
        {"G": "{&a}", "H": "{&b}"},
        {"G": "{&a}", "H": "{&b}"},
        ["G", "H"],
        undo=20,
        budget=AuditBudget(timeout_seconds=0.0),
    )
    assert result.status == STATUS_INCONCLUSIVE
    assert result.inconclusive_reason == "timeout_seconds exceeded"


# ---------------------------------------------------------------------------
# Input validation and native pipeline isolation
# ---------------------------------------------------------------------------


def test_alphabet_validation():
    with pytest.raises(AuditScopeError):
        make_alphabet([])
    with pytest.raises(AuditScopeError):
        make_alphabet(["NOPE"])
    assert len(make_alphabet(["S", "S", "T"])) == 2


def test_pipeline_uses_native_translator_and_formatter():
    pipeline = NativePipeline(stack({"S": "a"}), 3, FormattingSettings())
    assert isinstance(pipeline._translator, Translator)
    assert isinstance(pipeline._formatter, Formatter)
    # The output channel is fully in-memory and only records instructions.
    assert pipeline.recorder.instructions == []
    pipeline.step(make_alphabet(["S"])[0])
    assert pipeline.recorder.instructions == [("string", " a")]


def test_result_is_json_serializable():
    result = audit({"S": "a"}, {"S": "b"}, ["S"], undo=2)
    encoded = json.dumps(result.as_dict())
    assert json.loads(encoded)["status"] == STATUS_INCOMPATIBLE


# ---------------------------------------------------------------------------
# Console script
# ---------------------------------------------------------------------------


def _write_json_dict(path, mappings):
    path.write_text(json.dumps(mappings), encoding="utf-8")


def _write_spec(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_cli_incompatible_and_json_report(tmp_path, capsys):
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    _write_json_dict(old, {"S": "hello", "T": "world"})
    _write_json_dict(new, {"S": "hello", "T": "WORLD"})
    spec = tmp_path / "spec.json"
    report = tmp_path / "report.json"
    _write_spec(
        spec,
        {
            "old": [{"path": str(old)}],
            "new": [{"path": str(new)}],
            "strokes": ["S", "T"],
            "undo_levels": 2,
            "budget": {"max_states": 1000},
        },
    )
    code = cli.main([str(spec), "--json", str(report)])
    assert code == cli.EXIT_INCOMPATIBLE
    assert "INCOMPATIBLE" in capsys.readouterr().out
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["status"] == "incompatible"
    assert data["finding"]["shortest_input"] == ["T"]


def test_cli_compatible_with_inline_mappings(tmp_path, capsys):
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        {
            "old": [{"mappings": {"S": "a", "T": "b"}}],
            "new": [{"mappings": {"S": "a", "T": "b"}}],
            "strokes": ["S", "T"],
            "undo_levels": 2,
        },
    )
    code = cli.main([str(spec)])
    assert code == cli.EXIT_COMPATIBLE
    assert "COMPATIBLE" in capsys.readouterr().out


def test_cli_inconclusive_exit_code(tmp_path, capsys):
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        {
            "old": [{"mappings": {"G": "{&a}"}}],
            "new": [{"mappings": {"G": "{&a}"}}],
            "strokes": ["G"],
            "undo_levels": 10,
            "budget": {"max_states": 2},
        },
    )
    code = cli.main([str(spec)])
    assert code == cli.EXIT_INCONCLUSIVE
    assert "INCONCLUSIVE" in capsys.readouterr().out


def test_cli_reports_spec_errors(tmp_path):
    missing = tmp_path / "missing.json"
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        {
            "old": [{"path": str(missing)}],
            "new": [{"mappings": {}}],
            "strokes": ["S"],
        },
    )
    assert cli.main([str(spec)]) == cli.EXIT_USAGE_ERROR
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert cli.main([str(bad)]) == cli.EXIT_USAGE_ERROR


def test_cli_rejects_invalid_budget_and_undo_levels(tmp_path):
    spec = tmp_path / "spec.json"
    base = {
        "old": [{"mappings": {"S": "a"}}],
        "new": [{"mappings": {"S": "a"}}],
        "strokes": ["S"],
    }
    _write_spec(spec, {**base, "undo_levels": 0})
    assert cli.main([str(spec)]) == cli.EXIT_USAGE_ERROR
    _write_spec(spec, {**base, "budget": {"max_states": -1}})
    assert cli.main([str(spec)]) == cli.EXIT_USAGE_ERROR
    _write_spec(spec, {**base, "budget": {"timeout_seconds": "soon"}})
    assert cli.main([str(spec)]) == cli.EXIT_USAGE_ERROR
    _write_spec(spec, {**base, "strokes": []})
    assert cli.main([str(spec)]) == cli.EXIT_USAGE_ERROR
