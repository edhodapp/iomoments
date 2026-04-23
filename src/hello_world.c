/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Copyright (C) 2026 Ed Hodapp <ed@hodapp.com> */

/*
 * Pipeline-bootstrap sentinel translation unit.
 *
 * Purpose: exercise the four-engine C static-analysis stack (D008) and
 * the clang-format gate on a real file so the tooling is proven working
 * before any load-bearing C lands. Not part of the shipped tool.
 *
 * Removed or replaced once the real userland + BPF sources exist.
 */

int main(void)
{
	return 0;
}
