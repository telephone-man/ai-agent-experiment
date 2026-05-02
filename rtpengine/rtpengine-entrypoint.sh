#!/bin/sh
set -e

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
  RTPENGINE_LOCAL_IP \
  RTPENGINE_LISTEN_CLI_IP \
  RTPENGINE_LISTEN_NG_IP \
  RTPENGINE_INTERFACE_INTERNAL_BIND_IP \
  RTPENGINE_INTERFACE_EXTERNAL_BIND_IP \
  RTPENGINE_INTERFACE_EXTERNAL_ADVERTISE_IP \
  RTPENGINE_RTP_PORT_START \
  RTPENGINE_RTP_PORT_END \
  RTPENGINE_LOG_LEVEL \
  RTPENGINE_LOG_LEVEL_SUBSYSTEM
load_config_file
restore_env_overrides

set_if_unset_or_any() {
  var_name="$1"
  default_value="$2"

  eval current_val=\${$var_name:-}
  if [ -z "$current_val" ] || [ "$current_val" = "0.0.0.0" ]; then
    eval "$var_name=\"$default_value\""
    export "$var_name"
  fi
}

discover_default_route_src_ip() {
  local ip
  ip=""
  if command -v ip >/dev/null 2>&1; then
    ip=$(
      ip -4 route get 1.1.1.1 2>/dev/null \
        | awk '{for (i=1; i<=NF; i++) if ($i=="src") {print $(i+1); exit}}' \
        || true
    )
  fi
  echo "$ip"
}

discover_first_global_ipv4() {
  local ip
  ip=""
  if command -v ip >/dev/null 2>&1; then
    ip=$(
      ip -4 -o addr show scope global 2>/dev/null \
        | awk '{split($4,a,"/"); print a[1]; exit}' \
        || true
    )
  fi
  if [ -z "$ip" ]; then
    ip=$(hostname -i 2>/dev/null | awk '{print $1}' || true)
  fi
  echo "$ip"
}

discover_internal_ipv4_excluding() {
  local exclude_ip ip
  exclude_ip="${1:-}"
  ip=""

  if command -v ip >/dev/null 2>&1; then
    ip=$(
      ip -4 -o addr show scope global 2>/dev/null \
        | awk -v ext="$exclude_ip" '{split($4,a,"/"); if (a[1] != ext) {print a[1]; exit}}' \
        || true
    )
  fi

  echo "$ip"
}

AUTO_EXTERNAL_IP=$(discover_default_route_src_ip)
AUTO_EXTERNAL_IP=${AUTO_EXTERNAL_IP:-$(discover_first_global_ipv4)}

AUTO_INTERNAL_IP=$(discover_internal_ipv4_excluding "$AUTO_EXTERNAL_IP")
AUTO_INTERNAL_IP=${AUTO_INTERNAL_IP:-$(discover_first_global_ipv4)}

AUTO_INTERNAL_IP=${AUTO_INTERNAL_IP:-127.0.0.1}
AUTO_EXTERNAL_IP=${AUTO_EXTERNAL_IP:-$AUTO_INTERNAL_IP}

RESOLVED_INTERNAL_IP="${RTPENGINE_INTERFACE_INTERNAL_BIND_IP:-}"
if [ -z "$RESOLVED_INTERNAL_IP" ] || [ "$RESOLVED_INTERNAL_IP" = "0.0.0.0" ]; then
  RESOLVED_INTERNAL_IP="${RTPENGINE_LISTEN_NG_IP:-}"
fi
if [ -z "$RESOLVED_INTERNAL_IP" ] || [ "$RESOLVED_INTERNAL_IP" = "0.0.0.0" ]; then
  RESOLVED_INTERNAL_IP="${RTPENGINE_LOCAL_IP:-}"
fi
if [ -z "$RESOLVED_INTERNAL_IP" ] || [ "$RESOLVED_INTERNAL_IP" = "0.0.0.0" ]; then
  RESOLVED_INTERNAL_IP="$AUTO_INTERNAL_IP"
