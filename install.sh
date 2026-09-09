#!/bin/sh
# AgentWeb one-line Mabc installer.
# Usage:
#   curl -fsSL https://agentweb.example/install.sh | sh -s -- --enroll https://gateway.example/setup/api/enrollment/manifest/ONE_TIME_CLAIM
# Optional env:
#   AGENTWEB_HOME=$HOME/.agentweb
#   AGENTWEB_DEVICE_NAME=myhost
#   AGENTWEB_ENROLL_URL=https://gateway.example/setup/api/enrollment/manifest/ONE_TIME_CLAIM
#   AGENTWEB_REMOTE_GWS=wss://gw/ws?role=upstream,...  (break-glass only)
#   AGENTWEB_CHANNEL=prod
#   HTTPS_PROXY=http://user:password@proxy.example:8080  (inherited for HTTP fallback)
# Equivalent args are supported when using `sh -s --`, for example:
#   curl -fsSL https://agentweb.example/install.sh | sh -s -- --device m2mac

set -eu
umask 077

log() {
  printf '%s\n' "$*" >&2
}

fail() {
  log "ERROR: $*"
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"
}

download() {
  src="$1"
  dst="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$src" -o "$dst"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$dst" "$src"
  else
    fail "curl or wget is required"
  fi
}

sha256_file() {
  file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file" | awk '{print $1}'
  else
    fail "sha256sum or shasum is required"
  fi
}

sanitize_name() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_]/_/g; s/^_*//; s/_*$//; s/__*/_/g'
}

usage() {
  cat >&2 <<'USAGE'
Usage: install.sh --enroll CLAIM_URL [--home PATH]
       install.sh --device NAME --remote-gws URLS [--channel dev|main|prod] [--home PATH]

Environment variables:
  AGENTWEB_DEVICE_NAME   Stable topology device name. Prefer this over hostname.
  AGENTWEB_ENROLL_URL    Single-use manifest claim issued by an authorized gateway.
  AGENTWEB_CHANNEL       Release channel, default prod.
  AGENTWEB_HOME          Install root, default ${HOME}/.agentweb.
  AGENTWEB_REMOTE_GWS    Explicit break-glass upstream URLs; never supplied by the public package.
  HTTPS_PROXY            Optional http:// proxy inherited by installed HTTP-stream fallback.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --device)
      [ "$#" -ge 2 ] || fail "--device requires a value"
      AGENTWEB_DEVICE_NAME="$2"
      shift 2
      ;;
    --enroll)
      [ "$#" -ge 2 ] || fail "--enroll requires a value"
      AGENTWEB_ENROLL_URL="$2"
      shift 2
      ;;
    --channel)
      [ "$#" -ge 2 ] || fail "--channel requires a value"
      AGENTWEB_CHANNEL="$2"
      shift 2
      ;;
    --home)
      [ "$#" -ge 2 ] || fail "--home requires a value"
      AGENTWEB_HOME="$2"
      shift 2
      ;;
    --remote-gws)
      [ "$#" -ge 2 ] || fail "--remote-gws requires a value"
      AGENTWEB_REMOTE_GWS="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown argument: $1"
      ;;
  esac
done

json_value_for_artifact() {
  key="$1"
  field="$2"
  manifest="$3"
  if command -v perl >/dev/null 2>&1; then
    perl -0777 -e 'my ($k,$f,$path)=@ARGV; open my $fh, "<", $path or die $!; local $/; $_=<$fh>; if (/"\Q$k\E"\s*:\s*\{.*?"\Q$f\E"\s*:\s*"([^"]+)"/s) { print "$1\n"; exit 0 } exit 1' "$key" "$field" "$manifest"
  elif command -v python3 >/dev/null 2>&1; then
    python3 - "$key" "$field" "$manifest" <<'PY'
import json, sys
key, field, path = sys.argv[1:]
with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)
print(data["artifacts"][key][field])
PY
  elif command -v python >/dev/null 2>&1; then
    python - "$key" "$field" "$manifest" <<'PY'
import json, sys
key, field, path = sys.argv[1:]
with open(path, "r") as f:
    data = json.load(f)
