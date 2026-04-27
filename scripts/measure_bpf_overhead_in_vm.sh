#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ed Hodapp <ed@hodapp.com>
#
# Run scripts/measure_bpf_overhead.sh inside a vmtest guest. Lets us
# measure on kernels other than the host's — particularly useful for
# the k=4 variant which the host (6.17) rejects but every kernel in
# our supported range (5.15-6.12) accepts.
#
# What this does (host side):
#   - Composes a setup-and-run script for the guest.
#   - Invokes vmtest with --kernel <KERNEL_IMAGE> and the script as
#     the command. vmtest mounts the host's / via 9p (read-only by
#     default, writable scratch on tmpfs at /tmp /run /mnt /dev/shm).
#
# What runs inside the guest:
#   - truncate + losetup creates a tmpfs-backed loopback block
#     device. The loop driver IS a blk_mq driver, so dd direct-I/O
#     against /dev/loopN fires the same block_rq_issue /
#     block_rq_complete tracepoints we attach in production. The
#     fact that the backing storage is RAM rather than disk doesn't
#     change the measured BPF cost — we're measuring the BPF
#     program, not the storage stack below it.
#   - The existing measure_bpf_overhead.sh script with
#     OVERHEAD_TARGET=/dev/loopN.
#   - Cleanup: detach the loop device, unlink the backing file.
#
# Usage:
#   scripts/measure_bpf_overhead_in_vm.sh ~/kernel-images/vmlinuz-v6.12

set -euo pipefail

KERNEL_IMAGE=${1:-$HOME/kernel-images/vmlinuz-v6.12}
if [ ! -f "$KERNEL_IMAGE" ]; then
	echo "ERROR: KERNEL_IMAGE $KERNEL_IMAGE not found." >&2
	echo "  Build via ~/vmtest-build/scripts/build_kernel.sh v<ver> fedora38" >&2
	echo "  then: cp ~/vmtest-build/bzImage-v<ver>-fedora38 ~/kernel-images/vmlinuz-v<ver>" >&2
	exit 1
fi

# vmtest wraps QEMU. Use the same probe order the Makefile uses.
VMTEST=$(command -v vmtest || true)
if [ -z "$VMTEST" ] && [ -x "$HOME/.cargo/bin/vmtest" ]; then
	VMTEST="$HOME/.cargo/bin/vmtest"
fi
if [ -z "$VMTEST" ]; then
	echo "ERROR: vmtest not on PATH (cargo install vmtest)" >&2
	exit 1
fi

# Repo root: the script lives in scripts/, so the absolute path of
# the parent is what the in-guest script will reference for invoking
# measure_bpf_overhead.sh and the .bpf.o files.
REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)

# Pre-flight: the BPF objects must exist before we hand off to the VM.
for obj in "$REPO_ROOT/build/iomoments.bpf.o" "$REPO_ROOT/build/iomoments-k3.bpf.o"; do
	if [ ! -f "$obj" ]; then
		echo "ERROR: $obj missing — run 'make bpf-compile' first." >&2
		exit 1
	fi
done

# Compose the in-guest setup-and-run script. Putting it in /tmp on
# the host makes it visible inside the guest via the 9p mount.
GUEST_SCRIPT=$(mktemp /tmp/iomoments-vm-runner.XXXXXX.sh)
trap 'rm -f "$GUEST_SCRIPT"' EXIT

cat >"$GUEST_SCRIPT" <<GUEST
#!/usr/bin/env bash
# Inside the vmtest guest. Set up a tmpfs-backed loopback device
# and run the underlying measurement script against it.
set -euo pipefail

# vmtest's init mounts /sys but not /sys/fs/bpf. The measurement
# script pins programs there via bpftool, so we need bpffs mounted
# before that. Idempotent: skip if already mounted.
if ! mountpoint -q /sys/fs/bpf; then
	mkdir -p /sys/fs/bpf
	mount -t bpf bpf /sys/fs/bpf
fi

DISK_IMG=/mnt/iomoments-disk.img
truncate -s 256M "\$DISK_IMG"

# losetup -f --show finds a free loop device and prints the path.
LOOPDEV=\$(losetup -f --show "\$DISK_IMG")

cleanup_inside() {
	losetup -d "\$LOOPDEV" 2>/dev/null || true
	rm -f "\$DISK_IMG"
}
trap cleanup_inside EXIT

# Smoke-check the loop device is a real block device. If it isn't,
# the loop driver isn't loaded (rare on stripped guests) and our
# measurement would silently fall through to whatever path the
# fallback exercises. Refuse rather than mislead.
if [ ! -b "\$LOOPDEV" ]; then
	echo "ERROR: \$LOOPDEV is not a block device — loop driver missing?" >&2
	exit 1
fi

cd "$REPO_ROOT"
OVERHEAD_TARGET="\$LOOPDEV" \\
	IOMOMENTS_FORCE_VARIANT="${IOMOMENTS_FORCE_VARIANT:-}" \\
	bash scripts/measure_bpf_overhead.sh
GUEST

chmod +x "$GUEST_SCRIPT"

echo "Running measurement inside vmtest guest:"
echo "  kernel: $KERNEL_IMAGE"
echo "  guest script: $GUEST_SCRIPT"
echo
"$VMTEST" --kernel "$KERNEL_IMAGE" -- bash "$GUEST_SCRIPT"
