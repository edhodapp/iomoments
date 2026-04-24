"""Resolve a ParsedRef against the working tree.

For each ref we answer three questions in order:
1. Does the file exist at all?
2. If a symbol was named, does the file contain a plausible
   definition of that symbol?
3. If yes, where (line number) so the auditor can cite it?

Symbol-matching is grep-based on purpose: the full solution (ctags
for C, ast for Python) is more precise but imports toolchain
dependencies we don't need today. Grep over a handful of anchor
patterns catches the common case and reports false-misses rather
than false-hits, which is the right failure mode for an audit
tool — a gap flagged wrongly annoys the author; a gap silently
masked misleads the auditor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from audit_ontology.parser import ParsedRef


class Resolution(str, Enum):
    """Outcome of resolving a single ref."""

    OK = "ok"
    FILE_MISSING = "file_missing"
    SYMBOL_MISSING = "symbol_missing"


@dataclass(frozen=True)
class ResolvedRef:
    """The resolution result for one ref."""

    ref: ParsedRef
    resolution: Resolution
    line: int | None = None
    notes: str = ""


# Per-extension symbol-definition anchor patterns. One match is
# enough — the resolver isn't trying to enumerate every definition,
# just prove that at least one exists.
_PYTHON_PATTERNS = (
    re.compile(r"^\s*def\s+{name}\s*\("),
    re.compile(r"^\s*async\s+def\s+{name}\s*\("),
    re.compile(r"^\s*class\s+{name}\s*[\(:]"),
    re.compile(r"^\s*{name}\s*="),
)

_C_PATTERNS = (
    # Function definition: `type name(...)` at column 0 or after
    # storage-class keywords. Coarse by design — the common
    # false-positive is a function *call* whose line happens to
    # start with `type return ...`; accepted tradeoff vs the cost
    # of a proper parse.
    re.compile(r"^\s*(?:static\s+|extern\s+|inline\s+)*"
               r"[A-Za-z_][\w\s\*]*\s+{name}\s*\("),
    re.compile(r"^\s*{name}\s*\("),  # macro-like invocations
    re.compile(r"^\s*{name}\s*="),
    # libbpf BPF_PROG / BPF_KPROBE wrapper macros expand to a
    # function whose C-level name ends up inside the macro's
    # argument list: `int BPF_PROG(my_func, ...)`. Match the name
    # as the first arg of a macro call (comma or closing paren
    # after, whitespace tolerated).
    re.compile(r"\bBPF_(?:PROG|KPROBE|KRETPROBE|TP)\s*\(\s*{name}\s*[,)]"),
)

_SHELL_PATTERNS = (
    re.compile(r"^\s*(?:function\s+)?{name}\s*\(\s*\)"),
    re.compile(r"^\s*{name}\s*="),
)


def _patterns_for(path: Path) -> tuple[re.Pattern[str], ...]:
    suffix = path.suffix
    if suffix == ".py":
        return _PYTHON_PATTERNS
    if suffix in (".c", ".h"):
        return _C_PATTERNS
    if suffix == ".sh":
        return _SHELL_PATTERNS
    # Unknown file extension — fall back to a broad substring search
    # so markdown / yaml / config refs still resolve without a gap.
    return (re.compile(r"\b{name}\b"),)


def _compiled(
    patterns: tuple[re.Pattern[str], ...], name: str,
) -> list[re.Pattern[str]]:
    """Substitute ``{name}`` placeholder into each pattern."""
    escaped = re.escape(name)
    return [
        re.compile(p.pattern.format(name=escaped))
        for p in patterns
    ]


def _find_symbol(
    text: str, patterns: list[re.Pattern[str]],
) -> int | None:
    """Return the first line number matching any pattern, or None."""
    for line_idx, line in enumerate(text.splitlines(), start=1):
        for pat in patterns:
            if pat.search(line):
                return line_idx
    return None


def _grep_symbol(
    ref: ParsedRef, abs_path: Path, symbol: str,
) -> ResolvedRef:
    """Read the file and grep for a symbol definition."""
    patterns = _compiled(_patterns_for(abs_path), symbol)
    try:
        text = abs_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ResolvedRef(
            ref=ref,
            resolution=Resolution.SYMBOL_MISSING,
            notes=f"{ref.path} not UTF-8; cannot grep for {symbol}",
        )
    line = _find_symbol(text, patterns)
    if line is not None:
        return ResolvedRef(ref=ref, resolution=Resolution.OK, line=line)
    return ResolvedRef(
        ref=ref,
        resolution=Resolution.SYMBOL_MISSING,
        notes=f"no definition-like line for '{symbol}' in {ref.path}",
    )


def resolve_ref(ref: ParsedRef, repo_root: Path) -> ResolvedRef:
    """Resolve a ref against ``repo_root``, returning a ResolvedRef.

    Never raises — all failures land in the Resolution enum so the
    caller can aggregate gaps into a report rather than aborting on
    the first missing file.
    """
    abs_path = repo_root / ref.path
    if not abs_path.is_file():
        return ResolvedRef(
            ref=ref,
            resolution=Resolution.FILE_MISSING,
            notes=f"{ref.path} not found under {repo_root}",
        )
    if ref.symbol is None:
        return ResolvedRef(ref=ref, resolution=Resolution.OK)
    return _grep_symbol(ref, abs_path, ref.symbol)
