#!/bin/sh
set -e

RTPENGINE_SOCK_OVERRIDE=${RTPENGINE_SOCK:-}

load_config_file() {
  if [ "${CONFIG_FILE_LOADED:-}" = "1" ]; then
    return
  fi

  if [ -z "${CONFIG_FILE:-}" ]; then
    echo "CONFIG_FILE is not set. Mount the shared env file (e.g. /conf/env) and set CONFIG_FILE to its path." >&2
    exit 1
  fi

  if [ ! -r "${CONFIG_FILE}" ]; then
    echo "CONFIG_FILE points to '${CONFIG_FILE}', but the file is missing or unreadable." >&2
    exit 1
  fi

  set -a
  # shellcheck disable=SC1090
  . "${CONFIG_FILE}"
  set +a

  export CONFIG_FILE_LOADED=1
}

save_env_overrides() {
  SAVED_ENV_KEYS=""
  for var_name in "$@"; do
    eval "is_set=\${$var_name+x}"
    if [ "$is_set" = "x" ]; then
      eval "saved_value=\${$var_name}"
      eval "SAVED_$var_name=\$saved_value"
      SAVED_ENV_KEYS="${SAVED_ENV_KEYS} ${var_name}"
    fi
  done
}

restore_env_overrides() {
  for var_name in $SAVED_ENV_KEYS; do
    eval "saved_value=\${SAVED_$var_name}"
    eval "$var_name=\$saved_value"
    export "$var_name"
  done
}

save_env_overrides \
  KAMAILIO_DB_SUPERUSER \
  KAMAILIO_DB_SUPERUSER_PASS \
  KAMAILIO_DB_RW_USER \
  KAMAILIO_DB_RW_PASS \
  KAMAILIO_DB_RO_USER \
  KAMAILIO_DB_RO_PASS \
  KAMAILIO_DB_HOSTNAME \
  KAMAILIO_DB_NAME \
  KAMAILIO_LISTEN_IP \
  KAMAILIO_ADVERTISED_IP \
  KAMAILIO_EXTERNAL_SIP_PORT \
  KAMAILIO_EXTERNAL_ADVERTISED_PORT \
  KAMAILIO_EXTERNAL_WEBRTC_PORT \
  KAMAILIO_INTERNAL_SIP_PORT \
  KAMAILIO_INTERNAL_WEBRTC_PORT \
  KAMAILIO_DEBUG_LEVEL \
  KAMAILIO_SIP_BODY_LOGGING \
  KAMAILIO_SIPDUMP_ENABLE \
  KAMAILIO_SIPTRACE_ENABLE \
  RTPENGINE_SOCK
load_config_file
restore_env_overrides

# Allow container/runtime wiring to override the shared env file when needed.
if [ -n "${RTPENGINE_SOCK_OVERRIDE:-}" ]; then
  RTPENGINE_SOCK="${RTPENGINE_SOCK_OVERRIDE}"
  export RTPENGINE_SOCK
fi

discover_primary_ip() {
  local ip
  ip=$(hostname -i 2>/dev/null || true)
  ip=$(printf '%s\n' "$ip" | awk '{print $1}')
  if [ -z "$ip" ] || [ "$ip" = "127.0.0.1" ]; then
    if command -v ip >/dev/null 2>&1; then
      ip=$(ip -4 route get 1 2>/dev/null | awk '/src/ {print $7; exit}' || true)
    fi
  fi
  echo "$ip"
}

wait_for_dns() {
  local name
  local timeout_seconds
  local attempts
  local i
  name=$1
  timeout_seconds=${2:-2}

  if ! command -v getent >/dev/null 2>&1; then
    echo "getent not available; skipping DNS wait for ${name}"
    return 0
  fi

  attempts=$((timeout_seconds * 2))
  if [ "$attempts" -lt 1 ]; then
    attempts=1
  fi

  i=0
  while [ "$i" -lt "$attempts" ]; do
    if getent hosts "$name" >/dev/null 2>&1; then
      echo "DNS resolved for ${name}"
      return 0
    fi
    i=$((i + 1))
    sleep 0.5
  done

  echo "Timed out waiting for DNS for ${name} after ${timeout_seconds}s."
  return 1
}

is_truthy() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

progress_log() {
  if is_truthy "${KAMAILIO_VALIDATE_ONLY:-0}" || is_truthy "${KAMAILIO_VALIDATE_PROGRESS:-0}"; then
    printf '[kamailio-validate] %s\n' "$*"
  else
    printf '%s\n' "$*"
  fi
}

