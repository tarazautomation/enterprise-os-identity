#!/usr/bin/env bash
# LOCAL ONLY: creates Secret identity/keycloak-realm-secrets with a random client secret for each
# confidential app in realm-taraz.json. Existing values are kept. The Microsoft (Entra) secret is
# added separately by set-entra-secret.sh. On staging/production this Secret comes from
# AWS Secrets Manager through External Secrets instead.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
NS=identity
NAME=keycloak-realm-secrets
KEYS="KC_CLIENT_SECRET_AUDIT_LOG KC_CLIENT_SECRET_KNOWLEDGE_BASE KC_CLIENT_SECRET_LECTRA KC_CLIENT_SECRET_MCP_SERVER KC_CLIENT_SECRET_SCRIBE KC_CLIENT_SECRET_STORAGE_SERVICE KC_CLIENT_SECRET_TENANT_SERVICE"

kubectl -n "$NS" get secret "$NAME" >/dev/null 2>&1 || kubectl -n "$NS" create secret generic "$NAME" >/dev/null
for k in $KEYS; do
  if [ -n "$(kubectl -n "$NS" get secret "$NAME" -o jsonpath="{.data.$k}")" ]; then
    echo "kept    $k"
  else
    v=$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 32)
    printf '{"stringData":{"%s":"%s"}}' "$k" "$v" | kubectl -n "$NS" patch secret "$NAME" --type merge --patch-file /dev/stdin >/dev/null
    unset v
    echo "created $k"
  fi
done
