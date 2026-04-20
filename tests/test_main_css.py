#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Regression test: _CSS in main.py must be pure ASCII bytes.

GTK's CssProvider.load_from_data() expects a bytes object, so _CSS is
declared as b\"\"\"...\"\"\".  Python raises SyntaxError at parse time if any
non-ASCII character (e.g. an em dash copied from a doc) appears inside a
bytes literal.  This test catches that before it reaches the user's shell.
"""
from pathlib import Path
import ast
import re


_MAIN_PY = Path(__file__).parent.parent / "app" / "main.py"


def _extract_css_literal(source: str) -> bytes | None:
    """Return the bytes value of the _CSS = b\"\"\"...\"\"\" assignment, or None."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "_CSS"
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, bytes)
        ):
            return node.value.value
    return None


def test_css_is_ascii():
    """_CSS bytes literal must contain only ASCII characters (0x00–0x7F)."""
    source = _MAIN_PY.read_text(encoding="utf-8")
    css = _extract_css_literal(source)
    assert css is not None, "_CSS bytes literal not found in main.py"
    non_ascii = [(i, b) for i, b in enumerate(css) if b > 127]
    assert not non_ascii, (
        f"_CSS contains {len(non_ascii)} non-ASCII byte(s); "
        f"first at offset {non_ascii[0][0]}: 0x{non_ascii[0][1]:02x}. "
        "Replace non-ASCII characters (e.g. em dashes) with ASCII equivalents."
    )


def test_main_py_parses():
    """main.py must be parseable by Python (no SyntaxError)."""
    source = _MAIN_PY.read_bytes()
    # compile() raises SyntaxError on invalid byte literals before we even exec
    compile(source, str(_MAIN_PY), "exec")
