"""Parse a ref string into (path, symbol) components.

Accepted shapes (derived from ontology usage on 2026-04-23):
- ``src/iomoments.c:pebay_update``              — C function.
- ``src/iomoments.bpf.c:emit_moments``          — BPF C function.
- ``tests/test_pebay_ref.py::test_round_trip``  — pytest-style.
- ``tooling/src/mod/submod.py:my_func``         — colon-style Python.
- ``src/iomoments.h``                           — file-only (no symbol).

The parser accepts both ``:`` and ``::`` as the file/symbol
separator. Pytest-style ``::`` is the dominant convention for
Python test refs; ``:`` matches the kernel-adjacent ``file:symbol``
shape for C.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedRef:
    """A parsed reference into the working tree.

    ``raw`` preserves the original string for error messages so the
    auditor can print "ref ``foo.py:bar``" verbatim rather than a
    re-joined approximation.
    """

    raw: str
    path: str
    symbol: str | None


def parse_ref(ref: str) -> ParsedRef:
    """Parse ``path[:symbol]`` or ``path[::symbol]`` into ParsedRef.

    Empty strings and whitespace-only inputs raise ValueError — the
    caller should filter those out rather than surfacing them as
    parsed refs.
    """
    cleaned = ref.strip()
    if not cleaned:
        raise ValueError("ref is empty or whitespace-only")

    # Prefer the pytest-style ``::`` first so ``a::b:c`` parses as
    # (path="a", symbol="b:c") rather than ("a:", symbol=":b:c").
    if "::" in cleaned:
        path, symbol = cleaned.split("::", 1)
        return _make_ref(ref, path, symbol, separator="::")

    if ":" in cleaned:
        path, symbol = cleaned.split(":", 1)
        return _make_ref(ref, path, symbol, separator=":")

    # No symbol separator — file-only ref. Valid for "this file
    # implements the constraint" refs where no single symbol maps.
    return ParsedRef(raw=ref, path=cleaned, symbol=None)


def _make_ref(
    raw: str, path: str, symbol: str, separator: str,
) -> ParsedRef:
    """Validate path + symbol were both non-empty after the separator.

    Empty strings on either side indicate a typo
    (``foo.py::`` or ``::test_x``) the author expected to mean
    something concrete — surface it rather than accept the
    half-formed ref.
    """
    path_s = path.strip()
    symbol_s = symbol.strip()
    if not path_s:
        raise ValueError(
            f"ref '{raw}': empty path before '{separator}'"
        )
    if not symbol_s:
        raise ValueError(
            f"ref '{raw}': empty symbol after '{separator}'"
        )
    return ParsedRef(raw=raw, path=path_s, symbol=symbol_s)
