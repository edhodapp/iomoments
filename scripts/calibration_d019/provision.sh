#!/usr/bin/env bash
# scripts/calibration_d019/provision.sh — D019 EC2 calibration run.
#
# Spins up one m5.large running Ubuntu 22.04 LTS HWE with a separate
# 50 GB gp3 EBS data volume, copies the locally-built iomoments
# binary + BPF objects + harness scripts to the instance, runs the
# three fio workload classes × 3 reps each (Class A/B/C; ~16 min
# total), retrieves the per-rep output tree to the laptop, tears
# everything down. Hardcoded teardown via trap EXIT — no IaC.
#
# Cost estimate: ~$0.20 per run (m5.large at ~$0.096/hr × 1 hr +
# ~$0.011 for the 50 GB gp3 + small outbound transfer).
#
# Usage (from repo root):
#
#     ./scripts/calibration_d019/provision.sh
#
# Prerequisites locally:
#   - aws CLI configured with profile "iomoments" (or override via
#     AWS_PROFILE env).
#   - build/iomoments + build/iomoments.bpf.o + build/iomoments-k3.bpf.o
#     present (run `make iomoments-build` first).
#   - jq optional (for post-run inspection).

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
AWS_PROFILE="${AWS_PROFILE:-iomoments}"
AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT_TAG="iomoments-calibration-d019"
INSTANCE_TYPE="m5.large"
ROOT_VOLUME_GB="20"
DATA_VOLUME_GB="50"
# Ubuntu 22.04 LTS (jammy) HWE, kernel 6.8 — same AMI Canonical
# the AWS-tracer probe uses for its 22.04 entry.
AMI_OWNER="099720109477"
AMI_NAME_PATTERN="ubuntu/images/hvm-ssd*/ubuntu-jammy-22.04-amd64-server-*"
SSH_USER="ubuntu"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
RESULTS_DIR="${REPO_ROOT}/scripts/calibration_d019/out-ec2"

AWS="${REPO_ROOT}/.venv/bin/aws --profile ${AWS_PROFILE} --region ${AWS_REGION}"

RUN_ID="$(date +%s)-$$"
KEY_NAME=""; KEY_PATH=""; SG_ID=""; INSTANCE_ID=""; PUBLIC_IP=""

# ---------------------------------------------------------------------------
# Teardown — runs on every exit path. Filters by both Project AND Run
# tags so concurrent invocations don't clobber each other.
# ---------------------------------------------------------------------------
teardown() {
    local rc=$?
    echo "" >&2
    echo "[teardown] starting (script exit=${rc})" >&2

    local ids
    ids=$(${AWS} ec2 describe-instances \
        --filters "Name=tag:Project,Values=${PROJECT_TAG}" \
                  "Name=tag:Run,Values=${RUN_ID}" \
                  "Name=instance-state-name,Values=pending,running,stopping,stopped" \
        --query "Reservations[].Instances[].InstanceId" \
        --output text 2>/dev/null || true)
    if [ -n "${ids}" ] && [ "${ids}" != "None" ]; then
        echo "[teardown] terminating instances: ${ids}" >&2
        # shellcheck disable=SC2086
        ${AWS} ec2 terminate-instances --instance-ids ${ids} >/dev/null || true
        # shellcheck disable=SC2086
        ${AWS} ec2 wait instance-terminated --instance-ids ${ids} || true
    fi

    if [ -n "${SG_ID}" ]; then
        echo "[teardown] deleting security group ${SG_ID}" >&2
        ${AWS} ec2 delete-security-group --group-id "${SG_ID}" 2>/dev/null || true
    fi
    if [ -n "${KEY_NAME}" ]; then
        echo "[teardown] deleting key pair ${KEY_NAME}" >&2
        ${AWS} ec2 delete-key-pair --key-name "${KEY_NAME}" 2>/dev/null || true
    fi
    if [ -n "${KEY_PATH}" ] && [ -f "${KEY_PATH}" ]; then
        rm -f "${KEY_PATH}"
    fi

    local survivors
    survivors=$(${AWS} ec2 describe-instances \
        --filters "Name=tag:Project,Values=${PROJECT_TAG}" \
                  "Name=tag:Run,Values=${RUN_ID}" \
                  "Name=instance-state-name,Values=pending,running,stopping,stopped" \
        --query "Reservations[].Instances[].InstanceId" --output text 2>/dev/null || true)
    if [ -n "${survivors}" ] && [ "${survivors}" != "None" ]; then
        echo "[teardown] WARNING: instances still present: ${survivors}" >&2
        echo "[teardown] manual cleanup required:" >&2
        echo "  ${AWS} ec2 terminate-instances --instance-ids ${survivors}" >&2
    else
        echo "[teardown] verified: no surviving instances for Run=${RUN_ID}" >&2
    fi
    exit "${rc}"
}
trap teardown EXIT INT TERM HUP

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
for required in build/iomoments build/iomoments.bpf.o build/iomoments-k3.bpf.o; do
    if [ ! -f "${REPO_ROOT}/${required}" ]; then
        echo "ERROR: ${required} not found." >&2
        echo "       Run 'make iomoments-build' first." >&2
        exit 1
    fi
