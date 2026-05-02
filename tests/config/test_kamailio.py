from pathlib import Path


def test_handle_ws_nat_does_not_stop_non_websocket_routing():
    config = Path("kamailio/config/routes-webrtc.cfg").read_text()

    no_nat_branch = config.split('if (!nat_uac_test("64")) {', 1)[1].split("}", 1)[0]

    assert "return(1);" in no_nat_branch
    assert "return(0);" not in no_nat_branch


def test_predicate_routes_do_not_return_zero():
    helpers = Path("kamailio/config/routes-request-helpers.cfg").read_text()
    registration = Path("kamailio/config/routes-registration.cfg").read_text()

    for route_name in (
        "HANDLE_RETRANSMISSIONS",
        "HANDLE_OPTIONS_KEEPALIVE",
        "HANDLE_CANCEL",
        "HANDLE_STATEFUL_ACK",
    ):
        route_body = helpers.split(f"route[{route_name}] {{", 1)[1].split("\n}", 1)[0]
        assert "return(0);" not in route_body
        assert "return(-1);" in route_body

    to_registered = registration.split("route[TOREGISTERED] {", 1)[1].split("\n}", 1)[0]
    assert "return(0);" not in to_registered
    assert "return(-1);" in to_registered


def test_rtpengine_extra_id_does_not_read_missing_via_header():
    config = Path("kamailio/config/routes-dialog-media.cfg").read_text()

    assert "@via[2].branch" not in config
    assert "$avp(extra_id) = $ci" in config


def test_kamailio_entrypoint_is_quiet_non_destructive_and_uses_rw_db_url():
    entrypoint = Path("kamailio/kamailio-entrypoint.sh").read_text()

    assert "set -x" not in entrypoint
    assert "env | sort" not in entrypoint
    assert 'cat "${KAMAILIO_CFG_RUNTIME}"' not in entrypoint
    assert "DROP DATABASE" not in entrypoint
    assert "KAMAILIO_DB_RW_USER" in entrypoint
    assert "KAMAILIO_DB_RW_PASS" in entrypoint
    assert (
        "postgres://$KAMAILIO_DB_SUPERUSER:$KAMAILIO_DB_SUPERUSER_PASS"
        not in entrypoint
    )


def test_kamailio_bootstrap_does_not_require_static_kamctlrc():
    dockerfile = Path("kamailio/Dockerfile").read_text()
    entrypoint = Path("kamailio/kamailio-entrypoint.sh").read_text()

    assert not Path("kamailio/kamctlrc").exists()
    assert "COPY kamctlrc" not in dockerfile
    assert "rm -f /etc/kamailio/kamctlrc" in dockerfile
    assert "export_kamdbctl_config" in entrypoint
    assert "DBENGINE=PGSQL" in entrypoint


def test_kamailio_webrtc_fallback_ports_match_env_template():
    entrypoint = Path("kamailio/kamailio-entrypoint.sh").read_text()
    env_template = Path(".env.example").read_text()

    assert "KAMAILIO_EXTERNAL_WEBRTC_PORT=5066" in env_template
    assert "KAMAILIO_INTERNAL_WEBRTC_PORT=5068" in env_template
    assert "KAMAILIO_EXTERNAL_WEBRTC_PORT=${KAMAILIO_EXTERNAL_WEBRTC_PORT:-5066}" in entrypoint
    assert "KAMAILIO_INTERNAL_WEBRTC_PORT=${KAMAILIO_INTERNAL_WEBRTC_PORT:-5068}" in entrypoint


def test_kamailio_webrtc_socket_names_are_spelled_correctly():
    kamailio_cfg = Path("kamailio/kamailio.cfg").read_text()

    assert "webrtp" not in kamailio_cfg
    assert 'name "docker_external_tcp_webrtc"' in kamailio_cfg
    assert 'name "docker_internal_tcp_webrtc"' in kamailio_cfg


def test_kamailio_diagnostic_logging_is_disabled_by_default():
    kamailio_cfg = Path("kamailio/kamailio.cfg").read_text()
    module_params = Path("kamailio/config/modules-params.cfg").read_text()
    env_template = Path(".env.example").read_text()

    assert "debug={{KAMAILIO_DEBUG_LEVEL}}" in kamailio_cfg
    assert "KAMAILIO_DEBUG_LEVEL=0" in env_template
    assert "KAMAILIO_SIP_BODY_LOGGING=0" in env_template
    assert 'modparam("sipdump", "enable", {{KAMAILIO_SIPDUMP_ENABLE}})' in module_params
    assert 'modparam("siptrace", "trace_on", {{KAMAILIO_SIPTRACE_ENABLE}})' in module_params


def test_kamailio_database_bootstrap_docs_and_fallbacks_match_compose():
    entrypoint = Path("kamailio/kamailio-entrypoint.sh").read_text()
    kamailio_cfg = Path("kamailio/kamailio.cfg").read_text()
    readme = Path("kamailio/README.md").read_text()

    assert "KAMAILIO_DB_HOSTNAME=${KAMAILIO_DB_HOSTNAME:-kamailio_db}" in entrypoint
    assert "kamdbctl create" in entrypoint
    assert "kamdbctl create" in readme
    assert "seed data" not in readme.lower()
    assert "postgres://kamailio_ro:kamailioro@db/kamailio" not in entrypoint
    assert "postgres://kamailio_ro:kamailioro@db/kamailio" not in kamailio_cfg
    assert (
        'postgres://KAMAILIO_DB_RO_USER:KAMAILIO_DB_RO_PASS@kamailio_db/kamailio'
        in kamailio_cfg
    )


def test_kamailio_entrypoint_preserves_explicit_compose_env_over_conf_env():
    entrypoint = Path("kamailio/kamailio-entrypoint.sh").read_text()
    startup_block = entrypoint.split("save_env_overrides \\", 1)[1].split(
        "# Allow container/runtime wiring", 1
    )[0]

    assert "KAMAILIO_DB_HOSTNAME" in startup_block
    assert "KAMAILIO_DEBUG_LEVEL" in startup_block
    assert "RTPENGINE_SOCK" in startup_block
    assert "load_config_file\nrestore_env_overrides" in startup_block
