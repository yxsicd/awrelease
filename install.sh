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

valid_enrollment_device_name() {
  value="$1"
  [ -n "$value" ] || return 1
  [ "${#value}" -le 128 ] || return 1
  case "$value" in
    [A-Za-z0-9]* ) ;;
    * ) return 1 ;;
  esac
  case "$value" in
    *[!A-Za-z0-9._-]* ) return 1 ;;
  esac
}

usage() {
  cat >&2 <<'USAGE'
Usage: install.sh --enroll CLAIM_URL [--home PATH] [--instance NAME]
       install.sh [--gateway URL] [--device NAME] [--policy ID] [--profile production|dv] [--verify VERIFY] [--instance NAME]
       install.sh --device NAME --remote-gws URLS [--channel dev|main|prod] [--home PATH] [--instance NAME]

Environment variables:
  AGENTWEB_DEVICE_NAME   Stable topology device name. Prefer this over hostname.
  AGENTWEB_ENROLL_URL    Single-use manifest claim issued by an authorized gateway.
  AGENTWEB_SETUP_GATEWAY Required claim issuer origin when --gateway is omitted.
  AGENTWEB_SETUP_POLICY  Registration policy, default personal-default.
  AGENTWEB_SETUP_PROFILE Manager profile, default production.
  AGENTWEB_VERIFY         Shared gateway-access/runtime verify, default agentwebadmin.
  AGENTWEB_INSTANCE       Optional named instance. Unset or "default" preserves the legacy layout.
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
    --gateway)
      [ "$#" -ge 2 ] || fail "--gateway requires a value"
      AGENTWEB_SETUP_GATEWAY="$2"
      shift 2
      ;;
    --policy)
      [ "$#" -ge 2 ] || fail "--policy requires a value"
      AGENTWEB_SETUP_POLICY="$2"
      shift 2
      ;;
    --profile)
      [ "$#" -ge 2 ] || fail "--profile requires a value"
      AGENTWEB_SETUP_PROFILE="$2"
      shift 2
      ;;
    --basic)
      [ "$#" -ge 2 ] || fail "--basic requires a value"
      AGENTWEB_VERIFY="$2"
      shift 2
      ;;
    --verify)
      [ "$#" -ge 2 ] || fail "--verify requires a value"
      AGENTWEB_VERIFY="$2"
      shift 2
      ;;
    --instance)
      [ "$#" -ge 2 ] || fail "--instance requires a value"
      AGENTWEB_INSTANCE="$2"
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
  linux:x86_64|linux:amd64) artifact_key="linux-x64-musl"; expected_target="x86_64-unknown-linux-musl" ;;
  linux:aarch64|linux:arm64) artifact_key="linux-arm64-musl"; expected_target="aarch64-unknown-linux-musl" ;;
  darwin:arm64|darwin:aarch64) artifact_key="macos-arm64"; expected_target="aarch64-apple-darwin" ;;
  darwin:x86_64|darwin:amd64) fail "macOS x86_64 is not published in the prod channel yet" ;;
  *) fail "unsupported platform: $(uname -s 2>/dev/null || echo unknown) $(uname -m 2>/dev/null || echo unknown)" ;;
esac

need_cmd awk
need_cmd sed

channel="${AGENTWEB_CHANNEL:-prod}"
repo="${AGENTWEB_RELEASE_REPO:-yxsicd/awrelease}"
gateway_verify="${AGENTWEB_VERIFY:-agentwebadmin}"
case "$gateway_verify" in
  ""|*[!A-Za-z0-9._-]*) fail "--verify contains unsupported characters" ;;
esac
[ "${#gateway_verify}" -le 128 ] || fail "--verify is too long"
agentweb_home="${AGENTWEB_HOME:-${HOME}/.agentweb}"
instance_name="${AGENTWEB_INSTANCE:-}"
if [ "$instance_name" = "default" ]; then
  instance_name=""
fi
if [ -n "$instance_name" ]; then
  case "$instance_name" in
    *[!A-Za-z0-9._-]*) fail "--instance contains unsupported characters" ;;
  esac
  [ "${#instance_name}" -le 64 ] || fail "--instance is too long"
  install_root="${agentweb_home}/instances/${instance_name}"