print(data["artifacts"][key][field])
PY
  else
    return 1
  fi
}

json_value() {
  path="$1"
  file="$2"
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$path" "$file" <<'PY'
import json, sys
value = json.load(open(sys.argv[2], encoding="utf-8"))
try:
    for part in sys.argv[1].split('.'):
        value = value[part]
except (KeyError, IndexError, TypeError):
    raise SystemExit(1)
if isinstance(value, (dict, list)):
    raise SystemExit(1)
print(str(value).lower() if isinstance(value, bool) else value)
PY
  elif command -v perl >/dev/null 2>&1; then
    key="${path##*.}"
    perl -0777 -e 'my ($k,$path)=@ARGV; open my $fh,"<",$path or exit 1; local $/; $_=<$fh>; /"\Q$k\E"\s*:\s*"([^"]*)"/s or exit 1; print "$1\n"' "$key" "$file"
  else
    return 1
  fi
}

json_array_csv() {
  path="$1"
  file="$2"
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$path" "$file" <<'PY'
import json, sys
value = json.load(open(sys.argv[2], encoding="utf-8"))
for part in sys.argv[1].split('.'):
    value = value[part]
print(",".join(value))
PY
  elif command -v perl >/dev/null 2>&1; then
    key="${path##*.}"
    perl -0777 -e 'my ($k,$path)=@ARGV; open my $fh,"<",$path or exit 1; local $/; $_=<$fh>; /"\Q$k\E"\s*:\s*\[(.*?)\]/s or exit 1; my $body=$1; my @v=($body =~ /"([^"]+)"/g); @v or exit 1; print join(",",@v),"\n"' "$key" "$file"
  else
    return 1
  fi
}

new_runtime_id() {
  prefix="$1"
  if command -v uuidgen >/dev/null 2>&1; then
    value="$(uuidgen | tr '[:upper:]' '[:lower:]' | tr -d '-')"
  else
    value="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
  fi
  [ "${#value}" -eq 32 ] || fail "failed to generate runtime identity"
  printf '%s_%s\n' "$prefix" "$value"
}

os="$(uname -s 2>/dev/null | tr '[:upper:]' '[:lower:]')"
arch="$(uname -m 2>/dev/null | tr '[:upper:]' '[:lower:]')"
case "$os:$arch" in
  linux:x86_64|linux:amd64) artifact_key="linux-x64-musl" ;;
  linux:aarch64|linux:arm64) artifact_key="linux-arm64-musl" ;;
  darwin:arm64|darwin:aarch64) artifact_key="macos-arm64" ;;
  darwin:x86_64|darwin:amd64) fail "macOS x86_64 is not published in the prod channel yet" ;;
  *) fail "unsupported platform: $(uname -s 2>/dev/null || echo unknown) $(uname -m 2>/dev/null || echo unknown)" ;;
esac

need_cmd awk
need_cmd sed

channel="${AGENTWEB_CHANNEL:-prod}"
repo="${AGENTWEB_RELEASE_REPO:-yxsicd/awrelease}"
agentweb_home="${AGENTWEB_HOME:-${HOME}/.agentweb}"
upstream_proxy="${AGENTGW_UPSTREAM_PROXY:-}"
if [ -z "$upstream_proxy" ]; then
  for proxy_candidate in \
    "${HTTPS_PROXY:-}" \
    "${https_proxy:-}" \
    "${HTTP_PROXY:-}" \
    "${http_proxy:-}"
  do
    case "$proxy_candidate" in
      http://*) upstream_proxy="$proxy_candidate"; break ;;
      HTTP://*) upstream_proxy="http://${proxy_candidate#HTTP://}"; break ;;
      "") ;;
      *) ;;
    esac
  done
fi
if [ -n "$upstream_proxy" ]; then
  case "$upstream_proxy" in
    http://*) ;;
    *) fail "the inherited upstream proxy must use http://" ;;
  esac
  case "$upstream_proxy" in
    *[[:space:]]*) fail "the inherited upstream proxy must not contain whitespace" ;;
  esac
  case "$upstream_proxy" in
    *[!A-Za-z0-9._~:/@%+=-]*) fail "percent-encode special characters in the inherited upstream proxy" ;;
  esac
