#!/bin/sh
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Apply pipeline: plan to a file, check it with the destroy guard, then apply
# exactly that saved plan.
#
# Usage: tf_apply.sh plan | guard | apply | all
#
#   plan   init, `terraform plan -out=tfplan`, and write tfplan.json
#   guard  run destroy_guard.py on tfplan.json (needs python3)
#   apply  `terraform apply tfplan`
#   all    plan, guard and apply in one go
#
# The steps are separate so a pipeline can run the guard in a different
# container from Terraform (Cloud Build does). They share TF_DIR, so run them
# in the same checkout.
#
# Environment: see tf_init.sh, plus
#   ALLOW_DESTROY  true to let the guard pass a plan that deletes protected
#                  resources (default: false)

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TF_DIR="${TF_DIR:-terraform/gcp}"
export TF_DIR
export TF_IN_AUTOMATION="${TF_IN_AUTOMATION:-1}"

do_plan() {
  sh "$SCRIPT_DIR/tf_init.sh"
  rm -f "$TF_DIR/tfplan" "$TF_DIR/tfplan.json"
  # Wait for a concurrent apply rather than failing on the state lock.
  terraform -chdir="$TF_DIR" plan -input=false -no-color -lock-timeout=15m -out=tfplan
  terraform -chdir="$TF_DIR" show -json tfplan > "$TF_DIR/tfplan.json"
}

do_guard() {
  python3 "$SCRIPT_DIR/destroy_guard.py" "$TF_DIR/tfplan.json"
}

do_apply() {
  if [ ! -f "$TF_DIR/tfplan" ]; then
    echo "No saved plan at $TF_DIR/tfplan; run '$0 plan' first." >&2
    exit 1
  fi
  terraform -chdir="$TF_DIR" apply -input=false -no-color -lock-timeout=15m tfplan
}

case "${1:-}" in
  plan) do_plan ;;
  guard) do_guard ;;
  apply) do_apply ;;
  all)
    do_plan
    do_guard
    do_apply
    ;;
  *)
    echo "Usage: $0 plan | guard | apply | all" >&2
    exit 2
    ;;
esac
