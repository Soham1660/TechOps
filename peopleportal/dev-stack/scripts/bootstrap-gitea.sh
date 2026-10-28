#!/usr/bin/env bash
# Reconcile Gitea's admin, recoverable local API token and OIDC auth source.
set -euo pipefail
# shellcheck source=peopleportal/dev-stack/scripts/common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
require_tools openssl
require_docker
load_env
[[ -s .oidc ]] || die "Run scripts/bootstrap-authentik.sh first."
# The blueprint uses the stable credentials in .env; .oidc records API verification.
url="http://127.0.0.1:$GITEA_PORT"
wait_url "$url/api/healthz"
users=$(gitea_admin user list)
if ! awk -v user="$GITEA_ADMIN_USER" '$2 == user { found=1 } END { exit !found }' <<<"$users"; then
  gitea_admin user create --username "$GITEA_ADMIN_USER" \
    --password "$GITEA_ADMIN_PASSWORD" --email "$GITEA_ADMIN_EMAIL" \
    --admin --must-change-password=false >&2
fi

# Tokens never go on a command line, where any local process could read them:
# curl takes them from a --config on stdin, the Python helper from its env.
token_valid=false
if [[ -s .gitea-token ]]; then
  token=$(<.gitea-token)
  if printf 'header = "Authorization: token %s"\n' "$token" |
    curl --fail --silent --max-time 10 --config - "$url/api/v1/user" >/dev/null; then
    token_valid=true
  fi
fi
new_token() {
  local raw
  raw=$(gitea_admin user generate-access-token --username "$GITEA_ADMIN_USER" \
    --token-name "$1" --scopes all --raw)
  raw=$(awk '/^[0-9a-fA-F]+$/ && length($0) == 40 {print}' <<<"$raw")
  [[ $raw =~ ^[0-9a-fA-F]{40}$ ]] || die "Gitea did not return a single raw token."
  printf '%s\n' "$raw"
}

# Prints the ids of the admin's tokens named $2, or starting with $3 if given,
# except one named $4, authenticating with token $1. Gitea's token API needs
# HTTP Basic, and ENABLE_BASIC_AUTHENTICATION=false refuses passwords there, so
# the credential is a token sent as the password. JSON is parsed inside
# authentik's image; HTTP failures must abort rather than read as "none".
token_ids() {
  GITEA_BASIC_SECRET=$1 compose exec -T -e GITEA_BASIC_SECRET authentik-worker \
    python - "$GITEA_ADMIN_USER" "$2" "${3:-}" "${4:-}" <<'PY'
import base64, json, os, sys, urllib.request
user, name, prefix, skip = sys.argv[1:]
secret = os.environ['GITEA_BASIC_SECRET']
request = urllib.request.Request(f'http://gitea:3000/api/v1/users/{user}/tokens')
request.add_header('Authorization', 'Basic ' + base64.b64encode(f'{user}:{secret}'.encode()).decode())
with urllib.request.urlopen(request, timeout=10) as response:
    tokens = json.load(response)
for t in tokens:
    if t['name'] != skip and (t['name'] == name or (prefix and t['name'].startswith(prefix))):
        print(t['id'])
PY
}

# Deletes the admin's token $2 (an id or a name), authenticating with token $1.
delete_token() {
  printf 'user = "%s:%s"\n' "$GITEA_ADMIN_USER" "$1" |
    curl --fail --silent --show-error --max-time 10 --config - --request DELETE \
      "$url/api/v1/users/$GITEA_ADMIN_USER/tokens/$2" >/dev/null
}

# If recovery stops partway, the short-lived token revokes itself on exit so a
# failed run doesn't leave an extra admin token behind. By name, so this needs
# only curl and Gitea, not the lookup that may be what failed.
revoke_rotate() {
  [[ -n ${rotate:-} ]] || return 0
  delete_token "$rotate" "$rotate_name" ||
    echo "WARNING: could not revoke Gitea token $rotate_name; delete it in $GITEA_ADMIN_USER's Applications settings." >&2
}

if [[ $token_valid == false ]]; then
  # The CLI can mint tokens but not list or delete them. A short-lived token
  # clears the stale people-portal-dev one, and any rotation tokens an earlier
  # failed run left, so the name can be reused. The replacement is saved before
  # it deletes the short-lived token. Only this stack's names are touched.
  rotate_name="people-portal-dev-rotate-$(openssl rand -hex 4)"
  rotate=$(new_token "$rotate_name")
  trap revoke_rotate EXIT
  # Assign first: set -e ignores a failed substitution in a for list.
  stale_ids=$(token_ids "$rotate" people-portal-dev people-portal-dev-rotate- "$rotate_name")
  for id in $stale_ids; do
    delete_token "$rotate" "$id"
  done
  token=$(new_token people-portal-dev)
  private_file "$STACK_DIR/.gitea-token" <<<"$token"
  delete_token "$token" "$rotate_name"
  rotate=""
  trap - EXIT
fi
chmod 600 .gitea-token

sources=$(gitea_admin auth list)
source_id=$(awk '$2 == "authentik" {print $1}' <<<"$sources")
args=(--name authentik --provider openidConnect --key "$GITEA_OIDC_CLIENT_ID"
  --secret "$GITEA_OIDC_CLIENT_SECRET"
  --auto-discover-url "$AUTHENTIK_URL/application/o/gitea/.well-known/openid-configuration"
  --scopes openid --scopes email --scopes profile --scopes people_portal)
# OpenSSL is present on both supported hosts; avoid GNU sha256sum.
wanted=$(printf '%s\n' "${args[@]}" | openssl dgst -sha256 | awk '{print $NF}')
save_hash=false
if [[ -z $source_id ]]; then
  gitea_admin auth add-oauth "${args[@]}" >&2
  save_hash=true
else
  current=$(compose exec -T --user git gitea sh -c \
    'if [ -f /data/gitea/.portal-oidc-hash ]; then cat /data/gitea/.portal-oidc-hash; fi')
  if [[ $current != "$wanted" ]]; then
    gitea_admin auth update-oauth --id "$source_id" "${args[@]}" >&2
    save_hash=true
  fi
fi
if [[ $save_hash == true ]]; then
  printf '%s\n' "$wanted" | compose exec -T --user git gitea sh -c \
    'umask 077; cat > /data/gitea/.portal-oidc-hash'
fi
echo "Gitea admin, token and authentik OIDC source ready (.gitea-token mode 600)." >&2
