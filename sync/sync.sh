#!/usr/bin/env bash
# Sobe para o GitHub o que mudou na pasta (uso do dono do repo; disparado pela unidade de caminho do systemd).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
sleep "${SYNC_DEBOUNCE:-20}"            # junta edições feitas em sequência num commit só
[ -n "$(git status --porcelain)" ] || exit 0
git add -A
git commit -qm "sync: $(date '+%Y-%m-%d %H:%M')"
git push -q origin HEAD
