# cppcheck project-level suppressions

`tooling/cppcheck.suppress` is the project-level suppressions file
referenced by the `make lint-c` invocation.

**Inline suppressions are strongly preferred** (DECISIONS.md D008,
principle 5). Use the file only for findings whose wrongness is
structural across the whole codebase — not for per-site exceptions.

## Format

Newline-separated cppcheck entries. **No comments** — cppcheck
parses `#` lines as empty suppression IDs and errors out. Each
entry is one of:

- `<id>` — suppress globally
- `<id>:<file>` — suppress in a specific file
- `<id>:<file>:<line>` — suppress at a specific line

## Rule of engagement

Every entry added to the file must be justified in a companion
commit message explaining:

1. **Why** the rule is wrong for this codebase.
2. **What change** would make the suppression unnecessary.

If the rule is only wrong in one place, use `// cppcheck-suppress <id>`
inline at the call site instead, with the same two-point rationale
as a C comment on the line above.

## Status

Empty today. No cppcheck findings against `src/hello_world.c` require
global suppressions. As real source lands, entries added here should
be rare; expect most suppressions to be inline.
