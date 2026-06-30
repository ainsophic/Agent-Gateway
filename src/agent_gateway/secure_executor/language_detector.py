"""
secure_executor/language_detector.py — Detect programming language from code.

Priority:
  1. Shebang line (first line, unambiguous)
  2. Syntactic heuristics (regex patterns)
  3. Default: Python (safest sandbox default)

Supported languages: Python, Bash, JavaScript.
"""
from __future__ import annotations

import re

from agent_gateway.types import Language


_SHEBANGS: list[tuple[str, Language]] = [
    ("#!/usr/bin/env python3",    Language.PYTHON),
    ("#!/usr/bin/env python",     Language.PYTHON),
    ("#!/usr/bin/python3",        Language.PYTHON),
    ("#!/usr/bin/python",         Language.PYTHON),
    ("#!/usr/bin/env node",       Language.JAVASCRIPT),
    ("#!/usr/bin/node",           Language.JAVASCRIPT),
    ("#!/usr/bin/env bash",       Language.BASH),
    ("#!/bin/bash",               Language.BASH),
    ("#!/bin/sh",                 Language.BASH),
]

# Each pattern is checked against the full code string with MULTILINE flag.
# Ordered from most distinctive to least.
_HEURISTICS: list[tuple[re.Pattern[str], Language]] = [
    (
        re.compile(
            r'\bdef \w+\s*\(|^from \w+ import |^import \w+|'
            r'print\s*\(|if __name__\s*==',
            re.MULTILINE,
        ),
        Language.PYTHON,
    ),
    (
        re.compile(
            r'\bconsole\.(log|error|warn)\b|'
            r'\brequire\s*\(|'
            r'\bconst \w+\s*=|\blet \w+\s*=|\bvar \w+\s*=',
            re.MULTILINE,
        ),
        Language.JAVASCRIPT,
    ),
    (
        re.compile(
            r'^\s*(echo|chmod|grep|sed|awk|curl|wget|export|source|cd|ls|cat)\b|'
            r'\$[A-Z_][A-Z0-9_]*|\$\{|\$\(',
            re.MULTILINE,
        ),
        Language.BASH,
    ),
]


def detect_language(code: str) -> Language:
    """
    Returns the detected Language enum value.

    Examples:
      detect_language("def foo(): pass")       → Language.PYTHON
      detect_language("#!/bin/bash\\necho hi") → Language.BASH
      detect_language("console.log('hi')")     → Language.JAVASCRIPT
      detect_language("42")                    → Language.PYTHON  (default)
    """
    if not code.strip():
        return Language.PYTHON

    first_line = code.strip().split("\n")[0].rstrip()

    # 1. Shebang — unambiguous if present
    for shebang, lang in _SHEBANGS:
        if first_line.startswith(shebang):
            return lang

    # 2. Heuristics — first match wins
    for pattern, lang in _HEURISTICS:
        if pattern.search(code):
            return lang

    # 3. Default
    return Language.PYTHON
