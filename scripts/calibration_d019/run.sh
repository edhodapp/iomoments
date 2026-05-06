#!/usr/bin/env bash
# D019 calibration harness.
#
# Drives three fio workload classes (A: well-behaved, B: heavy-
# tailed, C: bimodal) against a target block device and runs
# iomoments in lockstep with --json + --dump-raw-samples for each
# rep. Captures iomoments JSON output, raw-latency dump, fio JSON,
# and instance metadata into a per-run output directory.
#
# Per the D019 design, this script is the math layer's empirical
# extension into real-hardware territory: each fio class is
# constructed to produce a known distributional shape, and the
# raw-latency dump is the ground truth iomoments' verdict is
# checked against in offline analysis.
#
# Usage (root):
#
#     IOMOMENTS_DEVICE=/dev/nvme1n1 ./run.sh
#
# Required env:
#   IOMOMENTS_DEVICE  Block device fio drives (e.g. /dev/nvme1n1).
#                     Must be a raw block device (no filesystem),
#                     otherwise the bandwidth + queue behaviour we
#                     calibrate against is filtered through ext4 /
#                     xfs and the comparison stops being honest.
#
# Optional env:
#   IOMOMENTS_BIN     Path to iomoments binary (default: $PWD/build/iomoments).
#   FIO_BIN           Path to fio (default: /usr/bin/fio).
#   OUT_DIR           Output root (default: $PWD/scripts/calibration_d019/out).
#   REPS              Reps per class (default: 3).
#   CLASSES           Space-separated class letters (default: "A B C").
#   IOMOMENTS_DURATION  iomoments run length in seconds (default: 100,
#                     covers fio's 90 s + 5 s warmup + safety margin).

set -euo pipefail

: "${IOMOMENTS_DEVICE:?IOMOMENTS_DEVICE must be set (e.g. /dev/nvme1n1)}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

IOMOMENTS_BIN="${IOMOMENTS_BIN:-${REPO_ROOT}/build/iomoments}"
FIO_BIN="${FIO_BIN:-/usr/bin/fio}"
OUT_DIR="${OUT_DIR:-${SCRIPT_DIR}/out}"
REPS="${REPS:-3}"
CLASSES="${CLASSES:-A B C}"
IOMOMENTS_DURATION="${IOMOMENTS_DURATION:-100}"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: must run as root (BPF load needs CAP_BPF + CAP_PERFMON)" >&2
    exit 2
fi
if [[ ! -x "${IOMOMENTS_BIN}" ]]; then
    echo "ERROR: ${IOMOMENTS_BIN} not found / not executable" >&2
    echo "       (run \`make iomoments-build\` first)" >&2
    exit 2
fi
if [[ ! -x "${FIO_BIN}" ]]; then
    echo "ERROR: ${FIO_BIN} not found" >&2
    echo "       (try \`apt install fio\`)" >&2
    exit 2
fi
if [[ ! -b "${IOMOMENTS_DEVICE}" ]]; then
    echo "ERROR: ${IOMOMENTS_DEVICE} is not a block device" >&2
    exit 2
fi

# Capture host metadata once per run; same for every class/rep.
META_GLOBAL=$(mktemp)
trap 'rm -f "${META_GLOBAL}"' EXIT

