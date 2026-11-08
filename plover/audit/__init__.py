# Copyright (c) 2026 The Plover Authors.
# See LICENSE.txt for details.

"""Offline dictionary upgrade compatibility auditing.

This package implements an exhaustive, offline equivalence check between two
plain dictionary stacks (the *old* stack and the *new* stack), restricted to
the native English Stenotype pure-text scope:

* multi-stroke backward re-segmentation and prefix lookups,
* implicit suffixes and orthography,
* attach/glue (including unbounded attach/glue loops),
* forward case and mode, punctuation, word-end and carry capitalization,
* look-ahead (conditional) metas,
* undo recovery, as long as it stays within the recoverable history.

Out-of-scope constructs (plugin metas/macros, system overrides,
retro-case/retro-currency, key combinations, engine commands, the
repeat/retrospective macros and keyboard/engine commands) are detected on the
branch that reaches them instead of being executed.

The check drives the unmodified, isolated native
:class:`~plover.translation.Translator` and :class:`~plover.formatting.Formatter`
classes: no keyboard is connected, no external operation is ever performed and
the dictionaries are never modified.
"""

from plover.audit._explore import AuditBudget, AuditResult, audit_stacks
from plover.audit._pipeline import (
    AuditScopeError,
    FormattingSettings,
    StackSpec,
    load_stack,
)

__all__ = (
    "AuditBudget",
    "AuditResult",
    "AuditScopeError",
    "FormattingSettings",
    "StackSpec",
    "audit_stacks",
    "load_stack",
)
