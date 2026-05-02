from pathlib import Path
import re


def test_freeswitch_prefers_opus_for_webrtc_audio():
    vars_xml = Path("freeswitch/conf/vars.xml").read_text()
    build_modules = Path("freeswitch/conf/modules.conf").read_text()
    runtime_modules = Path(
        "freeswitch/conf/autoload_configs/modules.conf.xml"
    ).read_text()

    assert "global_codec_prefs=OPUS,PCMU,PCMA" in vars_xml
    assert "codecs/mod_opus" in build_modules
    assert '<load module="mod_opus"/>' in runtime_modules


def test_freeswitch_runtime_modules_are_built_or_external():
    build_modules = {
        line.strip().rsplit("/", 1)[-1]
        for line in Path("freeswitch/conf/modules.conf").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    runtime_modules = set(
        re.findall(
            r'<load module="([^"]+)"',
            Path("freeswitch/conf/autoload_configs/modules.conf.xml").read_text(),
        )
    )

    assert runtime_modules - build_modules == {"mod_audio_stream"}


def test_freeswitch_config_tree_is_scoped_to_active_demo_paths():
    expected_files = {
        "freeswitch/conf/README.md",
        "freeswitch/conf/autoload_configs/acl.conf.xml",
        "freeswitch/conf/autoload_configs/console.conf.xml",
        "freeswitch/conf/autoload_configs/event_socket.conf.xml",
        "freeswitch/conf/autoload_configs/logfile.conf.xml",
        "freeswitch/conf/autoload_configs/modules.conf.xml",
        "freeswitch/conf/autoload_configs/piper_tts.conf.xml",
        "freeswitch/conf/autoload_configs/sofia.conf.xml",
        "freeswitch/conf/autoload_configs/switch.conf.xml",
        "freeswitch/conf/dialplan/public.xml",
        "freeswitch/conf/freeswitch.xml",
        "freeswitch/conf/modules.conf",
        "freeswitch/conf/sip_profiles/external.xml",
        "freeswitch/conf/vars.xml",
    }

    assert {
        str(path) for path in Path("freeswitch/conf").rglob("*") if path.is_file()
    } == expected_files


def test_freeswitch_runtime_modules_match_active_demo_scope():
    runtime_modules = set(
        re.findall(
            r'<load module="([^"]+)"',
            Path("freeswitch/conf/autoload_configs/modules.conf.xml").read_text(),
        )
    )

    assert runtime_modules == {
        "mod_audio_stream",
        "mod_commands",
        "mod_console",
        "mod_dialplan_xml",
        "mod_dptools",
        "mod_event_socket",
        "mod_flite",
        "mod_logfile",
        "mod_native_file",
        "mod_opus",
        "mod_piper_tts",
        "mod_sndfile",
        "mod_sofia",
    }


def test_freeswitch_public_dialplan_only_hands_demo_calls_to_gateway():
    public_dialplan = Path("freeswitch/conf/dialplan/public.xml").read_text()

    assert 'expression="^7000$"' in public_dialplan
    assert 'expression="^7100$"' in public_dialplan
    assert public_dialplan.count('application="socket"') == 2
    assert "voice_gateway:5050 async" in public_dialplan
    assert "888000" not in public_dialplan
    assert "9191" not in public_dialplan
    assert "9192" not in public_dialplan
    assert "inbound_trunk_to_extension" not in public_dialplan
    assert "from_internal_to_external" not in public_dialplan


def test_freeswitch_entrypoint_only_renders_supported_runtime_config():
    entrypoint = Path("freeswitch/freeswitch-entrypoint.sh").read_text()
    event_socket = Path(
        "freeswitch/conf/autoload_configs/event_socket.conf.xml"
    ).read_text()

    assert "{{FREESWITCH_ESL_PORT}}" in event_socket
    assert "{{FREESWITCH_ESL_PASSWORD}}" in event_socket
    assert "{{FREESWITCH_CONSOLE_LOGLEVEL}}" in Path(
        "freeswitch/conf/autoload_configs/console.conf.xml"
    ).read_text()
    assert "{{FREESWITCH_CORE_LOGLEVEL}}" in Path(
        "freeswitch/conf/autoload_configs/switch.conf.xml"
    ).read_text()
    assert "FREESWITCH_CONSOLE_LOGLEVEL=${FREESWITCH_CONSOLE_LOGLEVEL:-notice}" in entrypoint
    assert "FREESWITCH_CORE_LOGLEVEL=${FREESWITCH_CORE_LOGLEVEL:-notice}" in entrypoint
    assert "FREESWITCH_CDR_DB" not in entrypoint
    assert "FREESWITCH_CDR_DB" not in Path(".env.example").read_text()
    assert not Path("freeswitch/conf/autoload_configs/cdr_pg_csv.conf.xml").exists()
    assert not Path("freeswitch/conf/cdr_schema.sql").exists()


def test_freeswitch_dockerfile_keeps_expensive_build_layers_cacheable():
    dockerfile = Path("freeswitch/Dockerfile").read_text()

    assert dockerfile.startswith("# syntax=docker/dockerfile:")
    assert "--mount=type=cache,target=/var/cache/apt" in dockerfile
    assert "--mount=type=cache,target=/root/.cache/ccache" in dockerfile
    assert 'make -j"$(nproc)"' in dockerfile
    assert 'cmake --build . --parallel "$(nproc)"' in dockerfile
    assert "git fetch --depth 1 origin" in dockerfile
    assert dockerfile.index("ARG PIPER_VERSION=") > dockerfile.index(
        "git init mod_audio_stream"
    )


def test_freeswitch_mod_audio_stream_ref_is_pinned_by_default():
    dockerfile = Path("freeswitch/Dockerfile").read_text()

    assert "ARG MOD_AUDIO_STREAM_REF=v1.0.3" in dockerfile
    assert "ARG MOD_AUDIO_STREAM_REF=\n" not in dockerfile
    assert 'git fetch --depth 1 origin "${MOD_AUDIO_STREAM_REF}"' in dockerfile
    assert "origin HEAD" not in dockerfile


def test_freeswitch_esl_is_local_docker_network_only_by_default():
    compose = Path("docker-compose.yml").read_text()
    freeswitch_service = compose.split("\n  freeswitch:", 1)[1].split(
        "\n\nnetworks:", 1
    )[0]
    docs = " ".join(Path("freeswitch/conf/README.md").read_text().lower().split())

    assert "\n    ports:" not in freeswitch_service
    assert "not host-published" in docs
    assert "local diagnostics" in docs


def test_voice_gateway_uses_same_esl_secret_source_as_freeswitch():
    compose = Path("docker-compose.yml").read_text()
    voice_gateway_service = compose.split("\n  voice_gateway:", 1)[1].split(
        "\n  stt_service:", 1
    )[0]
    freeswitch_service = compose.split("\n  freeswitch:", 1)[1].split(
        "\n\nnetworks:", 1
    )[0]

    expected_password = (
        "FREESWITCH_ESL_PASSWORD: "
        "${FREESWITCH_ESL_PASSWORD:?run ./setup.sh to create local secrets}"
    )
    assert "FREESWITCH_ESL_PORT: ${FREESWITCH_ESL_PORT:-8021}" in voice_gateway_service
    assert expected_password in voice_gateway_service
    assert expected_password in freeswitch_service


def test_freeswitch_bundles_clearer_french_piper_voice():
    dockerfile = Path("freeswitch/Dockerfile").read_text()
    entrypoint = Path("freeswitch/freeswitch-entrypoint.sh").read_text()

    assert "ARG PIPER_VOICE_FR=fr_FR-siwis-medium" in dockerfile
    assert "fr/fr_FR/siwis/medium/${PIPER_VOICE_FR}.onnx" in dockerfile
    assert "PIPER_VOICE_FR=${PIPER_VOICE_FR:-fr_FR-siwis-medium}" in entrypoint