fi

RESOLVED_EXTERNAL_IP="${RTPENGINE_INTERFACE_EXTERNAL_BIND_IP:-}"
if [ -z "$RESOLVED_EXTERNAL_IP" ] || [ "$RESOLVED_EXTERNAL_IP" = "0.0.0.0" ]; then
  RESOLVED_EXTERNAL_IP="$AUTO_EXTERNAL_IP"
fi
RESOLVED_EXTERNAL_IP=${RESOLVED_EXTERNAL_IP:-$RESOLVED_INTERNAL_IP}

set_if_unset_or_any RTPENGINE_LOCAL_IP "$RESOLVED_INTERNAL_IP"
set_if_unset_or_any RTPENGINE_LISTEN_NG_IP "$RESOLVED_INTERNAL_IP"
set_if_unset_or_any RTPENGINE_INTERFACE_INTERNAL_BIND_IP "$RESOLVED_INTERNAL_IP"
set_if_unset_or_any RTPENGINE_INTERFACE_EXTERNAL_BIND_IP "$RESOLVED_EXTERNAL_IP"
RTPENGINE_LOG_LEVEL=${RTPENGINE_LOG_LEVEL:-6}
RTPENGINE_LOG_LEVEL_SUBSYSTEM=${RTPENGINE_LOG_LEVEL_SUBSYSTEM:-core:6;control:6;crypto:6;srtp:6;rtcp:6;ice:6;internals:0}

echo "Starting RTPengine entrypoint script..."
echo "Resolved bind IPs: internal=${RTPENGINE_INTERFACE_INTERNAL_BIND_IP} external=${RTPENGINE_INTERFACE_EXTERNAL_BIND_IP}"

cp /etc/rtpengine.conf.template /etc/rtpengine/rtpengine.conf

sed -i -e "s|{{RTPENGINE_RTP_PORT_START}}|${RTPENGINE_RTP_PORT_START}|g" /etc/rtpengine/rtpengine.conf
sed -i -e "s|{{RTPENGINE_RTP_PORT_END}}|${RTPENGINE_RTP_PORT_END}|g" /etc/rtpengine/rtpengine.conf
sed -i -e "s|{{RTPENGINE_LISTEN_CLI_IP}}|${RTPENGINE_LISTEN_CLI_IP}|g" /etc/rtpengine/rtpengine.conf
sed -i -e "s|{{RTPENGINE_LISTEN_NG_IP}}|${RTPENGINE_LISTEN_NG_IP}|g" /etc/rtpengine/rtpengine.conf
sed -i -e "s|{{RTPENGINE_INTERFACE_INTERNAL_BIND_IP}}|${RTPENGINE_INTERFACE_INTERNAL_BIND_IP}|g" /etc/rtpengine/rtpengine.conf
sed -i -e "s|{{RTPENGINE_INTERFACE_EXTERNAL_BIND_IP}}|${RTPENGINE_INTERFACE_EXTERNAL_BIND_IP}|g" /etc/rtpengine/rtpengine.conf
sed -i -e "s|{{RTPENGINE_INTERFACE_EXTERNAL_ADVERTISE_IP}}|${RTPENGINE_INTERFACE_EXTERNAL_ADVERTISE_IP}|g" /etc/rtpengine/rtpengine.conf
sed -i -e "s|{{RTPENGINE_LOG_LEVEL}}|${RTPENGINE_LOG_LEVEL}|g" /etc/rtpengine/rtpengine.conf
sed -i -e "s|{{RTPENGINE_LOG_LEVEL_SUBSYSTEM}}|${RTPENGINE_LOG_LEVEL_SUBSYSTEM}|g" /etc/rtpengine/rtpengine.conf

echo "Starting RTPengine..."
exec /usr/bin/rtpengine --config-file=/etc/rtpengine/rtpengine.conf
