#!/usr/bin/env bash
set -euo pipefail

# Fetch latest successful workflow run SHA and write IMAGE_TAG into .env.prod.
# Usage:
#   scripts/set_image_tag_from_actions.sh
#   scripts/set_image_tag_from_actions.sh --workflow build-prod.yml --branch main --env-file .env.prod

WORKFLOW_FILE="build-prod.yml"
BRANCH="main"
ENV_FILE=".env.prod"
SHA_LEN="12"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workflow)
      WORKFLOW_FILE="$2"
      shift 2
      ;;
    --branch)
      BRANCH="$2"
      shift 2
      ;;
    --env-file)
      ENV_FILE="$2"
      shift 2
      ;;
    --sha-len)
      SHA_LEN="$2"
      shift 2
      ;;
    -h|--help)
      sed -n '1,20p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if ! [[ "$SHA_LEN" =~ ^[0-9]+$ ]] || [[ "$SHA_LEN" -lt 7 ]] || [[ "$SHA_LEN" -gt 40 ]]; then
  echo "--sha-len must be between 7 and 40" >&2
  exit 1
fi

if [[ -z "${GITHUB_TOKEN:-}" && -z "${GH_TOKEN:-}" ]]; then
  echo "Set GITHUB_TOKEN or GH_TOKEN with access to GitHub Actions metadata." >&2
  exit 1
fi

TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"

if ! command -v git >/dev/null 2>&1; then
  echo "git is required" >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required" >&2
  exit 1
fi

ORIGIN_URL="$(git remote get-url origin)"
REPO_SLUG="$(python3 - "$ORIGIN_URL" <<'PY'
import re
import sys
url = sys.argv[1].strip()
patterns = [
    r"github\.com[:/](?P<slug>[^\s]+?)(?:\.git)?$",
]
for p in patterns:
    m = re.search(p, url)
    if m:
        slug = m.group("slug")
        print(slug)
        sys.exit(0)
print("")
PY
)"

if [[ -z "$REPO_SLUG" ]]; then
  echo "Could not derive owner/repo from git remote origin: $ORIGIN_URL" >&2
  exit 1
fi

API_URL="https://api.github.com/repos/${REPO_SLUG}/actions/workflows/${WORKFLOW_FILE}/runs?status=success&branch=${BRANCH}&per_page=1"
JSON="$(curl -fsSL \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  "${API_URL}")"

HEAD_SHA="$(python3 - <<'PY' "$JSON"
import json
import sys
obj = json.loads(sys.argv[1])
runs = obj.get("workflow_runs") or []
if not runs:
    print("")
    sys.exit(0)
print(runs[0].get("head_sha", ""))
PY
)"

if [[ -z "$HEAD_SHA" ]]; then
  echo "No successful workflow runs found for ${WORKFLOW_FILE} on branch ${BRANCH}." >&2
  exit 1
fi

IMAGE_TAG="${HEAD_SHA:0:${SHA_LEN}}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Env file not found: $ENV_FILE" >&2
  exit 1
fi

python3 - "$ENV_FILE" "$IMAGE_TAG" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
image_tag = sys.argv[2]
text = path.read_text()
lines = text.splitlines()
updated = False
for i, line in enumerate(lines):
    if line.startswith("IMAGE_TAG="):
        lines[i] = f"IMAGE_TAG={image_tag}"
        updated = True
        break
if not updated:
    if lines and lines[-1] != "":
        lines.append("")
    lines.append(f"IMAGE_TAG={image_tag}")
path.write_text("\n".join(lines) + "\n")
PY

echo "Repository: ${REPO_SLUG}"
echo "Workflow: ${WORKFLOW_FILE}"
echo "Branch: ${BRANCH}"
echo "Selected IMAGE_TAG=${IMAGE_TAG}"
echo "Updated ${ENV_FILE}"