else
  install_root="${agentweb_home}"
fi
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
bin_dir="${install_root}/bin"
env_dir="${install_root}/env.d"
state_dir="${install_root}/state"
log_dir="${install_root}/logs"
tmp_dir="${install_root}/tmp"
bin_path="${bin_dir}/agentgw"
manifest_path="${tmp_dir}/${channel}.json"
bootstrap_path="${tmp_dir}/bootstrap.$$.json"

mkdir -p "$bin_dir" "$env_dir" "$state_dir" "$log_dir" "$tmp_dir"

install_state="new"
existing_device=""
for existing_env in \
  "${install_root}/ma/config.env" \
  "${install_root}/mb/config.env" \
  "${install_root}/mc/config.env" \
  "${install_root}/md/config.env" \
  "${env_dir}"/*.env
do
  [ -f "$existing_env" ] || continue
  candidate_device="$(sed -n 's/^AGENTGW_DEVICE_NAME=//p' "$existing_env" | head -1 | tr -d '"' || true)"
  [ -n "$candidate_device" ] || continue
  if [ -n "$existing_device" ] && [ "$existing_device" != "$candidate_device" ]; then
    fail "existing role configs disagree on Device Name (${existing_device} != ${candidate_device})"
  fi
  existing_device="$candidate_device"
  install_state="repair"
done
if [ -e "$bin_path" ]; then
  install_state="repair"
fi
if [ -n "$existing_device" ] && [ -n "${AGENTWEB_DEVICE_NAME:-}" ] && [ "$existing_device" != "$AGENTWEB_DEVICE_NAME" ]; then
  fail "existing node Device Name ${existing_device} does not match requested ${AGENTWEB_DEVICE_NAME}; refusing to overwrite another node"
fi
if [ "$install_state" = "repair" ]; then
  log "AgentWeb repair: existing installation detected; preserving device and role node identities"
fi

prepared_bin=""
prepared_manifest_url=""
prepared_sha=""

prepare_release_artifact() {
  release_manifest_url="$1"
  [ -n "$release_manifest_url" ] || fail "gateway bootstrap has no release manifest URL"
  download "$release_manifest_url" "$manifest_path"
  download_url="$(json_value_for_artifact "$artifact_key" downloadUrl "$manifest_path" || true)"
  expected_sha="$(json_value_for_artifact "$artifact_key" sha256 "$manifest_path" || true)"
  artifact_target="$(json_value_for_artifact "$artifact_key" target "$manifest_path" || true)"
  [ -n "$download_url" ] || fail "manifest does not contain downloadUrl for ${artifact_key}: ${release_manifest_url}"
  [ -n "$expected_sha" ] || fail "manifest does not contain sha256 for ${artifact_key}: ${release_manifest_url}"
  [ "$artifact_target" = "$expected_target" ] || fail "manifest target mismatch for ${artifact_key}: expected ${expected_target}, got ${artifact_target:-missing}"
  prepared_bin="${tmp_dir}/agentgw.${artifact_key}.$$"
  rm -f "$prepared_bin"
  log "AgentWeb package preflight: downloading ${artifact_key} before consuming enrollment claim"
  download "$download_url" "$prepared_bin"
  actual_sha="$(sha256_file "$prepared_bin")"
  [ "$actual_sha" = "$expected_sha" ] || fail "sha256 mismatch for ${download_url}: expected ${expected_sha}, got ${actual_sha}"
  chmod 0755 "$prepared_bin"
  prepared_manifest_url="$release_manifest_url"
  prepared_sha="$actual_sha"
}

default_device="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo agentweb-node)"

if [ -z "${AGENTWEB_ENROLL_URL:-}" ] && [ -z "${AGENTWEB_REMOTE_GWS:-}" ]; then
  need_cmd curl
  setup_gateway="${AGENTWEB_SETUP_GATEWAY:-}"
  [ -n "$setup_gateway" ] || fail "--gateway or AGENTWEB_SETUP_GATEWAY is required"
  setup_gateway="${setup_gateway%/}"
  case "$setup_gateway" in
    http://*|https://*) ;;
    *) fail "--gateway must be an http:// or https:// origin" ;;
  esac
  case "$setup_gateway" in
    *[[:space:]\"\']*) fail "--gateway must not contain whitespace or quotes" ;;
  esac
  setup_policy="${AGENTWEB_SETUP_POLICY:-personal-default}"
  case "$setup_policy" in
    ""|*[!A-Za-z0-9._-]*) fail "--policy contains unsupported characters" ;;
  esac
  setup_profile="${AGENTWEB_SETUP_PROFILE:-production}"
  [ "$setup_profile" = "production" ] || [ "$setup_profile" = "dv" ] || fail "--profile must be production or dv"
  requested_device="${AGENTWEB_DEVICE_NAME:-$(sanitize_name "$default_device")}"
  valid_enrollment_device_name "$requested_device" || fail "--device contains unsupported enrollment identity characters"
  AGENTWEB_DEVICE_NAME="$requested_device"

  setup_info="${tmp_dir}/setup-info.$$.json"
  claim_request="${tmp_dir}/claim-request.$$.json"
  claim_response="${tmp_dir}/claim-response.$$.json"
  access_config="${tmp_dir}/gateway-access.$$.curl"
  download "${setup_gateway}/setup/api/bootstrap-info" "$setup_info"
  [ "$(json_value enrollment.configured "$setup_info" || true)" = "true" ] || fail "gateway enrollment is not configured"
  claim_endpoint="$(json_value enrollment.claimEndpoint "$setup_info" || true)"
  case "$claim_endpoint" in
    /*) ;;
    *) fail "gateway bootstrap has no origin-relative claimEndpoint" ;;
  esac
  setup_policies="$(json_array_csv enrollment.registrationPolicies "$setup_info" || true)"
  case ",${setup_policies}," in
    *,"${setup_policy}",*) ;;
    *) fail "gateway does not advertise registration policy ${setup_policy}" ;;
  esac
  setup_manifest_url="$(json_value release.manifestUrl "$setup_info" || true)"
  prepare_release_artifact "$setup_manifest_url"
  printf '{"deviceName":"%s","registrationPolicyId":"%s","profile":"%s"}\n' \
    "$requested_device" "$setup_policy" "$setup_profile" >"$claim_request"
  # This Authorization value admits curl through the gateway entrypoint. The
  # claim handler does not interpret it as a second enrollment authorization.
  printf 'user = "agentweb:%s"\n' "$gateway_verify" >"$access_config"
  log "AgentWeb enrollment: requesting a one-time claim from ${setup_gateway}"
  claim_status="$(curl --config "$access_config" -sS -o "$claim_response" -w '%{http_code}' \
    -H 'content-type: application/json' --data-binary "@$claim_request" \
    "${setup_gateway}${claim_endpoint}")"
  rm -f "$access_config" "$claim_request"
  [ "$claim_status" = "201" ] || {
    rm -f "$claim_response" "$setup_info"
    fail "claim request failed with HTTP ${claim_status}; verify gateway access and --verify"
  }
  [ "$(json_value ok "$claim_response" || true)" = "true" ] || fail "gateway rejected the claim request"
  AGENTWEB_ENROLL_URL="$(json_value claimUrl "$claim_response" || true)"
  rm -f "$claim_response" "$setup_info"
  [ -n "$AGENTWEB_ENROLL_URL" ] || fail "gateway claim response has no claimUrl"
fi

if [ -n "${AGENTWEB_ENROLL_URL:-}" ] && [ -z "$prepared_bin" ]; then
  claim_origin="$(printf '%s' "$AGENTWEB_ENROLL_URL" | sed -n 's#^\(https\{0,1\}://[^/]*\)/.*#\1#p')"
  [ -n "$claim_origin" ] || fail "--enroll must be an http:// or https:// claim URL"
  case "$claim_origin" in
    *[[:space:]\"\']*) fail "--enroll origin contains unsupported characters" ;;
  esac
  setup_info="${tmp_dir}/setup-info.$$.json"
  download "${claim_origin}/setup/api/bootstrap-info" "$setup_info"
  setup_manifest_url="$(json_value release.manifestUrl "$setup_info" || true)"
  rm -f "$setup_info"
  prepare_release_artifact "$setup_manifest_url"
fi

upstream_transport="auto"
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
  valid_enrollment_device_name "$claim_device" || fail "bootstrap manifest has an invalid deviceName"
  if [ -n "${AGENTWEB_DEVICE_NAME:-}" ] && [ "$AGENTWEB_DEVICE_NAME" != "$claim_device" ]; then
    fail "--device does not match the signed bootstrap manifest"
  fi
  AGENTWEB_DEVICE_NAME="$claim_device"
  remote_gws="$(json_array_csv remoteGws "$bootstrap_path" || true)"
  upstream_transport="$(json_value upstreamTransport "$bootstrap_path" || true)"
  management_domain_id="$(json_value managementDomainId "$bootstrap_path" || true)"
  registration_policy_id="$(json_value registrationPolicyId "$bootstrap_path" || true)"
  install_profile="$(json_value profile "$bootstrap_path" || true)"
  claim_gateway_verify="$(json_value gatewayVerify "$bootstrap_path" || true)"
  claim_channel="$(json_value release.channel "$bootstrap_path" || true)"
  claim_manifest_url="$(json_value release.manifestUrl "$bootstrap_path" || true)"
  [ -n "$remote_gws" ] || fail "bootstrap manifest has no remoteGws"
  [ -z "$upstream_transport" ] || [ "$upstream_transport" = "auto" ] || fail "unsupported bootstrap upstreamTransport"
  upstream_transport="${upstream_transport:-auto}"
  [ -n "$management_domain_id" ] || fail "bootstrap manifest has no managementDomainId"
  [ -n "$registration_policy_id" ] || fail "bootstrap manifest has no registrationPolicyId"
  [ "$install_profile" = "production" ] || [ "$install_profile" = "dv" ] || fail "unsupported bootstrap profile: $install_profile"
  [ -n "$claim_gateway_verify" ] || fail "bootstrap manifest has no gatewayVerify"
  case "$claim_gateway_verify" in
    *[!A-Za-z0-9._-]*) fail "bootstrap manifest has an invalid gatewayVerify" ;;
  esac
  [ "${#claim_gateway_verify}" -le 128 ] || fail "bootstrap manifest gatewayVerify is too long"
  gateway_verify="$claim_gateway_verify"
  claim_gateway_verify=""
  [ -z "$claim_channel" ] || channel="$claim_channel"
  manifest_url="${AGENTWEB_MANIFEST_URL:-$claim_manifest_url}"
  if [ -n "$prepared_manifest_url" ] && [ "$manifest_url" != "$prepared_manifest_url" ]; then
    fail "signed bootstrap release manifest differs from the package preflight"
  fi
  rm -f "$bootstrap_path"
else
  remote_gws="${AGENTWEB_REMOTE_GWS:-}"
  [ -n "$remote_gws" ] || fail "an enrollment claim is required; use --enroll CLAIM_URL (or explicit --remote-gws for break-glass recovery)"
  manifest_url="${AGENTWEB_MANIFEST_URL:-https://github.com/${repo}/releases/download/${channel}/agentgw-${channel}.json}"
fi
[ -n "$manifest_url" ] || fail "bootstrap manifest has no release manifest URL"
if [ -n "${AGENTWEB_ENROLL_URL:-}" ]; then
  device_name="$AGENTWEB_DEVICE_NAME"
elif [ -n "${AGENTWEB_DEVICE_NAME:-}" ]; then
  valid_enrollment_device_name "$AGENTWEB_DEVICE_NAME" || fail "--device contains unsupported enrollment identity characters"
  device_name="$AGENTWEB_DEVICE_NAME"
elif [ -n "$existing_device" ]; then
  device_name="$existing_device"
else
  device_name="$(sanitize_name "$default_device")"
fi
[ -n "$device_name" ] || device_name="agentweb_node"
if [ -n "$existing_device" ] && [ "$existing_device" != "$device_name" ]; then
  fail "signed Device Name ${device_name} does not match existing node ${existing_device}; refusing to overwrite another node"
fi
service_device_name="$device_name"
if [ -n "$instance_name" ]; then
  service_device_name="${device_name}-${instance_name}"
fi

log "AgentWeb install: channel=${channel} platform=${os}/${arch} artifact=${artifact_key}"
if [ -z "$prepared_bin" ]; then
  prepare_release_artifact "$manifest_url"
fi
actual_sha="$prepared_sha"
if [ -x "$bin_path" ]; then
  cp -p "$bin_path" "${bin_path}.bak.$(date -u +%Y%m%d%H%M%S)"
fi
mv "$prepared_bin" "$bin_path"
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
  role_dir="${install_root}/${role}"
  role_bin_dir="${role_dir}/bin"
  role_bin_path="${role_bin_dir}/agentgw"
  mkdir -p "$role_dir" "$role_bin_dir" "${state_dir}/${role}" "${role_dir}/dist"
  role_bin_incoming="${role_bin_path}.incoming.$$"
  cp "$bin_path" "$role_bin_incoming"
  chmod 0755 "$role_bin_incoming"
  if [ -x "$role_bin_path" ]; then
    cp -p "$role_bin_path" "${role_bin_path}.bak.$(date -u +%Y%m%d%H%M%S)"
  fi
  mv -f "$role_bin_incoming" "$role_bin_path"
  if [ ! -s "${role_dir}/node-id" ]; then
    new_runtime_id lgw >"${role_dir}/node-id"
  fi
  if [ ! -s "${agentweb_home}/device-id" ]; then
    new_runtime_id dev >"${agentweb_home}/device-id"
  fi
  node_id="$(tr -d '\r\n' <"${role_dir}/node-id")"
  cat >"${role_dir}/config.env" <<ENV
AGENTWEB_HOME=${install_root}
AGENTWEB_VERIFY=${gateway_verify}
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
AGENTGW_MANAGEMENT_DOMAIN_ID=${management_domain_id}
AGENTGW_REGISTRATION_POLICY_ID=${registration_policy_id}
AGENTGW_ENROLLMENT_PROFILE=${install_profile}
AGENTGW_MANAGER_DIR=${state_dir}/${role}/manager
AGENTGW_MANAGER_CHILD_ENV_FILES=
AGENTGW_DIST_DIR=${role_dir}/dist
AGENTGW_SELF_PATH=${role_bin_path}
AGENTGW_LOG_FORMAT=json
RUST_LOG=agentgw=info
ENV
  cp "${role_dir}/config.env" "${env_dir}/agentweb-${device_name}-${role}.env"
}

if [ -n "$instance_name" ]; then
  primary_listen=none
  primary_bind=none
else
  primary_listen=true
  primary_bind=127.0.0.1:17888
fi

if [ "$install_profile" = "dv" ]; then
  write_env md manager "$primary_listen" "$primary_bind" manager-validation Md
else
  write_env ma manager "$primary_listen" "$primary_bind" default-chrome Ma
  write_env mb manager none none manager-backup Mb
  write_env mc manager none none manager-backup-b Mc
  ma_bin="${install_root}/ma/bin/agentgw"
  mb_bin="${install_root}/mb/bin/agentgw"
  mc_bin="${install_root}/mc/bin/agentgw"
  for role_bin in "$ma_bin" "$mb_bin" "$mc_bin"; do
    [ -x "$role_bin" ] || fail "role binary is missing: ${role_bin}"
    [ "$(sha256_file "$role_bin")" = "$actual_sha" ] || fail "role binary sha256 mismatch: ${role_bin}"
  done
  ma_inode="$(ls -id "$ma_bin" | awk '{print $1}')"
  mb_inode="$(ls -id "$mb_bin" | awk '{print $1}')"
  mc_inode="$(ls -id "$mc_bin" | awk '{print $1}')"
  [ "$ma_inode" != "$mb_inode" ] && [ "$ma_inode" != "$mc_inode" ] && [ "$mb_inode" != "$mc_inode" ] || \
    fail "Ma/Mb/Mc role binaries must be physically independent files"
fi

install_linux_systemd() {
  scope="$1"
  role="$2"
  unit_name="agentweb-${service_device_name}-${role}.service"
  role_dir="${install_root}/${role}"
  role_bin_path="${role_dir}/bin/agentgw"
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
ExecStart=${role_bin_path}
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
  i=0
  while [ "$i" -lt 30 ]; do
    if $systemctl_cmd is-active --quiet "$unit_name"; then
      return 0
    fi
    i=$((i + 1))
    sleep 1
  done
  $systemctl_cmd status "$unit_name" >&2 2>/dev/null || true
  fail "AgentWeb role failed supervisor readiness: ${unit_name}"
}

install_macos_launchd() {
  role="$1"
  label="win.yxsbase.agentweb.${service_device_name}.${role}"
  plist_dir="${HOME}/Library/LaunchAgents"
  plist="${plist_dir}/${label}.plist"
  role_dir="${install_root}/${role}"
  role_bin_path="${role_dir}/bin/agentgw"
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
    <string>set -a; . '${role_dir}/config.env'; set +a; exec '${role_bin_path}'</string>
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
      grep -Fq "'${install_root}/${role}/config.env'" "$plist" || continue
      base="$(basename "$plist" .plist)"
      case "$base" in
        "win.yxsbase.agentweb.${service_device_name}.${role}")
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
      (systemctl status "agentweb-${service_device_name}-ma.service" || systemctl --user status "agentweb-${service_device_name}-ma.service") >&2 2>/dev/null || true
      ;;
  esac
  return 1
}

wait_named_instance_ready() {
  role=ma
  [ "$install_profile" = "dv" ] && role=md
  case "$os" in
    linux)
      unit="agentweb-${service_device_name}-${role}.service"
      i=0
      while [ "$i" -lt 30 ]; do
        if systemctl is-active --quiet "$unit" 2>/dev/null || systemctl --user is-active --quiet "$unit" 2>/dev/null; then
          return 0
        fi
        i=$((i + 1))
        sleep 1
      done
      ;;
    darwin)
      label="win.yxsbase.agentweb.${service_device_name}.${role}"
      i=0
      while [ "$i" -lt 30 ]; do
        if launchctl print "gui/$(id -u)/${label}" >/dev/null 2>&1; then
          return 0
        fi
        i=$((i + 1))
        sleep 1
      done
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
      install_linux_systemd "$svc_scope" mb
      install_linux_systemd "$svc_scope" mc
      install_linux_systemd "$svc_scope" ma
    fi
    ;;
  darwin)
    cleanup_stale_macos_launchd
    if [ "$install_profile" = "dv" ]; then
      install_macos_launchd md
    else
      install_macos_launchd mb
      install_macos_launchd mc
      install_macos_launchd ma
    fi
    ;;
esac

if [ -n "$instance_name" ]; then
  wait_named_instance_ready || fail "AgentWeb named instance installation completed but supervisor readiness check failed"
else
  wait_local_ready || fail "AgentWeb manager installation completed but local primary readiness check failed"
fi

log "AgentWeb manager profile installed: ${install_profile}"
log "  operation: ${install_state}"
log "  home: ${install_root}"
[ -z "$instance_name" ] || log "  instance: ${instance_name}"
log "  binary: ${bin_path}"
[ "$install_profile" = "dv" ] || log "  role binaries: ma/mb/mc independent files"
log "  sha256: ${actual_sha}"
log "  device: ${device_name}"
log "  management domain: ${management_domain_id:-break-glass}"
log "  registration policy: ${registration_policy_id:-break-glass}"
log "  upstream count: $(printf '%s' "$remote_gws" | awk -F, '{print NF}')"
[ -z "$upstream_proxy" ] || log "  upstream proxy: inherited from installer environment"
