#!/usr/bin/env bash
# ==========================================================================
# deploy-cluster-profile.sh — apply a cluster profile to Online Boutique
# ==========================================================================
#
# Reads a cluster profile YAML (e.g. cluster-profiles/2-replica.yaml)
# and patches every deployment in the online-boutique namespace with the
# specified replicas, resource limits, and anti-affinity rules.
#
# Usage:
#   ./deploy-cluster-profile.sh 2-replica        # name without .yaml
#   ./deploy-cluster-profile.sh 1-replica         # restore original
#   ./deploy-cluster-profile.sh 2-replica --dry-run  # preview only
#
# The base service YAMLs under services/ are NEVER modified. All changes
# are applied via `kubectl patch` directly against the live cluster. To
# "undo" a profile, apply a different one (e.g. `1-replica`).
#
# ==========================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/k8s-config.sh"
PROFILE_DIR="${SCRIPT_DIR}/manifests/online-boutique/cluster-profiles"
NAMESPACE="online-boutique"

log()  { printf '\033[1;34m[cluster-profile]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m[cluster-profile]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m[cluster-profile]\033[0m %s\n' "$*" >&2; exit 1; }

PROFILE_NAME="${1:?usage: $0 <profile-name> [--dry-run]}"
DRY_RUN=false
[[ "${2:-}" == "--dry-run" ]] && DRY_RUN=true

PROFILE_FILE="${PROFILE_DIR}/${PROFILE_NAME}.yaml"
[[ -f "${PROFILE_FILE}" ]] || err "profile not found: ${PROFILE_FILE}"

log "applying cluster profile: ${PROFILE_NAME}"
[[ "${DRY_RUN}" == true ]] && log "(dry-run mode — no changes will be applied)"

# --------------------------------------------------------------------------
# Parse the profile YAML and emit kubectl patch commands via Python.
# --------------------------------------------------------------------------

python3 - "${PROFILE_FILE}" "${NAMESPACE}" "${DRY_RUN}" <<'PYEOF'
import yaml, json, sys, subprocess

profile_file, namespace, dry_run_str = sys.argv[1], sys.argv[2], sys.argv[3]
dry_run = dry_run_str == "true"

config = yaml.safe_load(open(profile_file))
services = config.get("services", {})

for svc_name, svc_config in services.items():
    replicas = svc_config.get("replicas", 1)
    cpu_req = str(svc_config.get("cpu_request", "200m"))
    cpu_lim = str(svc_config.get("cpu_limit", "300m"))
    mem_req = str(svc_config.get("mem_request", "64Mi"))
    mem_lim = str(svc_config.get("mem_limit", "128Mi"))
    anti_affinity = svc_config.get("anti_affinity", "none")
    container_name = svc_config.get("container", "server")

    deploy_name = svc_name

    # --- Build the strategic-merge patch ---
    patch = {
        "spec": {
            "replicas": replicas,
            "template": {
                "spec": {
                    "containers": [{
                        "name": container_name,
                        "resources": {
                            "requests": {"cpu": cpu_req, "memory": mem_req},
                            "limits":   {"cpu": cpu_lim, "memory": mem_lim},
                        },
                    }],
                },
            },
        },
    }

    # --- Anti-affinity ---
    if anti_affinity in ("required", "preferred"):
        affinity_spec = {"podAntiAffinity": {}}
        rule = {
            "labelSelector": {
                "matchLabels": {"app": svc_name},
            },
            "topologyKey": "kubernetes.io/hostname",
        }
        if anti_affinity == "required":
            affinity_spec["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"] = [rule]
        else:
            affinity_spec["podAntiAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"] = [{
                "weight": 100,
                "podAffinityTerm": rule,
            }]
        patch["spec"]["template"]["spec"]["affinity"] = affinity_spec
    elif anti_affinity == "none":
        # Explicitly clear any existing affinity so switching profiles works.
        patch["spec"]["template"]["spec"]["affinity"] = None

    patch_json = json.dumps(patch)

    action = "would patch" if dry_run else "patching"
    print(f"  {action} {deploy_name}: replicas={replicas}, "
          f"cpu={cpu_req}/{cpu_lim}, mem={mem_req}/{mem_lim}, "
          f"anti_affinity={anti_affinity}")

    if not dry_run:
        cmd = [
            "kubectl", "-n", namespace, "patch", "deployment", deploy_name,
            "--type=strategic", "-p", patch_json,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"    ERROR: {result.stderr.strip()}", file=sys.stderr)
        else:
            print(f"    {result.stdout.strip()}")

PYEOF

if [[ "${DRY_RUN}" == true ]]; then
  log "dry-run complete — no changes applied"
  exit 0
fi

# --- Wait for all deployments to roll out ---
log "waiting for rollout to complete..."
for svc in $(python3 -c "
import yaml
config = yaml.safe_load(open('${PROFILE_FILE}'))
for s in config.get('services', {}):
    print(s)
"); do
  kubectl -n "${NAMESPACE}" rollout status "deployment/${svc}" --timeout=120s || \
    warn "rollout timed out for ${svc}"
done

# --- Verify placement for required anti-affinity services ---
log "verifying anti-affinity placement..."
python3 - "${PROFILE_FILE}" "${NAMESPACE}" <<'PYEOF'
import yaml, subprocess, json, sys

config = yaml.safe_load(open(sys.argv[1]))
namespace = sys.argv[2]

for svc_name, svc_config in config.get("services", {}).items():
    if svc_config.get("anti_affinity") != "required":
        continue
    replicas = svc_config.get("replicas", 1)
    if replicas < 2:
        continue

    # Get pod-to-node mapping for this service.
    result = subprocess.run(
        ["kubectl", "-n", namespace, "get", "pods",
         "-l", f"app={svc_name}",
         "-o", "jsonpath={range .items[*]}{.metadata.name}:{.spec.nodeName}{\"\\n\"}{end}"],
        capture_output=True, text=True,
    )
    lines = [l.strip() for l in result.stdout.strip().split("\n") if ":" in l]
    nodes = [l.split(":")[1] for l in lines]
    unique_nodes = set(nodes)

    if len(unique_nodes) >= replicas:
        print(f"  ✓ {svc_name}: {len(lines)} replicas on {len(unique_nodes)} distinct nodes")
        for l in lines:
            print(f"      {l}")
    else:
        print(f"  ✗ {svc_name}: {len(lines)} replicas on only {len(unique_nodes)} node(s) — "
              f"anti-affinity constraint violated!", file=sys.stderr)
        for l in lines:
            print(f"      {l}", file=sys.stderr)

PYEOF

log "cluster profile '${PROFILE_NAME}' applied successfully"