require_env() {
  eval "value=\${$1:-}"
  if [ -z "$value" ]; then
    echo "$1 is required. Run ./setup.sh to generate local demo secrets." >&2
    exit 1
  fi
}

extract_rtpengine_hosts() {
  local sock
  local host
  printf '%s\n' "${RTPENGINE_SOCK:-}" | tr ' ' '\n' | while read -r sock; do
    [ -z "$sock" ] && continue
    sock=${sock%%;*}
    sock=${sock%%=*}
    case "$sock" in
      unix:*) continue ;;
    esac

    sock=${sock#*:}
    [ -z "$sock" ] && continue

    if [ "${sock#\[}" != "$sock" ]; then
      host=${sock%%]*}
      host=${host#\[}
    else
      case "$sock" in
        *:*) host=${sock%:*} ;;
        *) host=$sock ;;
      esac
    fi

    [ -n "$host" ] && printf '%s\n' "$host"
  done | awk 'NF && !seen[$0]++ {print}'
}

print_rtpengine_dns_diagnostics() {
  local resolved_hosts
  local failed_hosts
  resolved_hosts=$1
  failed_hosts=$2

  echo "RTPENGINE_SOCK=${RTPENGINE_SOCK}"
  echo "Failed RTPEngine hostnames:${failed_hosts}"
  if [ -r /etc/resolv.conf ]; then
    echo "=== /etc/resolv.conf ==="
    cat /etc/resolv.conf
    echo "========================"
  else
    echo "/etc/resolv.conf is not readable."
  fi

  if [ -n "$resolved_hosts" ]; then
    echo "getent hosts for resolved RTPEngine names:"
    for host in $resolved_hosts; do
      getent hosts "$host" || true
    done
  fi
}

validate_rtpengine_dns() {
  local timeout_seconds
  local strict_mode
  local hosts
  local resolved_hosts
  local failed_hosts
  local host

  timeout_seconds=${DNS_WAIT_TIMEOUT:-2}
  strict_mode=${RTPENGINE_DNS_STRICT:-1}

  if ! command -v getent >/dev/null 2>&1; then
    echo "getent not available; skipping RTPEngine DNS validation."
    return 0
  fi

  hosts=$(extract_rtpengine_hosts)
  if [ -z "$hosts" ]; then
    echo "No RTPEngine hostnames found in RTPENGINE_SOCK; skipping DNS validation."
    return 0
  fi

  echo "Validating RTPEngine DNS (${timeout_seconds}s per host)..."
  for host in $hosts; do
    if wait_for_dns "$host" "$timeout_seconds"; then
      resolved_hosts="${resolved_hosts} ${host}"
    else
      failed_hosts="${failed_hosts} ${host}"
    fi
  done

  if [ -n "$failed_hosts" ]; then
    echo "RTPEngine DNS resolution failed:${failed_hosts}"
    if is_truthy "$strict_mode"; then
      print_rtpengine_dns_diagnostics "$resolved_hosts" "$failed_hosts"
      exit 1
    fi
    echo "Continuing because RTPENGINE_DNS_STRICT=${strict_mode}."
  fi
}

# This script waits for Postgres, sets up the database with kamdbctl, and then
# starts Kamailio.

KAMAILIO_DB_SUPERUSER=${KAMAILIO_DB_SUPERUSER:-kamailio}
KAMAILIO_DB_RW_USER=${KAMAILIO_DB_RW_USER:-kamailio_rw}
KAMAILIO_DB_RO_USER=${KAMAILIO_DB_RO_USER:-kamailio_ro}
KAMAILIO_DB_HOSTNAME=${KAMAILIO_DB_HOSTNAME:-kamailio_db}
KAMAILIO_DB_NAME=${KAMAILIO_DB_NAME:-kamailio}
require_env KAMAILIO_DB_SUPERUSER_PASS
require_env KAMAILIO_DB_RW_PASS
require_env KAMAILIO_DB_RO_PASS

