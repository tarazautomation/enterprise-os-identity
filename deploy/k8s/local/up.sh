#!/usr/bin/env bash
# LOCAL ONLY: bring up Keycloak on a k3d cluster, the same way it runs on EKS.
# Safe to run again: every step skips what already exists.
#
#   bash local/up.sh            (inside WSL/Linux, from anywhere)
#
# Needs: Docker, k3d, kubectl, openssl, curl. Port 8080 must be free
# (stop any other Keycloak first). Keycloak ends up at http://localhost:8080.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
CLUSTER=taraz-local
NS=identity

step() { printf '\n== %s\n' "$*"; }

step "1/6 cluster $CLUSTER"
if k3d cluster list 2>/dev/null | grep -q "^$CLUSTER "; then
  echo "exists"
else
  k3d cluster create "$CLUSTER" --agents 1 -p "8080:80@loadbalancer" --wait --timeout 180s 2>&1 | tail -1
fi

step "2/6 Keycloak Operator (namespace $NS)"
kubectl apply --server-side --force-conflicts -k "$ROOT/base/operator" >/dev/null
kubectl -n "$NS" rollout status deployment/keycloak-operator --timeout=240s

step "3/6 database secret"
if kubectl -n "$NS" get secret keycloak-db >/dev/null 2>&1; then
  echo "kept"
else
  pw=$(openssl rand -base64 36 | tr -dc 'A-Za-z0-9' | head -c 32)
  kubectl -n "$NS" create secret generic keycloak-db --from-literal=username=keycloak --from-literal=password="$pw" >/dev/null
  unset pw; echo "created (random password)"
fi

step "4/6 realm secrets"
bash "$HERE/create-app-secrets.sh"
if [ -z "$(kubectl -n "$NS" get secret keycloak-realm-secrets -o jsonpath='{.data.KC_ENTRA_CLIENT_SECRET}')" ]; then
  echo
  echo "The Microsoft (Entra) client secret is missing. Run this, paste the secret, then run up.sh again:"
  echo "  bash $HERE/set-entra-secret.sh"
  exit 2
fi
echo "KC_ENTRA_CLIENT_SECRET present"

step "5/6 Postgres, Keycloak (2 copies), realm import, routing"
kubectl apply -k "$HERE" | sed 's/^/  /'
kubectl -n "$NS" rollout status statefulset/keycloak-db --timeout=180s >/dev/null
kubectl -n "$NS" wait keycloak/keycloak --for=condition=Ready --timeout=600s >/dev/null
for i in $(seq 1 60); do
  [ "$(kubectl -n "$NS" get keycloakrealmimport taraz-realm -o jsonpath='{.status.conditions[?(@.type=="Done")].status}' 2>/dev/null)" = "True" ] && break
  sleep 5
done

step "6/6 checks"
curl -s -o /dev/null -w "  http://localhost:8080/realms/taraz/.well-known/openid-configuration -> HTTP %{http_code}\n" \
  http://localhost:8080/realms/taraz/.well-known/openid-configuration
kubectl -n "$NS" get pods
echo
echo "Sign in: http://localhost:8080/realms/taraz/account  (Microsoft 365)"
echo "Admin console: http://localhost:8080/admin  (user/password in Secret $NS/keycloak-initial-admin)"
