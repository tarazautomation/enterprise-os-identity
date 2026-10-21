#!/usr/bin/env bash
# LOCAL ONLY: stores the Microsoft (Entra) client secret in Secret identity/keycloak-realm-secrets
# as KC_ENTRA_CLIENT_SECRET. It asks for the value with hidden typing, so the secret never appears
# on screen, in shell history or in any file.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
NS=identity
NAME=keycloak-realm-secrets

kubectl -n "$NS" get secret "$NAME" >/dev/null 2>&1 || kubectl -n "$NS" create secret generic "$NAME" >/dev/null
read -rs -p "Paste the Microsoft client secret VALUE (typing is hidden), then press Enter: " s
echo
s=$(printf '%s' "$s" | tr -d '[:space:]')
if [ ${#s} -lt 20 ]; then echo "That looks too short (${#s} characters). Nothing saved."; exit 1; fi
printf '{"stringData":{"KC_ENTRA_CLIENT_SECRET":"%s"}}' "$s" | kubectl -n "$NS" patch secret "$NAME" --type merge --patch-file /dev/stdin >/dev/null
echo "Saved: KC_ENTRA_CLIENT_SECRET (${#s} characters, starts with '${s:0:3}')"
unset s