# Provide sensible defaults so the config can be rendered even if vars are missing.
KAMAILIO_EXTERNAL_SIP_PORT=${KAMAILIO_EXTERNAL_SIP_PORT:-5067}
KAMAILIO_EXTERNAL_ADVERTISED_PORT=${KAMAILIO_EXTERNAL_ADVERTISED_PORT:-5060}
KAMAILIO_EXTERNAL_WEBRTC_PORT=${KAMAILIO_EXTERNAL_WEBRTC_PORT:-5066}
KAMAILIO_INTERNAL_SIP_PORT=${KAMAILIO_INTERNAL_SIP_PORT:-5060}
KAMAILIO_INTERNAL_WEBRTC_PORT=${KAMAILIO_INTERNAL_WEBRTC_PORT:-5068}
KAMAILIO_DEBUG_LEVEL=${KAMAILIO_DEBUG_LEVEL:-0}
KAMAILIO_SIP_BODY_LOGGING=${KAMAILIO_SIP_BODY_LOGGING:-0}
KAMAILIO_SIPDUMP_ENABLE=${KAMAILIO_SIPDUMP_ENABLE:-0}
KAMAILIO_SIPTRACE_ENABLE=${KAMAILIO_SIPTRACE_ENABLE:-0}
if [ -z "${RTPENGINE_SOCK:-}" ]; then
  RTPENGINE_SOCK="udp:rtpengine:2223"
fi

export KAMAILIO_EXTERNAL_SIP_PORT
export KAMAILIO_EXTERNAL_ADVERTISED_PORT
export KAMAILIO_EXTERNAL_WEBRTC_PORT
export KAMAILIO_INTERNAL_SIP_PORT
export KAMAILIO_INTERNAL_WEBRTC_PORT
export KAMAILIO_DEBUG_LEVEL
export KAMAILIO_SIP_BODY_LOGGING
export KAMAILIO_SIPDUMP_ENABLE
export KAMAILIO_SIPTRACE_ENABLE
export RTPENGINE_SOCK
export VOIPNET_SUBNET

PRIMARY_IP=${KAMAILIO_LOCAL_IP:-$(discover_primary_ip)}
if [ -z "$PRIMARY_IP" ] || [ "$PRIMARY_IP" = "0.0.0.0" ]; then
  PRIMARY_IP=$(discover_primary_ip)
fi
PRIMARY_IP=${PRIMARY_IP:-127.0.0.1}
KAMAILIO_LOCAL_IP=$PRIMARY_IP
KAMAILIO_LISTEN_IP=${KAMAILIO_LISTEN_IP:-0.0.0.0}
VOIPNET_SUBNET=${VOIPNET_SUBNET:-192.168.144.0/20}

progress_log "Kamailio resolved local IP: ${KAMAILIO_LOCAL_IP}"
progress_log "Kamailio listen IP: ${KAMAILIO_LISTEN_IP}"

progress_log "Preparing runtime directories..."
DEFAULT_RUNTIME_DIR=${KAMAILIO_RUNTIME_DIR:-/var/run/kamailio}
RUNTIME_DIR="${DEFAULT_RUNTIME_DIR}"
if ! mkdir -p "${RUNTIME_DIR}"; then
  progress_log "Unable to create ${RUNTIME_DIR}; falling back to /tmp/kamailio"
  RUNTIME_DIR=/tmp/kamailio
  mkdir -p "${RUNTIME_DIR}"
fi
KAMAILIO_RUNTIME_DIR="${RUNTIME_DIR}"
KAMAILIO_CFG_TEMPLATE=/etc/kamailio/kamailio.cfg
KAMAILIO_CFG_RUNTIME=${KAMAILIO_RUNTIME_DIR}/kamailio.cfg
PGPASS_RUNTIME_FILE=${KAMAILIO_RUNTIME_DIR}/.pgpass

apply_validation_defaults() {
  KAMAILIO_ADVERTISED_IP=${KAMAILIO_ADVERTISED_IP:-203.0.113.10}

  export KAMAILIO_ADVERTISED_IP
}

