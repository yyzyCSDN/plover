# Copyright (c) 2026 The Plover Authors.
# See LICENSE.txt for details.

"""Isolated, side-effect free native translation pipeline for auditing.

The pipeline wraps the unmodified :class:`~plover.translation.Translator`
and :class:`~plover.formatting.Formatter` classes. Output is captured in
memory: nothing is ever typed on a keyboard and dictionaries are only read.
"""

from dataclasses import dataclass

from plover import system
from plover.dictionary.base import load_dictionary
from plover.formatting import (
    ATOM_RE,
    SPACE_PLACEMENT_BEFORE,
    Formatter,
    _Action,
    _get_meta,
    _LookAheadAction,
    _parse_meta,
)
from plover.steno import Stroke, normalize_steno
from plover.steno_dictionary import StenoDictionary, StenoDictionaryCollection
from plover.translation import (
    Translation,
    Translator,
    _mapping_to_macro,
    _State,
)

# Meta names that stay within the native pure-text scope of the audit.
# Everything else (retro_case, retro_currency, key_combo, command, generic
# macros, unknown/plugin metas...) makes a branch leave the audit domain.
IN_SCOPE_METAS = frozenset(
    (
        "attach",
        "carry_capitalize",
        "glue",
        "case",
        "comma",
        "stop",
        "word_end",
        "if_next_matches",
        "mode",
    )
)

# The only translator-level macro that remains within the audit domain, and
# only while the undo does not fall back to an OS-level key combination.
IN_SCOPE_MACROS = frozenset(("undo",))


class AuditScopeError(ValueError):
    """Raised when an audit specification cannot be prepared."""


class _TextRecorder:
    """In-memory replacement for the keyboard output.

    Mirrors the native output contract: ``send_backspaces`` erases characters
    and ``send_string`` appends them. Key combinations and engine commands are
    recorded instead of being executed, and flag the branch as out of the
    pure-text audit domain.
    """

    def __init__(self):
        self.text = ""
        self.instructions = []

    def reset(self, text=""):
        self.text = text
        self.instructions = []

    def send_backspaces(self, n):
        assert n <= len(self.text)
        self.text = self.text[:-n]
        self.instructions.append(("backspaces", n))

    def send_string(self, s):
        self.text += s
        self.instructions.append(("string", s))

    def send_key_combination(self, combo):
        self.instructions.append(("key_combination", combo))

    def send_engine_command(self, command):
        self.instructions.append(("engine_command", command))


@dataclass(frozen=True)
class FormattingSettings:
    """Initial formatter settings for an audit run."""

    start_attached: bool = False
    start_capitalized: bool = False
    space_char: str = " "
    space_placement: str = SPACE_PLACEMENT_BEFORE


@dataclass(frozen=True)
class StackSpec:
    """A plain dictionary stack, in priority order (highest priority first)."""

    dictionaries: tuple
    label: str

    @property
    def longest_key(self):
        return max(
            (d.longest_key for d in self.dictionaries if d.enabled),
            default=0,
        )


@dataclass(frozen=True)
class StepOutcome:
    """Result of replaying a single stroke on one side."""

    in_domain: bool
    text: str
    instructions: tuple
    reasons: tuple = ()
    events: tuple = ()


@dataclass
class _Snapshot:
    """Concrete, fully exact continuation state for one side."""

    translator_state: object
    formatter_config: tuple
    text: str


def clone_state(state):
    """Deep clone a native translator state without generic ``copy.deepcopy``.

    ``copy.deepcopy`` cannot be used on states holding look-ahead actions:
    the native :class:`~plover.formatting._LookAheadAction` proxies missing
    attributes through ``__getattr__``, which recurses while the generic
    reconstruction temporarily leaves the action without its resolved state.
    This structural clone builds fresh native instances instead, preserving
    object identity (e.g. translations referenced through ``replaced``) via
    an identity memo.
    """

    memo = {}

    def clone(obj):
        if isinstance(obj, (str, int, float, bool, bytes, type(None))):
            return obj
        obj_id = id(obj)
        if obj_id in memo:
            return memo[obj_id]
        if isinstance(obj, _LookAheadAction):
            action1 = clone(obj.action1)
            action2 = clone(obj.action2)
            clone_obj = _LookAheadAction(obj.pattern, action1, action2)
            memo[obj_id] = clone_obj
            clone_obj.action = action1 if obj.action is obj.action1 else action2
            return clone_obj
        if isinstance(obj, _Action):
            clone_obj = object.__new__(_Action)
            memo[obj_id] = clone_obj
            clone_obj.__dict__.update(
                {key: clone(value) for key, value in obj.__dict__.items()}
            )
            return clone_obj
        if isinstance(obj, Translation):
            clone_obj = Translation(obj.strokes, obj.english)
            memo[obj_id] = clone_obj
            clone_obj.rtfcre = obj.rtfcre
            clone_obj.replaced = [clone(item) for item in obj.replaced]
            clone_obj.formatting = [clone(item) for item in obj.formatting]
            clone_obj.is_retrospective_command = obj.is_retrospective_command
            return clone_obj
        if isinstance(obj, _State):
            clone_obj = _State()
            memo[obj_id] = clone_obj
            clone_obj.translations = [clone(item) for item in obj.translations]
            clone_obj.tail = None if obj.tail is None else clone(obj.tail)
            return clone_obj
        if isinstance(obj, list):
            clone_obj = []
            memo[obj_id] = clone_obj
            clone_obj.extend(clone(item) for item in obj)
            return clone_obj
        if isinstance(obj, tuple):
            return tuple(clone(item) for item in obj)
        if isinstance(obj, dict):
            clone_obj = {}
            memo[obj_id] = clone_obj
            clone_obj.update((clone(key), clone(value)) for key, value in obj.items())
            return clone_obj
        # Anything else (enums, Stroke ints, frozen helpers...) is immutable.
        return obj

    return clone(state)


