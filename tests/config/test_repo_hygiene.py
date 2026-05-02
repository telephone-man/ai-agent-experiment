from pathlib import Path
import os
import shutil
import subprocess


def test_no_tracked_generated_or_os_artifacts():
    result = subprocess.run(
        ["git", "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    )
    forbidden = (
        "__pycache__/",
        ".pyc",
        ".DS_Store",
        ".pytest_cache/",
        ".ruff_cache/",
        ".venv/",
    )
    tracked = [path for path in result.stdout.splitlines() if Path(path).exists()]

    assert [
        path for path in tracked if any(marker in path for marker in forbidden)
    ] == []


def test_env_template_uses_unique_shell_style_keys():
    keys = []
    for line in Path(".env.example").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assert ":" not in stripped.split("=", 1)[0]
        assert "=" in stripped
        keys.append(stripped.split("=", 1)[0])

    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    assert duplicates == []


def test_tracked_configs_do_not_ship_known_demo_credentials():
    tracked_templates = [
        Path(".env.example"),
        Path("docker-compose.yml"),
        Path("kamailio/kamailio-entrypoint.sh"),
        Path("freeswitch/freeswitch-entrypoint.sh"),
        Path("services/voice_gateway/clients.py"),
        Path("services/tts_service/main.py"),
    ]
    forbidden = [
        "ClueCon",
        "local-demo-esl-password-change-me",
        "KAMAILIO_DB_SUPERUSER_PASS=kamailio",
        "KAMAILIO_DB_RW_PASS=kamailiorw",
        "KAMAILIO_DB_RO_PASS=kamailioro",
        "POSTGRES_PASSWORD: kamailio",
    ]

    for path in tracked_templates:
        text = path.read_text(encoding="utf-8")
        assert [value for value in forbidden if value in text] == []


def test_setup_script_generates_local_runtime_secrets():
    setup_script = Path("scripts/setup-env.sh").read_text(encoding="utf-8")

    assert "populate_local_secrets" in setup_script
    assert "FREESWITCH_ESL_PASSWORD" in setup_script
    assert "KAMAILIO_DB_RW_PASS" in setup_script


def test_setup_script_generates_shell_sourceable_conf_env(tmp_path):
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    shutil.copyfile(".env.example", tmp_path / ".env.example")

    subprocess.run(
        ["sh", "scripts/setup-env.sh", "--force"],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "SETUP_ENV_ROOT_DIR": str(tmp_path)},
    )

    conf_env = conf_dir / "env"
    assert conf_env.exists()
    assert "RTPENGINE_LOG_LEVEL_SUBSYSTEM='core:6;control:6;" in conf_env.read_text(
        encoding="utf-8"
    )

    subprocess.run(
        [
            "sh",
            "-c",
            (
                'set -eu; . "$1"; '
                'test "$RTPENGINE_LOG_LEVEL_SUBSYSTEM" = '
                '"core:6;control:6;crypto:6;srtp:6;rtcp:6;ice:6;internals:0"; '
                'test "$LOCAL_STT_FINAL_TEXT" = '
                '"Speech detected by offline STT fallback; no transcript available."'
            ),
            "sh",
            str(conf_env),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_setup_script_warns_when_existing_env_is_missing_template_keys(tmp_path):
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    (tmp_path / ".env.example").write_text("FOO=one\nBAR=two\n", encoding="utf-8")
    (tmp_path / ".env").write_text("FOO=one\n", encoding="utf-8")
    (conf_dir / "env").write_text("FOO='one'\n", encoding="utf-8")

    result = subprocess.run(
        ["sh", "scripts/setup-env.sh"],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "SETUP_ENV_ROOT_DIR": str(tmp_path)},
    )

    assert "Warning: existing .env is missing .env.example keys: BAR" in result.stderr
    assert "Warning: existing conf/env is missing .env.example keys: BAR" in result.stderr
    assert "./setup.sh --force" in result.stderr


def test_eval_reports_are_generated_outside_source_tree_by_default():
    voice_runner = Path("evals/voice_policy/run_voice_policy_evals.py").read_text(
        encoding="utf-8"
    )
    multilingual_runner = Path("evals/multilingual/run_multilingual_evals.py").read_text(
        encoding="utf-8"
    )

    assert 'DEFAULT_REPORTS_DIR = Path("/tmp/voice-ai-evals")' in voice_runner
    assert (
        'DEFAULT_REPORTS_DIR = Path("/tmp/voice-ai-multilingual")'
        in multilingual_runner
    )
    assert not Path("evals/voice_policy/reports/voice_policy_eval_report.json").exists()
    assert not Path("evals/voice_policy/reports/voice_policy_eval_report.md").exists()


def test_services_dockerfile_pins_uv_installer():
    dockerfile = Path("services/Dockerfile").read_text(encoding="utf-8")

    assert "pip install --no-cache-dir uv==" in dockerfile
    assert "pip install --no-cache-dir uv\n" not in dockerfile


def test_vendored_browser_assets_have_reviewer_safe_provenance():
    sip_license = Path("web_client/assets/sip.min.js.LICENSE.txt").read_text(
        encoding="utf-8"
    )
    sounds_readme = Path("web_client/assets/sounds/README.md").read_text(
        encoding="utf-8"
    )
    web_readme = Path("web_client/README.md").read_text(encoding="utf-8")

    assert "SIP.js 0.21.2" in sip_license
    assert "MIT" in sip_license
    assert "JsSIP" in sip_license
    assert "not currently known" in sounds_readme.lower()
    assert "replace" in sounds_readme.lower()
    assert "callback_url" in web_readme
    assert "harness only" in web_readme


def test_submission_hygiene_and_tracked_agent_guidance_are_explicit():
    gitignore = Path(".gitignore").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")
    agents = Path("AGENTS.md").read_text(encoding="utf-8")

    assert "AGENTS.md" not in gitignore
    assert "evals/**/reports/" in gitignore
    assert "git archive" in readme
    for excluded in (
        ".env",
        "conf/env",
        ".venv",
        "__pycache__",
        "docs/freeswitch-docs",
        "Sphinx/doctree output",
    ):
        assert excluded in readme
    assert "intentionally tracked" in agents
    assert "optional local-only" in agents


def test_entrypoint_env_override_helpers_do_not_reparse_saved_values():
    for path in (
        Path("kamailio/kamailio-entrypoint.sh"),
        Path("rtpengine/rtpengine-entrypoint.sh"),
        Path("freeswitch/freeswitch-entrypoint.sh"),
    ):
        text = path.read_text(encoding="utf-8")

        assert 'eval "SAVED_$var_name=\\${$var_name}"' not in text
        assert 'eval "SAVED_$var_name=\\$saved_value"' in text
        assert 'eval "$var_name=\\$saved_value"' in text