render_kamailio_config() {
  local render_target

  progress_log "Preparing runtime Kamailio config from template..."
  rm -rf "${KAMAILIO_RUNTIME_DIR}/config"
  mkdir -p "${KAMAILIO_RUNTIME_DIR}/config"
  cp -R /etc/kamailio/config/. "${KAMAILIO_RUNTIME_DIR}/config/"
  cp "${KAMAILIO_CFG_TEMPLATE}" "${KAMAILIO_CFG_RUNTIME}"

  progress_log "Replacing env vars in Kamailio config..."
  RTPENGINE_SOCK_ESCAPED=$(printf '%s' "${RTPENGINE_SOCK}" | sed -e 's/[|&]/\\&/g')
  DBURL_ESCAPED=$(printf 'postgres://%s:%s@%s/%s' "$KAMAILIO_DB_RW_USER" "$KAMAILIO_DB_RW_PASS" "$KAMAILIO_DB_HOSTNAME" "$KAMAILIO_DB_NAME" | sed -e 's/[|&]/\\&/g')
  for render_target in "${KAMAILIO_CFG_RUNTIME}" "${KAMAILIO_RUNTIME_DIR}"/config/*.cfg; do
    sed -i -e "s|{{KAMAILIO_ADVERTISED_IP}}|${KAMAILIO_ADVERTISED_IP}|g" "${render_target}"
    sed -i -e "s|{{KAMAILIO_LISTEN_IP}}|${KAMAILIO_LISTEN_IP}|g" "${render_target}"
    sed -i -e "s|{{KAMAILIO_LOCAL_IP}}|${KAMAILIO_LOCAL_IP}|g" "${render_target}"
    sed -i -e "s|{{KAMAILIO_EXTERNAL_SIP_PORT}}|${KAMAILIO_EXTERNAL_SIP_PORT}|g" "${render_target}"
    sed -i -e "s|{{KAMAILIO_EXTERNAL_ADVERTISED_PORT}}|${KAMAILIO_EXTERNAL_ADVERTISED_PORT}|g" "${render_target}"
    sed -i -e "s|{{KAMAILIO_EXTERNAL_WEBRTC_PORT}}|${KAMAILIO_EXTERNAL_WEBRTC_PORT}|g" "${render_target}"
    sed -i -e "s|{{KAMAILIO_INTERNAL_SIP_PORT}}|${KAMAILIO_INTERNAL_SIP_PORT}|g" "${render_target}"
    sed -i -e "s|{{KAMAILIO_INTERNAL_WEBRTC_PORT}}|${KAMAILIO_INTERNAL_WEBRTC_PORT}|g" "${render_target}"
    sed -i -e "s|{{KAMAILIO_DEBUG_LEVEL}}|${KAMAILIO_DEBUG_LEVEL}|g" "${render_target}"
    sed -i -e "s|{{KAMAILIO_SIP_BODY_LOGGING}}|${KAMAILIO_SIP_BODY_LOGGING}|g" "${render_target}"
    sed -i -e "s|{{KAMAILIO_SIPDUMP_ENABLE}}|${KAMAILIO_SIPDUMP_ENABLE}|g" "${render_target}"
    sed -i -e "s|{{KAMAILIO_SIPTRACE_ENABLE}}|${KAMAILIO_SIPTRACE_ENABLE}|g" "${render_target}"
    sed -i -e "s|{{RTPENGINE_SOCK}}|${RTPENGINE_SOCK_ESCAPED}|g" "${render_target}"
    sed -i -e "s|{{VOIPNET_SUBNET}}|${VOIPNET_SUBNET}|g" "${render_target}"
    sed -i -e "s|#!trydef DBURL \"postgres://KAMAILIO_DB_RO_USER:KAMAILIO_DB_RO_PASS@kamailio_db/kamailio\"|#!trydef DBURL \"${DBURL_ESCAPED}\"|g" "${render_target}"
  done

  progress_log "Rendered config summary:"
  grep listen "${KAMAILIO_CFG_RUNTIME}" || true
  grep rtpengine_sock "${KAMAILIO_CFG_RUNTIME}" || true
  progress_log "End rendered config summary"

  if ! is_truthy "${KAMAILIO_VALIDATE_ONLY:-0}"; then
    echo "Starting Kamailio with the following environment variables:"
    echo "  KAMAILIO_ADVERTISED_IP = ${KAMAILIO_ADVERTISED_IP}"
    echo "  KAMAILIO_LOCAL_IP = ${KAMAILIO_LOCAL_IP}"
    echo "  KAMAILIO_EXTERNAL_SIP_PORT = ${KAMAILIO_EXTERNAL_SIP_PORT}"
    echo "  KAMAILIO_EXTERNAL_ADVERTISED_PORT = ${KAMAILIO_EXTERNAL_ADVERTISED_PORT}"
    echo "  KAMAILIO_EXTERNAL_WEBRTC_PORT = ${KAMAILIO_EXTERNAL_WEBRTC_PORT}"
    echo "  KAMAILIO_INTERNAL_SIP_PORT = ${KAMAILIO_INTERNAL_SIP_PORT}"
    echo "  KAMAILIO_INTERNAL_WEBRTC_PORT = ${KAMAILIO_INTERNAL_WEBRTC_PORT}"
  fi
}

validate_kamailio_config() {
  progress_log "Running kamailio -c"
  kamailio -c -f "${KAMAILIO_CFG_RUNTIME}"
  progress_log "Kamailio config validation passed."
}

export_kamdbctl_config() {
  DBENGINE=PGSQL
  DBHOST="${KAMAILIO_DB_HOSTNAME}"
  DBPORT=5432
  DBNAME="${KAMAILIO_DB_NAME}"
  DBRWUSER="${KAMAILIO_DB_RW_USER}"
  DBRWPW="${KAMAILIO_DB_RW_PASS}"
  DBROUSER="${KAMAILIO_DB_RO_USER}"
  DBROPW="${KAMAILIO_DB_RO_PASS}"
  DBROOTUSER="${KAMAILIO_DB_SUPERUSER}"
  DBROOTHOST="${KAMAILIO_DB_HOSTNAME}"
  DBROOTPORT=5432
  INSTALL_EXTRA_TABLES=y
  INSTALL_PRESENCE_TABLES=y
  INSTALL_DBUID_TABLES=y

  export DBENGINE DBHOST DBPORT DBNAME
  export DBRWUSER DBRWPW DBROUSER DBROPW
  export DBROOTUSER DBROOTHOST DBROOTPORT
  export INSTALL_EXTRA_TABLES INSTALL_PRESENCE_TABLES INSTALL_DBUID_TABLES
}

if is_truthy "${KAMAILIO_VALIDATE_ONLY:-0}"; then
  progress_log "KAMAILIO_VALIDATE_ONLY enabled; rendering and validating config without DB/bootstrap."
  apply_validation_defaults
  render_kamailio_config
  validate_kamailio_config
  exit 0
fi

echo "Waiting for Postgres (superuser check)..."
until PGPASSWORD="$KAMAILIO_DB_SUPERUSER_PASS" psql -h "$KAMAILIO_DB_HOSTNAME" -U "$KAMAILIO_DB_SUPERUSER" -d "postgres" -c '\q' 2>/dev/null; do
  sleep 2
done
echo "Postgres is ready."

# Check if the Kamailio database exists
DB_EXISTS=$(PGPASSWORD="$KAMAILIO_DB_SUPERUSER_PASS" psql -h "$KAMAILIO_DB_HOSTNAME" -U "$KAMAILIO_DB_SUPERUSER" -d "postgres" -tAc "SELECT 1 FROM pg_database WHERE datname='$KAMAILIO_DB_NAME'")

if [ "$DB_EXISTS" = "1" ]; then
  echo "Database '$KAMAILIO_DB_NAME' exists. Checking for Kamailio schema..."
  
  # Check if the 'version' table exists (indicates Kamailio schema is present)
  TABLE_EXISTS=$(PGPASSWORD="$KAMAILIO_DB_SUPERUSER_PASS" psql -h "$KAMAILIO_DB_HOSTNAME" -U "$KAMAILIO_DB_SUPERUSER" -d "$KAMAILIO_DB_NAME" -tAc "SELECT 1 FROM information_schema.tables WHERE table_name='version'")
  
  if [ "$TABLE_EXISTS" = "1" ]; then
    echo "Kamailio schema already present in '$KAMAILIO_DB_NAME', skipping initialization."
  else
    echo "Database '$KAMAILIO_DB_NAME' exists but has no Kamailio schema; refusing to modify it automatically." >&2
    echo "Remove or migrate the database manually, then restart Kamailio." >&2
    exit 1
  fi
fi

if [ "$DB_EXISTS" != "1" ]; then
  echo "Creating Kamailio database..."
  
  # Create a .pgpass file for kamdbctl to authenticate as the postgres superuser without prompt
  # Format: hostname:port:database:username:password
  echo "$KAMAILIO_DB_HOSTNAME:5432:*:$KAMAILIO_DB_SUPERUSER:$KAMAILIO_DB_SUPERUSER_PASS" > "${PGPASS_RUNTIME_FILE}"
  chmod 600 "${PGPASS_RUNTIME_FILE}"
  export PGPASSFILE="${PGPASS_RUNTIME_FILE}"

  export_kamdbctl_config
  kamdbctl create
fi

DNS_WAIT_TIMEOUT=${KAMAILIO_DNS_WAIT_TIMEOUT:-2}
wait_for_dns "freeswitch" "${DNS_WAIT_TIMEOUT}" || true
validate_rtpengine_dns

render_kamailio_config
validate_kamailio_config

term_handler() {
  if [ -n "${KAMAILIO_PID:-}" ] && kill -0 "$KAMAILIO_PID" 2>/dev/null; then
    kill -TERM "$KAMAILIO_PID"
    wait "$KAMAILIO_PID" || true
  fi
  exit 0
}

trap term_handler INT TERM

echo "Starting Kamailio..."
kamailio -DD -E -f "${KAMAILIO_CFG_RUNTIME}" &
KAMAILIO_PID=$!

wait "$KAMAILIO_PID"
