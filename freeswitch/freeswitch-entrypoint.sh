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

discover_ip_for_interface() {
  local iface="$1"
  if [ -z "$iface" ] || ! command -v ip >/dev/null 2>&1; then
    return
  fi
  # Pick the first non-loopback IPv4 on the interface.
  ip -4 -o addr show dev "$iface" scope global 2>/dev/null | awk '{split($4,a,"/"); if (a[1] != "") {print a[1]; exit}}'
}

discover_ip_for_cidr() {
  local cidr="$1"
  if [ -z "$cidr" ] || ! command -v ip >/dev/null 2>&1; then
    return
  fi
  ip -4 -o addr show scope global | while read -r idx iface fam addr _rest; do
    [ "$fam" != "inet" ] && continue
    local candidate=${addr%/*}
    [ -z "$candidate" ] && continue
    if python3 - "$cidr" "$candidate" >/dev/null 2>&1 <<'PY'; then
import ipaddress, sys
cidr = sys.argv[1]
ip = sys.argv[2]
sys.exit(0 if ipaddress.IPv4Address(ip) in ipaddress.IPv4Network(cidr, strict=False) else 1)
PY
      echo "$candidate"
      break
    fi
  done
}

discover_primary_ip() {
  local ip

  # 1) Explicit interface override.
  ip=$(discover_ip_for_interface "${FREESWITCH_BIND_INTERFACE:-}")
  if [ -n "$ip" ]; then
    echo "$ip"
    return
  fi

  # 2) Match a CIDR if provided (fallback to VOIPNET_SUBNET).
  ip=$(discover_ip_for_cidr "${FREESWITCH_PREFERRED_CIDR:-${VOIPNET_SUBNET:-}}")
  if [ -n "$ip" ]; then
    echo "$ip"
    return
  fi

  # 3) Default route.
  if command -v ip >/dev/null 2>&1; then
    ip=$(ip -4 route get 1 2>/dev/null | awk '/src/ {print $7; exit}' || true)
    if [ -n "$ip" ] && [ "$ip" != "127.0.0.1" ]; then
      echo "$ip"
      return
    fi
  fi

  # 4) Hostname resolution fallback.
  ip=$(hostname -i 2>/dev/null || true)
  ip=$(printf '%s\n' "$ip" | awk '{print $1}')
  echo "$ip"
}

save_env_overrides \
  FREESWITCH_LOCAL_IP \
  FREESWITCH_ESL_PORT \
  FREESWITCH_ESL_PASSWORD \
  FREESWITCH_EXTERNAL_DTMF_TYPE \
  FREESWITCH_CONSOLE_LOGLEVEL \
  FREESWITCH_CORE_LOGLEVEL \
  FREESWITCH_TTS_ENGINE \
  FREESWITCH_TTS_VOICE \
  FREESWITCH_TTS_ALT_ENGINE \
  FREESWITCH_TTS_ALT_VOICE \
  FREESWITCH_TTS_ENGINE_EN \
  FREESWITCH_TTS_VOICE_EN \
  FREESWITCH_TTS_ENGINE_FR \
  FREESWITCH_TTS_VOICE_FR
load_config_file
restore_env_overrides

FREESWITCH_LOCAL_IP=${FREESWITCH_LOCAL_IP:-$(discover_primary_ip)}
if [ -z "$FREESWITCH_LOCAL_IP" ] || [ "$FREESWITCH_LOCAL_IP" = "0.0.0.0" ]; then
  FREESWITCH_LOCAL_IP=$(discover_primary_ip)
fi
FREESWITCH_LOCAL_IP=${FREESWITCH_LOCAL_IP:-127.0.0.1}

VOIPNET_SUBNET_CIDR=${VOIPNET_SUBNET:-192.168.144.0/20}

FREESWITCH_ESL_PORT=${FREESWITCH_ESL_PORT:-8021}
if [ -z "${FREESWITCH_ESL_PASSWORD:-}" ]; then
  echo "FREESWITCH_ESL_PASSWORD is required. Run ./setup.sh to generate local demo secrets." >&2
  exit 1
fi
FREESWITCH_EXTERNAL_DTMF_TYPE=${FREESWITCH_EXTERNAL_DTMF_TYPE:-rfc2833}
FREESWITCH_CONSOLE_LOGLEVEL=${FREESWITCH_CONSOLE_LOGLEVEL:-notice}
FREESWITCH_CORE_LOGLEVEL=${FREESWITCH_CORE_LOGLEVEL:-notice}

PIPER_HOME=${PIPER_HOME:-/opt/piper}
PIPER_VOICE=${PIPER_VOICE:-en_GB-northern_english_male-medium}
PIPER_BIN=${PIPER_BIN:-${PIPER_HOME}/piper}
PIPER_LANGUAGE=${PIPER_LANGUAGE:-en}
PIPER_VOICE_EN=${PIPER_VOICE_EN:-${PIPER_VOICE}}
PIPER_LANGUAGE_EN=${PIPER_LANGUAGE_EN:-${PIPER_LANGUAGE}}
PIPER_VOICE_FR=${PIPER_VOICE_FR:-fr_FR-siwis-medium}
PIPER_LANGUAGE_FR=${PIPER_LANGUAGE_FR:-fr}
PIPER_VOICE_PATH=${PIPER_VOICE_PATH:-${PIPER_HOME}/voices/${PIPER_VOICE}/${PIPER_VOICE}.onnx}
PIPER_VOICE_PATH_EN=${PIPER_VOICE_PATH_EN:-${PIPER_VOICE_PATH}}
PIPER_VOICE_PATH_FR=${PIPER_VOICE_PATH_FR:-${PIPER_HOME}/voices/${PIPER_VOICE_FR}/${PIPER_VOICE_FR}.onnx}
PIPER_OPTS=${PIPER_OPTS:-}
PIPER_CACHE_PATH=${PIPER_CACHE_PATH:-/tmp/piper-tts-cache}
PIPER_CACHE_ENABLE=${PIPER_CACHE_ENABLE:-false}

echo "FreeSWITCH resolved local IP: ${FREESWITCH_LOCAL_IP}"
echo "Using VOIP subnet ACL: ${VOIPNET_SUBNET_CIDR}"

echo "Replacing env vars in FreeSWITCH configs..."
cp -r /usr/local/freeswitch/conf.template/* /usr/local/freeswitch/conf/
FREESWITCH_ESL_PASSWORD_ESCAPED=$(printf '%s' "$FREESWITCH_ESL_PASSWORD" | sed -e 's/[&|]/\\&/g')
sed -i -e "s|{{FREESWITCH_LOCAL_IP}}|${FREESWITCH_LOCAL_IP}|g" \
       /usr/local/freeswitch/conf/vars.xml
sed -i -e "s|{{FREESWITCH_ESL_PORT}}|${FREESWITCH_ESL_PORT}|g" \
       /usr/local/freeswitch/conf/autoload_configs/event_socket.conf.xml
sed -i -e "s|{{FREESWITCH_ESL_PASSWORD}}|${FREESWITCH_ESL_PASSWORD_ESCAPED}|g" \
       /usr/local/freeswitch/conf/autoload_configs/event_socket.conf.xml
sed -i -e "s|{{FREESWITCH_CONSOLE_LOGLEVEL}}|${FREESWITCH_CONSOLE_LOGLEVEL}|g" \
       /usr/local/freeswitch/conf/autoload_configs/console.conf.xml
sed -i -e "s|{{FREESWITCH_CORE_LOGLEVEL}}|${FREESWITCH_CORE_LOGLEVEL}|g" \
       /usr/local/freeswitch/conf/autoload_configs/switch.conf.xml
sed -i -e "s|{{VOIPNET_SUBNET}}|${VOIPNET_SUBNET_CIDR}|g" \
       /usr/local/freeswitch/conf/autoload_configs/acl.conf.xml
sed -i -e "s|{{PIPER_BIN}}|${PIPER_BIN}|g" \
       -e "s|{{PIPER_OPTS}}|${PIPER_OPTS}|g" \
       -e "s|{{PIPER_CACHE_PATH}}|${PIPER_CACHE_PATH}|g" \
       -e "s|{{PIPER_CACHE_ENABLE}}|${PIPER_CACHE_ENABLE}|g" \
       -e "s|{{PIPER_LANGUAGE_EN}}|${PIPER_LANGUAGE_EN}|g" \
       -e "s|{{PIPER_VOICE_PATH_EN}}|${PIPER_VOICE_PATH_EN}|g" \
       -e "s|{{PIPER_LANGUAGE_FR}}|${PIPER_LANGUAGE_FR}|g" \
       -e "s|{{PIPER_VOICE_PATH_FR}}|${PIPER_VOICE_PATH_FR}|g" \
       /usr/local/freeswitch/conf/autoload_configs/piper_tts.conf.xml
sed -i -e "s|{{FREESWITCH_EXTERNAL_DTMF_TYPE}}|${FREESWITCH_EXTERNAL_DTMF_TYPE}|g" \
       /usr/local/freeswitch/conf/sip_profiles/external.xml

echo "Starting FreeSWITCH..."
exec /usr/local/freeswitch/bin/freeswitch