fi
bin_dir="${agentweb_home}/bin"
env_dir="${agentweb_home}/env.d"
state_dir="${agentweb_home}/state"
log_dir="${agentweb_home}/logs"
tmp_dir="${agentweb_home}/tmp"
bin_path="${bin_dir}/agentgw"
manifest_path="${tmp_dir}/${channel}.json"
bootstrap_path="${tmp_dir}/bootstrap.$$.json"

mkdir -p "$bin_dir" "$env_dir" "$state_dir" "$log_dir" "$tmp_dir"

enrollment_token=""
node_token_endpoint=""
upstream_transport="auto"
upstream_http_token=""
management_domain_id=""
registration_policy_id=""
install_profile="production"
if [ -n "${AGENTWEB_ENROLL_URL:-}" ]; then
  [ -z "${AGENTWEB_REMOTE_GWS:-}" ] || fail "--enroll cannot be combined with --remote-gws"
  log "AgentWeb enrollment: consuming single-use bootstrap manifest"
  download "$AGENTWEB_ENROLL_URL" "$bootstrap_path"
  manifest_kind="$(json_value kind "$bootstrap_path" || true)"
  [ "$manifest_kind" = "agentwebBootstrapManifest" ] || fail "claim did not return an AgentWeb bootstrap manifest"
  claim_device="$(json_value deviceName "$bootstrap_path" || true)"
  [ -n "$claim_device" ] || fail "bootstrap manifest has no deviceName"
  if [ -n "${AGENTWEB_DEVICE_NAME:-}" ] && [ "$(sanitize_name "$AGENTWEB_DEVICE_NAME")" != "$(sanitize_name "$claim_device")" ]; then
    fail "--device does not match the signed bootstrap manifest"
  fi
  AGENTWEB_DEVICE_NAME="$claim_device"
  remote_gws="$(json_array_csv remoteGws "$bootstrap_path" || true)"
  enrollment_token="$(json_value enrollmentToken "$bootstrap_path" || true)"
  node_token_endpoint="$(json_value nodeTokenEndpoint "$bootstrap_path" || true)"
  upstream_transport="$(json_value upstreamTransport "$bootstrap_path" || true)"
  upstream_http_token="$(json_value upstreamHttpToken "$bootstrap_path" || true)"
  management_domain_id="$(json_value managementDomainId "$bootstrap_path" || true)"
  registration_policy_id="$(json_value registrationPolicyId "$bootstrap_path" || true)"
  install_profile="$(json_value profile "$bootstrap_path" || true)"
  claim_channel="$(json_value release.channel "$bootstrap_path" || true)"
  claim_manifest_url="$(json_value release.manifestUrl "$bootstrap_path" || true)"
  [ -n "$remote_gws" ] || fail "bootstrap manifest has no remoteGws"
  [ -n "$enrollment_token" ] || fail "bootstrap manifest has no enrollmentToken"
  [ -z "$upstream_transport" ] || [ "$upstream_transport" = "auto" ] || fail "unsupported bootstrap upstreamTransport"
  [ -z "$upstream_http_token" ] || [ "${#upstream_http_token}" -ge 32 ] || fail "bootstrap upstreamHttpToken is too short"
  upstream_transport="${upstream_transport:-auto}"
  [ -n "$management_domain_id" ] || fail "bootstrap manifest has no managementDomainId"
  [ -n "$registration_policy_id" ] || fail "bootstrap manifest has no registrationPolicyId"
  [ "$install_profile" = "production" ] || [ "$install_profile" = "dv" ] || fail "unsupported bootstrap profile: $install_profile"
  [ -z "$claim_channel" ] || channel="$claim_channel"
  manifest_url="${AGENTWEB_MANIFEST_URL:-$claim_manifest_url}"
  rm -f "$bootstrap_path"
else
  remote_gws="${AGENTWEB_REMOTE_GWS:-}"
  [ -n "$remote_gws" ] || fail "an enrollment claim is required; use --enroll CLAIM_URL (or explicit --remote-gws for break-glass recovery)"
  manifest_url="${AGENTWEB_MANIFEST_URL:-https://github.com/${repo}/releases/download/${channel}/agentgw-${channel}.json}"
