#!/usr/bin/env bash
# Usage: sudo ./bootstrap.sh [--detect-only]
set -euo pipefail
cd "$(dirname "$0")"

apt-get update
apt-get install -y python3 python3-yaml ansible git cpu-checker util-linux

python3 detect.py "$@"
[[ "${1:-}" == "--detect-only" ]] && exit 0

ansible-galaxy collection install -r ansible/requirements.yml
ansible-playbook -i localhost, -c local ansible/site.yml \
  -e @profile.generated.json