def _inline_dictionary(mappings):
    dictionary = StenoDictionary()
    dictionary.update(
        (normalize_steno(steno), translation) for steno, translation in mappings.items()
    )
    dictionary.readonly = True
    return dictionary


def load_stack(entries, label, base_dir=None):
    """Build a :class:`StackSpec` from audit specification entries.

    Each entry is either ``{"path": <dictionary file>, "enabled": bool}`` or
    ``{"mappings": {<steno>: <translation>, ...}, "enabled": bool}``.
    Dictionaries are loaded through the native dictionary plugins and then
    marked read-only in memory so the audit cannot modify them.
    """
    dictionaries = []
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise AuditScopeError(
                f"{label}: dictionary entry #{position} must be an object"
            )
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise AuditScopeError(
                f"{label}: 'enabled' must be a boolean for entry #{position}"
            )
        if "path" in entry:
            path = entry["path"]
            if not isinstance(path, str):
                raise AuditScopeError(
                    f"{label}: 'path' must be a string for entry #{position}"
                )
            if base_dir is not None and not path.startswith(("asset:", "/")):
                import os

                if not os.path.isabs(path):
                    path = os.path.join(base_dir, path)
            dictionary = load_dictionary(path)
        elif "mappings" in entry:
            mappings = entry["mappings"]
            if not isinstance(mappings, dict):
                raise AuditScopeError(
                    f"{label}: 'mappings' must be an object for entry #{position}"
                )
            dictionary = _inline_dictionary(mappings)
        else:
            raise AuditScopeError(
                f"{label}: entry #{position} needs a 'path' or 'mappings'"
            )
        dictionary.enabled = enabled
        # Defensive: the audit only ever reads dictionaries.
        dictionary.readonly = True
        dictionaries.append(dictionary)
    return StackSpec(tuple(dictionaries), label)


def _scan_translation_scope(english):
    """Return out-of-scope reasons for a translation string, if any."""
    reasons = []
    if not english:
        return reasons
    for atom in filter(None, (x.strip(" ") for x in ATOM_RE.findall(english))):
        meta = _get_meta(atom)
        if meta is None:
            continue
        meta_name, _meta_arg = _parse_meta(meta)
        if meta_name not in IN_SCOPE_METAS:
            reasons.append(("meta", meta_name, atom))
    return reasons


