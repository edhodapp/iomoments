#!/usr/bin/env bash
# scripts/aws_tracer.sh — D008 #46
#
# AWS faithfulness probe for iomoments BPF verifier verdicts.
#
# Launches a t3.small of Ubuntu 22.04 LTS and Amazon Linux 2023, copies the
# locally-built iomoments.bpf.o (k=4) and iomoments-k3.bpf.o (k=3), runs
# `bpftool prog load` for each, and captures the verifier verdict + log.
# Hardcoded teardown via trap EXIT — no IaC, no orchestrator.
#
# Compare results in build/aws-tracer/<distro>/{meta.txt,k4.log,k3.log}
# against local `make bpf-test-vm KERNEL_IMAGE=~/kernel-images/vmlinuz-vX.Y`
# for the closest-matching kernel.

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
AWS_PROFILE="iomoments"
AWS_REGION="us-east-1"
PROJECT_TAG="iomoments-tracer"
INSTANCE_TYPE="t3.small"
RESULTS_DIR="build/aws-tracer"

AWS="$HOME/iomoments/.venv/bin/aws --profile ${AWS_PROFILE} --region ${AWS_REGION}"

# Per-distro: AMI owner | name pattern | key | ssh user | bpftool install cmd
DISTROS=(
  "099720109477|ubuntu/images/hvm-ssd*/ubuntu-jammy-22.04-amd64-server-*|ubuntu-22.04|ubuntu|sudo apt-get update -y && sudo apt-get install -y linux-tools-\$(uname -r) linux-tools-generic"
  "amazon|al2023-ami-2023.*-x86_64|al2023|ec2-user|sudo dnf install -y bpftool"
  "099720109477|ubuntu/images/hvm-ssd*/ubuntu-focal-20.04-amd64-server-*|ubuntu-20.04|ubuntu|sudo apt-get update -y && sudo apt-get install -y linux-tools-\$(uname -r) linux-tools-generic"
)

RUN_ID="$(date +%s)-$$"
KEY_NAME=""; KEY_PATH=""; SG_ID=""

