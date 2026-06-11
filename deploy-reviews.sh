#!/usr/bin/env bash
set -euo pipefail

# --- CONFIGURATION ---
USERNAME="sxh"
DEEPSEEK_API_KEY="sk-8231cc3fbcad481d8ae4fa1dc472e374"
CENTRAL_REPO="github-actions-share"

TARGET_REPOS=(
  "travel-app-1"
  "paints-app"
  "scheduler4"
  "dnd-app"
  "scale-alerts-app"
  "aircraft-web-java"
  "heroes-app"
)
# ---------------------

ORIGINAL_DIR=$(pwd)

for REPO in "${TARGET_REPOS[@]}"; do
  echo "--------------------------------------------------"
  echo "Processing repository: $REPO"
  echo "--------------------------------------------------"

  # 1. Add the secret
  echo "Uploading CHAT_TOKEN secret..."
  echo "$DEEPSEEK_API_KEY" | gh secret set CHAT_TOKEN --repo "$USERNAME/$REPO"

  # 2. Clean and Clone
  rm -rf "/tmp/$REPO"
  git clone --depth 1 "git@github.com:$USERNAME/$REPO.git" "/tmp/$REPO"
  cd "/tmp/$REPO"

  # 3. Create the workflow file
  mkdir -p .github/workflows
  
  # Using quotes around 'EOF' prevents Bash from trying to evaluate anything early
  cat << 'EOF' > .github/workflows/deepseek.yml
name: Code Review

on:
  pull_request_target:
    types: [opened, reopened, synchronize]

jobs:
  run-review:
    uses: sxh/github-actions-share/.github/workflows/deepseek-review-template.yml@main
    secrets:
      TOKEN: ${{ secrets.CHAT_TOKEN }}
EOF

  # 4. Push changes
  git add .github/workflows/deepseek.yml
  git commit --allow-empty -m "ci: add automated deepseek review workflow"
  git push origin main

  # 5. Safely step out BEFORE deleting the directory
  cd "$ORIGINAL_DIR"
  rm -rf "/tmp/$REPO"
done

echo "Global installation complete!"