class NativePipeline:
    """One audited side: native translator + formatter with captured output."""

    def __init__(self, stack, undo_levels, settings):
        self._stack = stack
        self._settings = settings
        self._undo_levels = undo_levels
        self.recorder = _TextRecorder()
        self._translator = None
        self._formatter = None
        self._events = []
        self._build()

    def _build(self):
        translator = Translator()
        translator.set_min_undo_length(self._undo_levels)
        translator.set_dictionary(
            StenoDictionaryCollection(list(self._stack.dictionaries))
        )
        formatter = Formatter()
        formatter.set_output(self.recorder)
        formatter.set_space_placement(self._settings.space_placement)
        formatter.start_attached = self._settings.start_attached
        formatter.start_capitalized = self._settings.start_capitalized
        formatter.space_char = self._settings.space_char
        # A single dispatcher guarantees the translation event is captured
        # before the native formatter runs (listener sets are unordered), so
        # out-of-scope metas remain identifiable even if formatting raises.
        translator.add_listener(self._dispatch_event)
        self._translator = translator
        self._formatter = formatter

    def _dispatch_event(self, undo, do, prev):
        self._events.append((undo, do, prev))
        self._formatter.format(undo, do, prev)

    @property
    def formatter_config(self):
        return (
            self._formatter.spaces_after,
            self._formatter.start_attached,
            self._formatter.start_capitalized,
            self._formatter.space_char,
            self._formatter.last_output_spaces_after,
        )

    def snapshot(self):
        return _Snapshot(
            translator_state=clone_state(self._translator.get_state()),
            formatter_config=self.formatter_config,
            text=self.recorder.text,
        )

    def restore(self, snapshot):
        self._translator.set_state(clone_state(snapshot.translator_state))
        (
            self._formatter.spaces_after,
            self._formatter.start_attached,
            self._formatter.start_capitalized,
            self._formatter.space_char,
            self._formatter.last_output_spaces_after,
        ) = snapshot.formatter_config
        self.recorder.reset(snapshot.text)
        self._events = []

    def _pre_detect_macro(self, stroke):
        translator = self._translator
        max_len = translator.get_dictionary().longest_key
        mapping = translator._lookup_with_prefix(
            max_len, translator.get_state().translations, [stroke]
        )
        return _mapping_to_macro(mapping, stroke)

    def step(self, stroke):
        """Replay one stroke natively and classify its scope."""
        self._events = []
        reasons = []
        macro = self._pre_detect_macro(stroke)
        if macro is not None and macro.name not in IN_SCOPE_MACROS:
            # Never execute out-of-scope (potentially plugin provided) macros.
            return StepOutcome(
                in_domain=False,
                text=self.recorder.text,
                instructions=tuple(self.recorder.instructions),
                reasons=(("macro", macro.name, stroke.rtfcre),),
                events=(),
            )
        executed_macro = macro.name if macro is not None else None
        try:
            self._translator.translate(stroke)
        except Exception as exc:
            # The capture listener runs before the formatter, so the
            # translations responsible for the failure are known: prefer the
            # precise out-of-scope meta reason over the raw exception.
            for _undo, do, _prev in self._events:
                for translation in do:
                    reasons.extend(_scan_translation_scope(translation.english))
            if not reasons:
                reasons.append(("exception", type(exc).__name__, str(exc)))
            return StepOutcome(
                in_domain=False,
                text=self.recorder.text,
                instructions=tuple(self.recorder.instructions),
                reasons=tuple(reasons),
                events=tuple(self._events),
            )
        # Scope scan of the translations the native translator emitted.
        for _undo, do, _prev in self._events:
            for translation in do:
                reasons.extend(_scan_translation_scope(translation.english))
        # Output backstop: any non-text operation is out of the audit domain.
        for kind, payload in self.recorder.instructions:
            if kind == "key_combination":
                if executed_macro == "undo":
                    reasons.append(("undo_overflow", payload))
                else:
                    reasons.append(("key_combination", payload))
            elif kind == "engine_command":
                reasons.append(("engine_command", payload))
        in_domain = not reasons
        return StepOutcome(
            in_domain=in_domain,
            text=self.recorder.text,
            instructions=tuple(self.recorder.instructions),
            reasons=tuple(reasons),
            events=tuple(self._events),
        )


def make_alphabet(steno_strokes):
    """Validate and normalize the finite raw stroke alphabet."""
    if not steno_strokes:
        raise AuditScopeError("the stroke alphabet must not be empty")
    alphabet = []
    seen = set()
    for steno in steno_strokes:
        if not isinstance(steno, str):
            raise AuditScopeError(f"invalid stroke in alphabet: {steno!r}")
        try:
            stroke = Stroke(steno)
        except (ValueError, KeyError) as exc:
            raise AuditScopeError(f"invalid stroke in alphabet: {steno!r}") from exc
        if stroke not in seen:
            seen.add(stroke)
            alphabet.append(stroke)
    return tuple(alphabet)


def derive_horizon(old_stack, new_stack, alphabet, undo_levels):
    """Compute the string suffix horizon used by state widening.

    The only state component that can grow without bounds over a finite
    dictionary stack is the current ``word`` (and, equivalently, the rendered
    text), through attach/glue chaining. Every in-scope reader of those
    strings only inspects a bounded suffix:

    * orthography rules only match fixed-length tails and wordlist membership
      can only hold for words no longer than the longest listed word;
    * ``rightmost_word`` / glue chaining only need to locate the last word
      boundary, at most one dictionary atom away;
    * ``last_text(k)`` queries are bounded by the replaced suffix length,
      itself bounded by the longest dictionary atom plus an orthography rule
      tail.

    Everything else in the semantic state is finite control data and is kept
    exact.
    """
    atom_lengths = [len(stroke.rtfcre) for stroke in alphabet]
    rule_tail_lengths = [0]
    for pattern, _replacement in system.ORTHOGRAPHY_RULES:
        pattern_source = pattern.pattern
        head = pattern_source.split(" ^ ", 1)[0]
        rule_tail_lengths.append(len(head))
    wordlist = system.ORTHOGRAPHY_WORDS
    max_word_len = max((len(word) for word in wordlist), default=0)
    for stack in (old_stack, new_stack):
        for dictionary in stack.dictionaries:
            if not dictionary.enabled:
                continue
            for key, english in dictionary.items():
                atom_lengths.append(len("/".join(key)))
                if english:
                    atom_lengths.extend(len(atom) for atom in ATOM_RE.findall(english))
    max_atom = max(atom_lengths, default=1)
    max_rule_tail = max(rule_tail_lengths)
    history_window = max(undo_levels, old_stack.longest_key, new_stack.longest_key, 1)
    horizon = (
        max(max_word_len, max_rule_tail, max_atom)
        + max_atom * max(history_window, 1)
        + max_atom
        + 16
    )
    return horizon, {
        "max_atom_length": max_atom,
        "max_orthography_rule_tail": max_rule_tail,
        "max_wordlist_word_length": max_word_len,
        "history_window": history_window,
    }