fi
[ -n "$manifest_url" ] || fail "bootstrap manifest has no release manifest URL"

default_device="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo agentweb-node)"
existing_device=""
if [ -z "${AGENTWEB_DEVICE_NAME:-}" ]; then
  for existing_env in \
    "${agentweb_home}/ma/config.env" \
    "${agentweb_home}/mb/config.env" \
    "${agentweb_home}/mc/config.env" \
    "${agentweb_home}/env.d"/*.env
  do
    [ -f "$existing_env" ] || continue
    existing_device="$(sed -n 's/^AGENTGW_DEVICE_NAME=//p' "$existing_env" | head -1 | tr -d '"' || true)"
    [ -n "$existing_device" ] && break
  done
fi
device_name="$(sanitize_name "${AGENTWEB_DEVICE_NAME:-${existing_device:-$default_device}}")"
[ -n "$device_name" ] || device_name="agentweb_node"

log "AgentWeb install: channel=${channel} platform=${os}/${arch} artifact=${artifact_key}"
download "$manifest_url" "$manifest_path"

download_url="$(json_value_for_artifact "$artifact_key" downloadUrl "$manifest_path" || true)"
expected_sha="$(json_value_for_artifact "$artifact_key" sha256 "$manifest_path" || true)"
[ -n "$download_url" ] || fail "manifest does not contain downloadUrl for ${artifact_key}: ${manifest_url}"
[ -n "$expected_sha" ] || fail "manifest does not contain sha256 for ${artifact_key}: ${manifest_url}"

new_bin="${tmp_dir}/agentgw.${artifact_key}.$$"
download "$download_url" "$new_bin"
actual_sha="$(sha256_file "$new_bin")"
[ "$actual_sha" = "$expected_sha" ] || fail "sha256 mismatch for ${download_url}: expected ${expected_sha}, got ${actual_sha}"
chmod 0755 "$new_bin"
if [ -x "$bin_path" ]; then
  cp -p "$bin_path" "${bin_path}.bak.$(date -u +%Y%m%d%H%M%S)"
fi
mv "$new_bin" "$bin_path"
chmod 0755 "$bin_path"
if [ "$os" = "darwin" ] && command -v codesign >/dev/null 2>&1; then
  codesign --force --sign - "$bin_path" >/dev/null 2>&1 || fail "macOS codesign failed for ${bin_path}"
fi

write_env() {
  role="$1"
  mode="$2"
  listen="$3"
  bind="$4"
  browser="$5"
  display="$6"
  role_dir="${agentweb_home}/${role}"
  mkdir -p "$role_dir" "${state_dir}/${role}" "${role_dir}/dist"
  if [ ! -s "${role_dir}/node-id" ]; then
    new_runtime_id lgw >"${role_dir}/node-id"
  fi
  if [ ! -s "${agentweb_home}/device-id" ]; then
    new_runtime_id dev >"${agentweb_home}/device-id"
  fi
  node_id="$(tr -d '\r\n' <"${role_dir}/node-id")"
  role_enrollment_token="$enrollment_token"
  if [ -n "$node_token_endpoint" ]; then
    token_request="${tmp_dir}/node-token-${role}.$$.json"
    token_response="${tmp_dir}/node-token-${role}.$$.response.json"
    printf '{"nodeId":"%s","managerRole":"%s"}\n' "$node_id" "$role" >"$token_request"
    chmod 0600 "$token_request"
    if command -v curl >/dev/null 2>&1; then
      printf 'header = "Authorization: Bearer %s"\n' "$enrollment_token" | \
        curl --config - -fsSL -H 'content-type: application/json' \
          --data-binary "@$token_request" "$node_token_endpoint" -o "$token_response"
    elif command -v wget >/dev/null 2>&1; then
      wget -qO "$token_response" \
        --header="Authorization: Bearer $enrollment_token" \
        --header='content-type: application/json' \
        --post-file="$token_request" "$node_token_endpoint"
    else
      fail "curl or wget is required to bind the enrollment token to $role"
    fi
    role_enrollment_token="$(json_value enrollmentToken "$token_response" || true)"
    returned_node_id="$(json_value nodeId "$token_response" || true)"
    returned_role="$(json_value managerRole "$token_response" || true)"
    rm -f "$token_request" "$token_response"
    [ "$returned_node_id" = "$node_id" ] || fail "node-token response does not match $role nodeId"
    [ "$returned_role" = "$role" ] || fail "node-token response does not match manager role $role"
    [ -n "$role_enrollment_token" ] || fail "node-token response has no enrollmentToken"
  fi
  cat >"${role_dir}/config.env" <<ENV
AGENTWEB_HOME=${agentweb_home}
AGENTGW_MODE=${mode}
AGENTGW_LISTEN=${listen}
AGENTGW_BIND=${bind}
AGENTGW_NODE_ID_FILE=${role_dir}/node-id
AGENTGW_DEVICE_ID_FILE=${agentweb_home}/device-id
AGENTGW_NODE_NAME=${device_name}-${role}
AGENTGW_DISPLAY_NAME=${device_name} ${display}
AGENTGW_DEVICE_NAME=${device_name}
AGENTGW_BROWSER_NAME=${browser}
AGENTGW_MANAGER_ROLE=${role}
AGENTGW_CDP_HTTP=http://127.0.0.1:9223
AGENTGW_CDP_HEARTBEAT_MS=0
AGENTGW_REMOTE_GWS=${remote_gws}
AGENTGW_UPSTREAM_MODE=all
AGENTGW_UPSTREAM_TRANSPORT=${upstream_transport}
AGENTGW_UPSTREAM_PROXY=${upstream_proxy}
AGENTGW_UPSTREAM_HTTP_TOKEN=${upstream_http_token}
AGENTGW_ENROLLMENT_TOKEN=${role_enrollment_token}
AGENTGW_MANAGEMENT_DOMAIN_ID=${management_domain_id}
AGENTGW_REGISTRATION_POLICY_ID=${registration_policy_id}
AGENTGW_ENROLLMENT_PROFILE=${install_profile}
AGENTGW_MANAGER_DIR=${state_dir}/${role}/manager
AGENTGW_MANAGER_CHILD_ENV_FILES=
AGENTGW_DIST_DIR=${role_dir}/dist
AGENTGW_SELF_PATH=${bin_path}
AGENTGW_LOG_FORMAT=json
RUST_LOG=agentgw=info
ENV
  cp "${role_dir}/config.env" "${env_dir}/agentweb-${device_name}-${role}.env"
}

if [ "$install_profile" = "dv" ]; then
  write_env md manager true 127.0.0.1:17888 manager-validation Md
else
  write_env ma manager true 127.0.0.1:17888 default-chrome Ma
  write_env mb manager none none manager-backup Mb
  write_env mc manager none none manager-backup-b Mc
fi

install_linux_systemd() {
  scope="$1"
  role="$2"
  unit_name="agentweb-${device_name}-${role}.service"
  role_dir="${agentweb_home}/${role}"
  if [ "$scope" = "system" ]; then
    unit_dir="/etc/systemd/system"
    systemctl_cmd="systemctl"
    wanted_by="multi-user.target"
  else
    unit_dir="${HOME}/.config/systemd/user"
    systemctl_cmd="systemctl --user"
    wanted_by="default.target"
    mkdir -p "$unit_dir"
  fi
  cat >"${unit_dir}/${unit_name}" <<UNIT
[Unit]
Description=AgentWeb ${device_name} ${role}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=${role_dir}/config.env
WorkingDirectory=${state_dir}/${role}
ExecStart=${bin_path}
Restart=always
RestartSec=3
KillSignal=SIGTERM
TimeoutStopSec=30
LimitNOFILE=1048576

[Install]
WantedBy=${wanted_by}
UNIT
  $systemctl_cmd daemon-reload
  $systemctl_cmd enable "$unit_name" >/dev/null
  $systemctl_cmd restart "$unit_name"
}

install_macos_launchd() {
  role="$1"
  label="win.yxsbase.agentweb.${device_name}.${role}"
  plist_dir="${HOME}/Library/LaunchAgents"
  plist="${plist_dir}/${label}.plist"
  role_dir="${agentweb_home}/${role}"
  mkdir -p "$plist_dir"
  cat >"$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-lc</string>
    <string>set -a; . '${role_dir}/config.env'; set +a; exec '${bin_path}'</string>
  </array>
  <key>WorkingDirectory</key><string>${state_dir}/${role}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${log_dir}/${role}.out.log</string>
  <key>StandardErrorPath</key><string>${log_dir}/${role}.err.log</string>
</dict>
</plist>
PLIST
  launchctl bootout "gui/$(id -u)" "$plist" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$plist" 2>/dev/null || {
    launchctl unload -w "$plist" >/dev/null 2>&1 || true
    launchctl load -w "$plist"
  }
  launchctl kickstart -k "gui/$(id -u)/${label}" >/dev/null 2>&1 || true
}

cleanup_stale_macos_launchd() {
  uid="$(id -u)"
  for role in ma mb mc md; do
    for plist in "${HOME}/Library/LaunchAgents"/win.yxsbase.agentweb.*."${role}".plist; do
      [ -e "$plist" ] || continue
      base="$(basename "$plist" .plist)"
      case "$base" in
        "win.yxsbase.agentweb.${device_name}.${role}")
          ;;
        win.yxsbase.agentweb.*."${role}")
          launchctl bootout "gui/${uid}/${base}" >/dev/null 2>&1 || launchctl unload "$plist" >/dev/null 2>&1 || true
          rm -f "$plist"
          ;;
      esac
    done
  done
}

http_get() {
  url="$1"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- "$url"
  else
    return 1
  fi
}

wait_local_ready() {
  i=0
  while [ "$i" -lt 30 ]; do
    if http_get "http://127.0.0.1:17888/build-info" >/dev/null 2>&1; then
      return 0
    fi
    i=$((i + 1))
    sleep 1
  done
  log "AgentWeb local Ma did not become ready on http://127.0.0.1:17888/build-info"
  case "$os" in
    darwin)
      log "launchd labels:"
      launchctl list 2>/dev/null | grep "agentweb.${device_name}" >&2 || true
      log "recent logs:"
      tail -60 "${log_dir}/ma.out.log" >&2 2>/dev/null || true
      tail -60 "${log_dir}/ma.err.log" >&2 2>/dev/null || true
      ;;
    linux)
      log "systemd status:"
      (systemctl status "agentweb-${device_name}-ma.service" || systemctl --user status "agentweb-${device_name}-ma.service") >&2 2>/dev/null || true
      ;;
  esac
  return 1
}

case "$os" in
  linux)
    if command -v systemctl >/dev/null 2>&1 && [ "$(id -u)" = "0" ]; then
      svc_scope="system"
    elif command -v systemctl >/dev/null 2>&1; then
      svc_scope="user"
      loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true
    else
      fail "Linux install requires systemd/systemctl"
    fi
    if [ "$install_profile" = "dv" ]; then
      install_linux_systemd "$svc_scope" md
    else
      install_linux_systemd "$svc_scope" ma
      install_linux_systemd "$svc_scope" mb
      install_linux_systemd "$svc_scope" mc
    fi
    ;;
  darwin)
    cleanup_stale_macos_launchd
    if [ "$install_profile" = "dv" ]; then
      install_macos_launchd md
    else
      install_macos_launchd ma
      install_macos_launchd mb
      install_macos_launchd mc
    fi
    ;;
esac

wait_local_ready || fail "AgentWeb manager installation completed but local primary readiness check failed"

log "AgentWeb manager profile installed: ${install_profile}"
log "  home: ${agentweb_home}"
log "  binary: ${bin_path}"
log "  sha256: ${actual_sha}"
log "  device: ${device_name}"
log "  management domain: ${management_domain_id:-break-glass}"
log "  registration policy: ${registration_policy_id:-break-glass}"
log "  upstream count: $(printf '%s' "$remote_gws" | awk -F, '{print NF}')"
[ -z "$upstream_proxy" ] || log "  upstream proxy: inherited from installer environment"