{
    echo "captured_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "kernel=$(uname -r)"
    echo "arch=$(uname -m)"
    echo "fio_version=$(${FIO_BIN} --version)"
    echo "iomoments_sha=$(cd "${REPO_ROOT}" && git rev-parse HEAD)"
    echo "iomoments_device=${IOMOMENTS_DEVICE}"
    echo "iomoments_duration_s=${IOMOMENTS_DURATION}"
    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        echo "distro=${PRETTY_NAME:-unknown}"
    fi
    # Best-effort EC2 instance metadata (IMDSv2 token-required).
    if command -v curl &>/dev/null; then
        token=$(curl -fsS --max-time 1 -X PUT \
            -H "X-aws-ec2-metadata-token-ttl-seconds: 60" \
            http://169.254.169.254/latest/api/token 2>/dev/null || true)
        if [[ -n "${token}" ]]; then
            for f in instance-id instance-type placement/availability-zone; do
                v=$(curl -fsS --max-time 1 \
                    -H "X-aws-ec2-metadata-token: ${token}" \
                    "http://169.254.169.254/latest/meta-data/${f}" \
                    2>/dev/null || true)
                echo "ec2_${f//\//_}=${v}"
            done
        fi
    fi
} > "${META_GLOBAL}"

echo "iomoments calibration_d019 harness"
echo "  device       : ${IOMOMENTS_DEVICE}"
echo "  classes      : ${CLASSES}"
echo "  reps/class   : ${REPS}"
echo "  output       : ${OUT_DIR}"
echo "  iomoments    : ${IOMOMENTS_BIN}"
echo "  fio          : ${FIO_BIN} ($(${FIO_BIN} --version))"
echo

mkdir -p "${OUT_DIR}"

run_one() {
    local class="$1"
    local rep="$2"
    local rep_dir="${OUT_DIR}/${class}/${rep}"
    local fio_job="${SCRIPT_DIR}/class_${class,,}.fio"
    if [[ ! -r "${fio_job}" ]]; then
        echo "ERROR: class ${class} fio job ${fio_job} not found" >&2
        return 1
    fi
    mkdir -p "${rep_dir}"
    cp "${META_GLOBAL}" "${rep_dir}/meta.txt"
    {
        echo "class=${class}"
        echo "rep=${rep}"
        echo "started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >> "${rep_dir}/meta.txt"

    echo "  [${class}/${rep}] starting iomoments..."
    # Run iomoments in the background. --duration covers fio's
    # runtime (90s) + 5s pre-warmup + 5s safety margin.
    "${IOMOMENTS_BIN}" \
        --duration="${IOMOMENTS_DURATION}" \
        --window=100 \
        --json \
        --dump-raw-samples="${rep_dir}/raw.bin" \
        > "${rep_dir}/iomoments.json" \
        2> "${rep_dir}/iomoments.stderr" &
    local iom_pid=$!

    # Pre-warmup: let iomoments attach BPF, allocate ringbuf, and
    # be ready to capture before fio fires. 5 s is conservative;
    # attach is typically <1 s.
    echo "  [${class}/${rep}] iomoments pid ${iom_pid}; warming up 5 s..."
    sleep 5

    echo "  [${class}/${rep}] running fio..."
    IOMOMENTS_DEVICE="${IOMOMENTS_DEVICE}" \
        "${FIO_BIN}" "${fio_job}" \
            --output="${rep_dir}/fio.json" \
            --output-format=json \
            > "${rep_dir}/fio.stdout" \
            2> "${rep_dir}/fio.stderr"

    echo "  [${class}/${rep}] fio done; waiting for iomoments..."
    wait "${iom_pid}"
    echo "  [${class}/${rep}] complete."
    echo "ended_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "${rep_dir}/meta.txt"
    echo "raw_bytes=$(stat -c %s "${rep_dir}/raw.bin")" >> "${rep_dir}/meta.txt"
}

for class in ${CLASSES}; do
    for ((rep = 1; rep <= REPS; rep++)); do
        run_one "${class}" "${rep}"
    done
done

echo
echo "All runs complete. Output tree:"
find "${OUT_DIR}" -mindepth 2 -maxdepth 3 -type d | sort | sed 's/^/  /'
echo
echo "Inspect with:"
echo "  jq -r '.verdict.overall' ${OUT_DIR}/A/1/iomoments.json"
echo "  python3 -c 'import numpy as np; print(np.fromfile(\"${OUT_DIR}/A/1/raw.bin\", dtype=\"<u8\").size)'"