# ---------------------------------------------------------------------------
# Teardown — runs on every exit path. Filters by both Project AND Run tags
# so concurrent invocations don't clobber each other's resources.
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
    ${AWS} ec2 terminate-instances --instance-ids ${ids} >/dev/null || true
    echo "[teardown] waiting for terminate..." >&2
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

  # Final inventory check — scoped to this run's tag pair.
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
# Setup
# ---------------------------------------------------------------------------
setup() {
  KEY_NAME="iomoments-tracer-${RUN_ID}"
  KEY_PATH="$(mktemp -t iomoments-tracer-XXXXXX.pem)"

  echo "[setup] creating key pair ${KEY_NAME}" >&2
  ${AWS} ec2 create-key-pair --key-name "${KEY_NAME}" \
    --tag-specifications "ResourceType=key-pair,Tags=[{Key=Project,Value=${PROJECT_TAG}},{Key=Run,Value=${RUN_ID}}]" \
    --query "KeyMaterial" --output text >"${KEY_PATH}"
  chmod 600 "${KEY_PATH}"

  local my_ip
  my_ip=$(curl -fsS https://checkip.amazonaws.com)
  echo "[setup] my public IP: ${my_ip}" >&2

  echo "[setup] creating security group" >&2
  SG_ID=$(${AWS} ec2 create-security-group \
    --group-name "iomoments-tracer-${RUN_ID}" \
    --description "iomoments BPF tracer probe ${RUN_ID}" \
    --tag-specifications "ResourceType=security-group,Tags=[{Key=Project,Value=${PROJECT_TAG}},{Key=Run,Value=${RUN_ID}}]" \
    --query "GroupId" --output text)
  ${AWS} ec2 authorize-security-group-ingress \
    --group-id "${SG_ID}" --protocol tcp --port 22 --cidr "${my_ip}/32" >/dev/null
  echo "[setup] SG ${SG_ID} (ssh from ${my_ip}/32)" >&2
}

# ---------------------------------------------------------------------------
# Per-distro probe
# ---------------------------------------------------------------------------
ssh_opts=(-i "" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
          -o ConnectTimeout=5 -o LogLevel=ERROR)

probe_distro() {
  local owner="$1" name_pattern="$2" distro_key="$3" ssh_user="$4" install_cmd="$5"
  local out_dir="${RESULTS_DIR}/${distro_key}"
  mkdir -p "${out_dir}"

  echo "" >&2; echo "=== ${distro_key} ===" >&2

  echo "[${distro_key}] resolving AMI" >&2
  local ami
  ami=$(${AWS} ec2 describe-images \
    --owners "${owner}" \
    --filters "Name=name,Values=${name_pattern}" \
              "Name=architecture,Values=x86_64" \
              "Name=state,Values=available" \
    --query "sort_by(Images, &CreationDate)[-1].ImageId" --output text)
  if [ -z "${ami}" ] || [ "${ami}" = "None" ]; then
    echo "[${distro_key}] AMI lookup failed" >&2
    return 1
  fi
  echo "[${distro_key}] AMI=${ami}" >&2

  local instance_id
  instance_id=$(${AWS} ec2 run-instances \
    --image-id "${ami}" --instance-type "${INSTANCE_TYPE}" \
    --key-name "${KEY_NAME}" --security-group-ids "${SG_ID}" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Project,Value=${PROJECT_TAG}},{Key=Run,Value=${RUN_ID}},{Key=Distro,Value=${distro_key}}]" \
    --query "Instances[0].InstanceId" --output text)
  echo "[${distro_key}] launched ${instance_id}" >&2

  ${AWS} ec2 wait instance-running --instance-ids "${instance_id}"
  local public_ip
  public_ip=$(${AWS} ec2 describe-instances --instance-ids "${instance_id}" \
    --query "Reservations[0].Instances[0].PublicIpAddress" --output text)
  echo "[${distro_key}] public IP ${public_ip}" >&2

  ssh_opts[1]="${KEY_PATH}"
  echo "[${distro_key}] waiting for SSH..." >&2
  local ssh_ready=0 i
  for i in $(seq 1 60); do
    if ssh "${ssh_opts[@]}" "${ssh_user}@${public_ip}" "echo ok" >/dev/null 2>&1; then
      ssh_ready=1
      break
    fi
    sleep 5
  done
  if [ "${ssh_ready}" -eq 0 ]; then
    echo "[${distro_key}] SSH never came up" >&2
    return 1
  fi

  if ! ssh "${ssh_opts[@]}" "${ssh_user}@${public_ip}" \
       "uname -r; uname -a; cat /etc/os-release | grep -E '^(NAME|VERSION_ID|PRETTY_NAME)='" \
       >"${out_dir}/meta.txt"; then
    echo "[${distro_key}] meta capture failed" >&2
  fi
  echo "AMI=${ami}" >>"${out_dir}/meta.txt"
  echo "InstanceId=${instance_id}" >>"${out_dir}/meta.txt"

  echo "[${distro_key}] installing bpftool..." >&2
  ssh "${ssh_opts[@]}" "${ssh_user}@${public_ip}" \
      "${install_cmd} >/tmp/install.log 2>&1; \
       which bpftool 2>/dev/null || sudo find /usr -type f -name bpftool 2>/dev/null | head -1" \
      >"${out_dir}/bpftool_path.txt" 2>&1 || true
  echo "[${distro_key}] bpftool: $(cat ${out_dir}/bpftool_path.txt | tail -1)" >&2

  scp -i "${KEY_PATH}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o LogLevel=ERROR \
      build/iomoments.bpf.o build/iomoments-k3.bpf.o \
      "${ssh_user}@${public_ip}:/tmp/" >/dev/null

  for variant in k4:iomoments.bpf.o k3:iomoments-k3.bpf.o; do
    local key="${variant%%:*}" obj="${variant##*:}"
    local pin="/sys/fs/bpf/iomoments_${key}_$$"
    echo "[${distro_key}/${key}] loading ${obj}" >&2
    ssh "${ssh_opts[@]}" "${ssh_user}@${public_ip}" \
        "BPFTOOL=\$(which bpftool 2>/dev/null || sudo find /usr -type f -name bpftool 2>/dev/null | head -1); \
         echo \"BPFTOOL=\$BPFTOOL\"; \
         sudo \$BPFTOOL prog load /tmp/${obj} ${pin} 2>&1; \
         rc=\$?; echo \"VERDICT=\$rc\"; \
         sudo rm -f ${pin} 2>/dev/null || true" \
        >"${out_dir}/${key}.log" 2>&1 || true

    grep -E "^VERDICT=" "${out_dir}/${key}.log" | tail -1 >"${out_dir}/${key}.verdict" || true
    echo "[${distro_key}/${key}] $(cat ${out_dir}/${key}.verdict 2>/dev/null || echo 'no verdict captured')" >&2
  done
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
mkdir -p "${RESULTS_DIR}"

for obj in build/iomoments.bpf.o build/iomoments-k3.bpf.o; do
  if [ ! -f "${obj}" ]; then
    echo "ERROR: ${obj} not found. Run 'make bpf-compile' first." >&2
    exit 1
  fi
done

setup

for entry in "${DISTROS[@]}"; do
  IFS='|' read -r owner name_pattern distro_key ssh_user install_cmd <<<"${entry}"
  probe_distro "${owner}" "${name_pattern}" "${distro_key}" "${ssh_user}" "${install_cmd}" || true
done

echo "" >&2
echo "[done] results: ${RESULTS_DIR}/" >&2
ls -la "${RESULTS_DIR}/" >&2