done

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
KEY_NAME="iomoments-d019-${RUN_ID}"
KEY_PATH="$(mktemp -t iomoments-d019-XXXXXX.pem)"

echo "[setup] creating key pair ${KEY_NAME}" >&2
${AWS} ec2 create-key-pair --key-name "${KEY_NAME}" \
    --tag-specifications "ResourceType=key-pair,Tags=[{Key=Project,Value=${PROJECT_TAG}},{Key=Run,Value=${RUN_ID}}]" \
    --query "KeyMaterial" --output text >"${KEY_PATH}"
chmod 600 "${KEY_PATH}"

MY_IP=$(curl -fsS https://checkip.amazonaws.com)
echo "[setup] my public IP: ${MY_IP}" >&2

echo "[setup] creating security group" >&2
SG_ID=$(${AWS} ec2 create-security-group \
    --group-name "iomoments-d019-${RUN_ID}" \
    --description "iomoments calibration D019 ${RUN_ID}" \
    --tag-specifications "ResourceType=security-group,Tags=[{Key=Project,Value=${PROJECT_TAG}},{Key=Run,Value=${RUN_ID}}]" \
    --query "GroupId" --output text)
${AWS} ec2 authorize-security-group-ingress \
    --group-id "${SG_ID}" --protocol tcp --port 22 --cidr "${MY_IP}/32" >/dev/null
echo "[setup] SG ${SG_ID} (ssh from ${MY_IP}/32)" >&2

# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------
echo "[launch] resolving AMI" >&2
AMI=$(${AWS} ec2 describe-images \
    --owners "${AMI_OWNER}" \
    --filters "Name=name,Values=${AMI_NAME_PATTERN}" \
              "Name=architecture,Values=x86_64" \
              "Name=state,Values=available" \
    --query "sort_by(Images, &CreationDate)[-1].ImageId" --output text)
if [ -z "${AMI}" ] || [ "${AMI}" = "None" ]; then
    echo "[launch] AMI lookup failed" >&2
    exit 1
fi
echo "[launch] AMI=${AMI}" >&2

# /dev/sda1 = root (Ubuntu's default device name), /dev/sdb = data.
# On NVMe-based instance types (m5.*) Linux exposes them as
# /dev/nvme0n1 / /dev/nvme1n1 regardless of the request-side names.
INSTANCE_ID=$(${AWS} ec2 run-instances \
    --image-id "${AMI}" --instance-type "${INSTANCE_TYPE}" \
    --key-name "${KEY_NAME}" --security-group-ids "${SG_ID}" \
    --block-device-mappings \
        "DeviceName=/dev/sda1,Ebs={VolumeSize=${ROOT_VOLUME_GB},VolumeType=gp3,DeleteOnTermination=true}" \
        "DeviceName=/dev/sdb,Ebs={VolumeSize=${DATA_VOLUME_GB},VolumeType=gp3,DeleteOnTermination=true}" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Project,Value=${PROJECT_TAG}},{Key=Run,Value=${RUN_ID}}]" \
                          "ResourceType=volume,Tags=[{Key=Project,Value=${PROJECT_TAG}},{Key=Run,Value=${RUN_ID}}]" \
    --query "Instances[0].InstanceId" --output text)
echo "[launch] launched ${INSTANCE_ID}" >&2

${AWS} ec2 wait instance-running --instance-ids "${INSTANCE_ID}"
PUBLIC_IP=$(${AWS} ec2 describe-instances --instance-ids "${INSTANCE_ID}" \
    --query "Reservations[0].Instances[0].PublicIpAddress" --output text)
echo "[launch] public IP ${PUBLIC_IP}" >&2

# ---------------------------------------------------------------------------
# SSH wait + on-instance setup
# ---------------------------------------------------------------------------
SSH_OPTS=(-i "${KEY_PATH}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
          -o ConnectTimeout=5 -o ServerAliveInterval=30 -o LogLevel=ERROR)

