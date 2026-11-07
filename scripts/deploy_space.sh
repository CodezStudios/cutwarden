#!/usr/bin/env bash
# Push the backend to a Hugging Face Space.  Usage: HF_TOKEN=hf_xxx scripts/deploy_space.sh <hf-user>/<space-name>
set -euo pipefail
SPACE="${1:?usage: HF_TOKEN=... $0 <hf-user>/<space-name>}"
: "${HF_TOKEN:?set HF_TOKEN (a write token from huggingface.co/settings/tokens)}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
cp -r Dockerfile pyproject.toml LICENSE cutwarden brands "$TMP/"
cp scripts/space_readme.md "$TMP/README.md"   # also satisfies pyproject's readme at build time
cd "$TMP"
git init -q -b main && git add -A && git -c user.name=cutwarden -c user.email=dev@example.com commit -q -m "deploy"
git push -f "https://user:${HF_TOKEN}@huggingface.co/spaces/${SPACE}" main
echo "Pushed. Space: https://huggingface.co/spaces/${SPACE}"
