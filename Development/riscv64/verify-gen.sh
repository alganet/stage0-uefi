#!/bin/sh
# SPDX-FileCopyrightText: 2026 Alexandre Gomes Gaigalas <alganet@gmail.com>
# SPDX-License-Identifier: MIT
#
# Guard against silent drift between the riscv64 generators in this directory
# and the committed bootstrap artifacts they produce. For each generated tool,
# re-run its generator and compare the result against the committed file by
# non-comment token (hex bytes plus hex2 label/ref directives), ignoring
# comments and whitespace -- the committed files carry richer hand-written
# documentation comments, but their machine content must match the generator.
#
# Hand-maintained artifacts (intentionally NOT checked here):
#   riscv64/hex0.hex0          -- no generator exists (bootstrap seed root).
#   riscv64/kaem-optional.hex0 -- hand-tuned after generation; its generator
#                                 (gen-kaem-optional-rv64.py) is a historical
#                                 reference that has since diverged and does
#                                 NOT reproduce the committed binary. The
#                                 committed file is authoritative.
#
# Exit non-zero on any drift so CI / `make verify-riscv64-gen` fails loudly.
set -eu

cd "$(dirname "$0")"
ART=../../riscv64

# generator  committed-artifact
PAIRS="
gen-M0-rv64.py:$ART/M0.hex2
gen-catm-rv64.py:$ART/catm.hex2
gen-hex1-rv64.py:$ART/hex1.hex0
gen-hex2-rv64.py:$ART/hex2.hex1
"

tokens () {
	# Drop comments (# to EOL), collapse all whitespace to one token per line,
	# drop blanks. Leaves hex byte pairs and hex2 label/ref directives.
	sed 's/#.*//' "$1" | tr -s '[:space:]' '\n' | grep -v '^$' || true
}

rc=0
for pair in $PAIRS; do
	gen="${pair%%:*}"
	art="${pair#*:}"
	if ! python3 "$gen" > /tmp/.verifygen.out 2>/tmp/.verifygen.err; then
		echo "FAIL: $gen did not run:"; cat /tmp/.verifygen.err; rc=1; continue
	fi
	tokens /tmp/.verifygen.out > /tmp/.verifygen.gen
	tokens "$art" > /tmp/.verifygen.art
	if diff -u /tmp/.verifygen.art /tmp/.verifygen.gen > /tmp/.verifygen.diff; then
		echo "ok:   $gen == $art"
	else
		echo "DRIFT: $gen no longer reproduces $art (non-comment tokens differ):"
		head -20 /tmp/.verifygen.diff
		rc=1
	fi
done

rm -f /tmp/.verifygen.out /tmp/.verifygen.err /tmp/.verifygen.gen /tmp/.verifygen.art /tmp/.verifygen.diff
if [ "$rc" -ne 0 ]; then
	echo "riscv64 generator verification FAILED" >&2
else
	echo "riscv64 generator verification passed (hex0.hex0 and kaem-optional.hex0 are hand-maintained, not checked)"
fi
exit "$rc"