echo "[ssh] waiting for ssh..." >&2
ready=0
for _ in $(seq 1 60); do
    if ssh "${SSH_OPTS[@]}" "${SSH_USER}@${PUBLIC_IP}" "echo ok" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 5
done
if [ "${ready}" -eq 0 ]; then
    echo "[ssh] never came up" >&2
    exit 1
fi

echo "[ssh] installing prerequisites (fio, libbpf1)..." >&2
ssh "${SSH_OPTS[@]}" "${SSH_USER}@${PUBLIC_IP}" '
    set -e
    sudo apt-get update -y >/tmp/apt.log 2>&1
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
        fio libbpf1 libelf1 zlib1g >>/tmp/apt.log 2>&1
    echo "fio: $(fio --version)"
    echo "kernel: $(uname -r)"
    # Wait for the data volume to appear (NVMe attach can be a few s post-boot).
    for _ in $(seq 1 20); do
        if [ -b /dev/nvme1n1 ]; then break; fi
        sleep 1
    done
    if [ ! -b /dev/nvme1n1 ]; then
        echo "ERROR: /dev/nvme1n1 not present" >&2
        ls -la /dev/nvme* >&2 || true
        exit 1
    fi
    echo "data device: /dev/nvme1n1 ($(sudo blockdev --getsize64 /dev/nvme1n1) bytes)"
'

echo "[scp] uploading iomoments binary + BPF objects + harness..." >&2
TMP_REMOTE="/home/${SSH_USER}/iomoments-d019"
ssh "${SSH_OPTS[@]}" "${SSH_USER}@${PUBLIC_IP}" "mkdir -p ${TMP_REMOTE}/build ${TMP_REMOTE}/scripts/calibration_d019"
scp -i "${KEY_PATH}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR \
    "${REPO_ROOT}/build/iomoments" \
    "${REPO_ROOT}/build/iomoments.bpf.o" \
    "${REPO_ROOT}/build/iomoments-k3.bpf.o" \
    "${SSH_USER}@${PUBLIC_IP}:${TMP_REMOTE}/build/" >/dev/null
scp -i "${KEY_PATH}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR \
    "${SCRIPT_DIR}/class_a.fio" "${SCRIPT_DIR}/class_b.fio" \
    "${SCRIPT_DIR}/class_c.fio" "${SCRIPT_DIR}/run.sh" \
    "${SCRIPT_DIR}/README.md" \
    "${SSH_USER}@${PUBLIC_IP}:${TMP_REMOTE}/scripts/calibration_d019/" >/dev/null
ssh "${SSH_OPTS[@]}" "${SSH_USER}@${PUBLIC_IP}" \
    "chmod +x ${TMP_REMOTE}/scripts/calibration_d019/run.sh \
              ${TMP_REMOTE}/build/iomoments"

# ---------------------------------------------------------------------------
# Run the harness on the instance
# ---------------------------------------------------------------------------
echo "[run] launching harness on EC2 (will take ~16 min)..." >&2
ssh "${SSH_OPTS[@]}" "${SSH_USER}@${PUBLIC_IP}" "
    cd ${TMP_REMOTE}
    sudo IOMOMENTS_DEVICE=/dev/nvme1n1 \
         ./scripts/calibration_d019/run.sh \
         2>&1 | tee /tmp/harness.log
"

# ---------------------------------------------------------------------------
# Retrieve outputs
# ---------------------------------------------------------------------------
mkdir -p "${RESULTS_DIR}"
echo "[retrieve] pulling results to ${RESULTS_DIR}/${RUN_ID}/" >&2
mkdir -p "${RESULTS_DIR}/${RUN_ID}"
scp -i "${KEY_PATH}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR -r \
    "${SSH_USER}@${PUBLIC_IP}:${TMP_REMOTE}/scripts/calibration_d019/out" \
    "${RESULTS_DIR}/${RUN_ID}/" >/dev/null
scp -i "${KEY_PATH}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR \
    "${SSH_USER}@${PUBLIC_IP}:/tmp/harness.log" \
    "${RESULTS_DIR}/${RUN_ID}/" >/dev/null

echo "" >&2
echo "[done] D019 calibration run ${RUN_ID} retrieved." >&2
echo "       Results: ${RESULTS_DIR}/${RUN_ID}/" >&2
echo "" >&2
echo "Inspect verdicts:" >&2
echo "  for d in ${RESULTS_DIR}/${RUN_ID}/out/*/*/; do" >&2
echo "      echo \"\$d:\"; jq -r '.verdict.overall' \"\$d/iomoments.json\";" >&2
echo "  done" >&2
echo "" >&2
echo "(teardown follows on script exit; instance + EBS + SG + key all cleaned.)" >&2
