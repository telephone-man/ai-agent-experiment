from pathlib import Path


def test_rtpengine_entrypoint_has_no_tautological_internal_bind_compat_block():
    entrypoint = Path("rtpengine/rtpengine-entrypoint.sh").read_text()
    tautology = (
        'RTPENGINE_INTERFACE_INTERNAL_BIND_IP:-}" ] && '
        '[ -z "${RTPENGINE_INTERFACE_INTERNAL_BIND_IP:-}'
    )

    assert tautology not in entrypoint


def test_rtpengine_entrypoint_has_quiet_single_source_config_startup():
    entrypoint = Path("rtpengine/rtpengine-entrypoint.sh").read_text()

    assert "ENABLE_TCPDUMP" not in entrypoint
    assert "ENABLE_DEBUG_WATCH" not in entrypoint
    assert "DEBUG_WATCH_" not in entrypoint
    assert "cat /etc/rtpengine/rtpengine.conf" not in entrypoint
    assert "--log-level=7" not in entrypoint
    assert "--log-stderr" not in entrypoint
    assert "--foreground" not in entrypoint
    assert (
        "exec /usr/bin/rtpengine --config-file=/etc/rtpengine/rtpengine.conf"
        in entrypoint
    )


def test_rtpengine_log_level_defaults_to_info_not_debug():
    config = Path("rtpengine/rtpengine.conf").read_text()
    entrypoint = Path("rtpengine/rtpengine-entrypoint.sh").read_text()
    env_template = Path(".env.example").read_text()

    assert "log-level = {{RTPENGINE_LOG_LEVEL}}" in config
    assert "RTPENGINE_LOG_LEVEL=6" in env_template
    assert "RTPENGINE_LOG_LEVEL=${RTPENGINE_LOG_LEVEL:-6}" in entrypoint
    assert "log-level = 7" not in config


def test_rtpengine_entrypoint_preserves_explicit_compose_env_over_conf_env():
    entrypoint = Path("rtpengine/rtpengine-entrypoint.sh").read_text()
    startup_block = entrypoint.split("save_env_overrides \\", 1)[1].split(
        "set_if_unset_or_any", 1
    )[0]

    assert "RTPENGINE_INTERFACE_EXTERNAL_ADVERTISE_IP" in startup_block
    assert "RTPENGINE_LOG_LEVEL_SUBSYSTEM" in startup_block
    assert "load_config_file\nrestore_env_overrides" in startup_block


def test_rtpengine_dockerfile_has_explicit_package_repair_and_polished_comments():
    dockerfile = Path("rtpengine/Dockerfile").read_text()

    assert "dpkg -i ngcp-rtpengine-daemon_*.deb || true" not in dockerfile
    assert "dpkg -i ngcp-rtpengine-utils_*.deb || true" not in dockerfile
    assert "apt-get install -f -y" in dockerfile
    assert "I think" not in dockerfile
