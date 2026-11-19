"""Model settings must control actual endpoints independently of .env/logins."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
from types import SimpleNamespace
import urllib.parse
from unittest.mock import Mock

import pytest
import yaml

from src import model_config


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    data = {
        "agent": "codex",
        "model": "configured-model",
        "base_url": "http://localhost:1234/v1",
        "api_key": "file-secret",
        "reasoning_effort": "high",
        "api": "openai-responses",
        "review": {
            "model": "review-model",
            "base_url": "http://localhost:4321/v1",
            "api_key": "review-secret",
        },
        "literature": {
            "s2_api_key": "literature-secret",
            "openalex_api_key": "openalex-secret",
            "openalex_email": "configured@example.test",
            "openalex_base_url": "http://localhost:6789/custom-api/works",
        },
        "figures": {
            "enabled": False,
            "model": "figure-model",
            "audit_model": "audit-model",
            "base_url": "http://localhost:5555/v1",
            "api_key": "",
        },
    }
    path = tmp_path / "configs/models.local.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setattr(model_config, "DEFAULT_CONFIG", path)
    monkeypatch.setattr(model_config, "REPO_ROOT", tmp_path)
    return data


def test_default_config_is_local_yaml() -> None:
    assert model_config.DEFAULT_CONFIG == model_config.REPO_ROOT / "configs/models.local.yaml"


def test_env_and_dotenv_never_override_file_settings(
    settings: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    (model_config.REPO_ROOT / ".env").write_text(
        "AGENT=claude\nOPENAI_API_KEY=dotenv-secret\nAGENT_MODEL=wrong\n", encoding="utf-8"
    )
    monkeypatch.setenv("OPENAI_API_KEY", "env-secret")
    monkeypatch.setenv("ANTHROPIC_MODEL", "env-model")
    config = SimpleNamespace(name="claude", model="old", pool=["claude"], models={"claude": "old"})
    model_config.bind_agent_config(config)
    assert (config.name, config.model, config.pool, config.models) == (
        "codex",
        "configured-model",
        [],
        {},
    )
    assert "file-secret" not in repr(config)
    assert model_config.load_settings()["api_key"] == "file-secret"


def test_local_file_changes_are_reloaded_without_legacy_defaults(settings: dict) -> None:
    assert not model_config.DEFAULT_CONFIG.with_name("models.yaml").exists()
    assert model_config.load_settings() == settings
    # Even an invalid legacy file cannot affect the local configuration.
    model_config.DEFAULT_CONFIG.with_name("models.yaml").write_text("api_key: [", encoding="utf-8")
    updated = copy.deepcopy(settings)
    updated["api_key"] = "local-secret"
    updated["review"]["model"] = "local-review"
    updated["literature"]["openalex_api_key"] = "local-openalex-secret"
    updated["literature"]["openalex_email"] = "local@example.test"
    model_config.DEFAULT_CONFIG.write_text(yaml.safe_dump(updated), encoding="utf-8")
    actual = model_config.load_settings()
    assert actual["api_key"] == "local-secret"
    assert actual["review"]["model"] == "local-review"
    assert actual["review"]["base_url"] == settings["review"]["base_url"]
    assert actual["literature"]["openalex_api_key"] == "local-openalex-secret"
    assert actual["literature"]["openalex_email"] == "local@example.test"
    assert actual["literature"]["s2_api_key"] == "literature-secret"
    assert actual["literature"]["openalex_base_url"] == settings["literature"]["openalex_base_url"]


def test_missing_local_file_does_not_fall_back_to_legacy_yaml(settings: dict) -> None:
    model_config.DEFAULT_CONFIG.with_name("models.yaml").write_text(
        yaml.safe_dump(settings), encoding="utf-8"
    )
    model_config.DEFAULT_CONFIG.unlink()
    with pytest.raises(FileNotFoundError, match=r"models\.local\.yaml"):
        model_config.load_settings()


def test_missing_key_cannot_use_environment_login(
    settings: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings["api_key"] = ""
    model_config.DEFAULT_CONFIG.write_text(yaml.safe_dump(settings), encoding="utf-8")
    monkeypatch.setenv("CODEX_API_KEY", "environment-secret")
    with pytest.raises(ValueError, match="api_key is required"):
        model_config.stage_runtime(tmp_path / "runtime")
    assert not (tmp_path / "runtime/config.json").exists()


def test_invalid_yaml_does_not_echo_credentials(settings: dict) -> None:
    model_config.DEFAULT_CONFIG.write_text('api_key: ["do-not-print\n', encoding="utf-8")
    with pytest.raises(ValueError) as error:
        model_config.load_settings()
    assert "do-not-print" not in str(error.value)


@pytest.mark.parametrize("agent", ["codex", "claude", "gemini", "pi", "opencode"])
def test_native_agent_configuration_receives_model_url_and_key(
    agent: str, settings: dict, tmp_path: Path
) -> None:
    settings["agent"] = agent
    ambient = {
        "OPENAI_API_KEY": "wrong",
        "CLAUDE_CODE_OAUTH_TOKEN": "wrong",
        "AWS_REGION": "wrong",
        "HEADLESS_BILLING": "subscription",
        "S2_API_KEY": "wrong",
        "OPENALEX_API_KEY": "wrong",
        "OPENALEX_EMAIL": "wrong",
        "HTTPS_PROXY": "http://proxy",
    }
    env = model_config.prepare_agent(settings, tmp_path, ambient)
    assert env["HEADLESS_BILLING"] == "auto"
    assert env["HTTPS_PROXY"] == "http://proxy"
    assert "wrong" not in env.values()
    if agent == "codex":
        config = tomllib.loads((tmp_path / ".codex/config.toml").read_text(encoding="utf-8"))
        assert config["model"] == settings["model"]
        assert config["model_provider"] == "configured"
        provider = config["model_providers"]["configured"]
        assert provider["base_url"] == settings["base_url"]
        assert env[provider["env_key"]] == "file-secret"
    elif agent == "claude":
        assert (
            env["ANTHROPIC_BASE_URL"],
            env["ANTHROPIC_API_KEY"],
            env["CLAUDE_CODE_SUBAGENT_MODEL"],
        ) == (settings["base_url"], "file-secret", settings["model"])
    elif agent == "gemini":
        assert (env["GOOGLE_GEMINI_BASE_URL"], env["GEMINI_API_KEY"], env["GEMINI_MODEL"]) == (
            settings["base_url"],
            "file-secret",
            settings["model"],
        )
    elif agent == "pi":
        data = json.loads(
            (Path(env["PI_CODING_AGENT_DIR"]) / "models.json").read_text(encoding="utf-8")
        )["providers"]["configured"]
        assert (data["baseUrl"], data["apiKey"], data["models"][0]["id"]) == (
            settings["base_url"],
            "file-secret",
            settings["model"],
        )
    else:
        data = json.loads(Path(env["OPENCODE_CONFIG"]).read_text(encoding="utf-8"))
        assert data["model"] == data["small_model"] == "configured/configured-model"
        assert data["provider"]["configured"]["options"] == {
            "baseURL": settings["base_url"],
            "apiKey": "file-secret",
        }


def test_pi_credentials_are_literal(settings: dict, tmp_path: Path) -> None:
    settings.update(agent="pi", api_key="!do-not-execute${TOKEN}")
    env = model_config.prepare_agent(settings, tmp_path, {})
    data = json.loads(
        (Path(env["PI_CODING_AGENT_DIR"]) / "models.json").read_text(encoding="utf-8")
    )
    assert data["providers"]["configured"]["apiKey"] == "$!do-not-execute$${TOKEN}"


def test_runtime_invokes_child_with_configured_endpoint_and_propagates_exit(
    settings: dict, tmp_path: Path
) -> None:
    directory = tmp_path / "runtime"
    model_config.stage_runtime(directory)
    private = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    assert not ({"review", "figures", "literature"} & private.keys())
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import os,sys,tomllib; from pathlib import Path; c=tomllib.loads((Path(os.environ['CODEX_HOME'])/'config.toml').read_text()); assert c['model']=='configured-model'; assert c['model_providers']['configured']['base_url']=='http://localhost:1234/v1'; assert os.environ['CODEX_API_KEY']=='file-secret'; assert 'ANTHROPIC_API_KEY' not in os.environ; sys.exit(17)",
        encoding="utf-8",
    )
    command = [
        sys.executable,
        str(directory / "runtime.py"),
        "run",
        "--config",
        str(directory / "config.json"),
        "--",
        sys.executable,
        str(probe),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=os.environ | {"CODEX_API_KEY": "wrong", "ANTHROPIC_API_KEY": "wrong"},
    )
    assert result.returncode == 17, result.stderr
    assert "file-secret" not in result.stdout + result.stderr + repr(command)
    if os.name != "nt":
        assert directory.stat().st_mode & 0o777 == 0o700
        assert (directory / "config.json").stat().st_mode & 0o777 == 0o600


def test_figure_api_can_use_independent_credentials(settings: dict, tmp_path: Path) -> None:
    settings = copy.deepcopy(settings)
    settings["figures"].update(
        enabled=True, base_url="http://localhost:5555/v1", api_key="figure-secret"
    )
    model_config.DEFAULT_CONFIG.write_text(yaml.safe_dump(settings), encoding="utf-8")
    directory = tmp_path / "runtime"
    model_config.stage_runtime(directory, figures=True)
    result = subprocess.run(
        [
            sys.executable,
            str(directory / "runtime.py"),
            "run",
            "--config",
            str(directory / "config.json"),
            "--figures",
            "--",
            sys.executable,
            "-c",
            "import os; assert os.environ['OPENAI_API_KEY']=='figure-secret'; assert os.environ['OPENAI_BASE_URL']=='http://localhost:5555/v1'; assert os.environ['CODEX_API_KEY']=='file-secret'",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def _reference_script(name: str):
    path = (
        Path(model_config.__file__).parent
        / "agents/skills/retrieve-references/scripts"
        / f"{name}.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_configured_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_host_literature_keys_ignore_ambient_credentials(
    settings: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.orchestrator.semantic_scholar import resolve_s2_api_key

    monkeypatch.setenv("S2_API_KEY", "wrong")
    monkeypatch.setenv("OPENALEX_EMAIL", "wrong@example.test")
    assert resolve_s2_api_key() == "literature-secret"
    assert resolve_s2_api_key("explicit") == "explicit"
    settings["literature"]["s2_api_key"] = ""
    model_config.DEFAULT_CONFIG.write_text(yaml.safe_dump(settings), encoding="utf-8")
    assert resolve_s2_api_key() is None


@pytest.mark.parametrize("api_key", ["", " \t "])
def test_empty_s2_key_skips_host_and_standalone_retrieval(
    settings: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, api_key: str,
) -> None:
    from src.orchestrator import semantic_scholar

    settings["literature"]["s2_api_key"] = api_key
    model_config.DEFAULT_CONFIG.write_text(yaml.safe_dump(settings), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("S2_API_KEY", "ambient-key-must-not-enable-requests")
    request = Mock()
    sleep = Mock()
    monkeypatch.setattr(semantic_scholar.urllib.request, "urlopen", request)
    monkeypatch.setattr(semantic_scholar.time, "sleep", sleep)
    assert semantic_scholar.semantic_scholar_key_forbidden_error("packing") is None
    with pytest.raises(semantic_scholar.SemanticScholarError, match="s2_api_key is empty"):
        semantic_scholar.retrieve_bibtex_entries(queries=["packing"], min_refs=1, max_refs=2)
    standalone = _reference_script("s2_fetch")
    out_bib = tmp_path / "references.bib"
    out_json = tmp_path / "references.json"
    monkeypatch.setattr(sys, "argv", [
        "s2_fetch", "--query", "packing", "--out-bib", str(out_bib), "--out-json", str(out_json),
    ])
    with pytest.raises(SystemExit, match="s2_api_key is empty"):
        standalone.main()
    assert not out_bib.exists() and not out_json.exists()
    request.assert_not_called()
    sleep.assert_not_called()
    monkeypatch.setattr(sys, "argv", ["model_config.py", "literature-status"])
    assert model_config.main() == 0
    output = capsys.readouterr()
    assert output.out == "semantic_scholar=disabled openalex=keyed\n"
    assert "search start" not in output.err


@pytest.mark.parametrize("api_key", ["openalex-secret+&=?", "", "  "])
@pytest.mark.parametrize("endpoint_path", ["/custom-api/works", "/proxy/search/"])
def test_openalex_uses_configured_key_email_and_search_endpoint(
    settings: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    api_key: str, endpoint_path: str,
) -> None:
    settings["literature"]["openalex_api_key"] = f" {api_key} "
    settings["literature"]["openalex_base_url"] = "http://localhost:6789" + endpoint_path
    model_config.DEFAULT_CONFIG.write_text(yaml.safe_dump(settings), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("OPENALEX_API_KEY", "wrong")
    monkeypatch.setenv("OPENALEX_EMAIL", "wrong@example.test")
    module = _reference_script("openalex_fetch")
    monkeypatch.setattr(
        sys, "argv", ["openalex_fetch", "--out-bib", "out.bib", "--out-json", "out.json"]
    )
    assert module.parse_args().mailto == "configured@example.test"
    urls = []

    def fetch(url):
        urls.append(url)
        return {"results": [{"id": "W1"}]}

    monkeypatch.setattr(module, "fetch_json", fetch)
    assert module.search_openalex("a & b", limit=3, mailto=module.parse_args().mailto) == [
        {"id": "W1"}
    ]
    parsed = urllib.parse.urlsplit(urls[0])
    assert parsed.scheme == "http" and parsed.netloc == "localhost:6789"
    assert parsed.path == endpoint_path
    expected = {
        "search": ["a & b"],
        "per-page": ["3"],
        "mailto": ["configured@example.test"],
    }
    if api_key.strip():
        expected["api_key"] = [api_key]
    assert urllib.parse.parse_qs(parsed.query) == expected


@pytest.mark.parametrize("private", [False, True])
def test_omitted_openalex_key_defaults_to_anonymous(
    settings: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, private: bool,
) -> None:
    settings["literature"].pop("openalex_api_key")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("OPENALEX_API_KEY", "wrong")
    if private:
        (tmp_path / ".agentic_literature.json").write_text(
            json.dumps(settings["literature"]), encoding="utf-8"
        )
    else:
        model_config.DEFAULT_CONFIG.write_text(yaml.safe_dump(settings), encoding="utf-8")
    module = _reference_script("openalex_fetch")
    assert module.load_literature_settings()["openalex_api_key"] == ""
    urls = []
    monkeypatch.setattr(module, "fetch_json", lambda url: (urls.append(url) or {"results": []}))
    assert module.search_openalex("paper", limit=1, mailto=None) == []
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(urls[0]).query) == {
        "search": ["paper"], "per-page": ["1"],
    }


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("api_key", [None, 123, ["secret"], "secret\n", "secret\r", "secret\0"])
def test_invalid_openalex_keys_are_rejected_without_echoing_credentials(
    settings: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    private: bool, api_key: object,
) -> None:
    settings["literature"]["openalex_api_key"] = api_key
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    if private:
        (tmp_path / ".agentic_literature.json").write_text(
            json.dumps(settings["literature"]), encoding="utf-8"
        )
    else:
        model_config.DEFAULT_CONFIG.write_text(yaml.safe_dump(settings), encoding="utf-8")
    module = _reference_script("openalex_fetch")
    with pytest.raises(ValueError) as error:
        module.load_literature_settings()
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    ("api_key", "email", "status"),
    [("openalex-secret", "", "keyed"), ("", "me@example.test", "polite-pool"), ("", "", "anonymous")],
)
def test_openalex_status_reports_configured_authentication_without_credentials(
    settings: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    api_key: str, email: str, status: str,
) -> None:
    settings["literature"].update(openalex_api_key=api_key, openalex_email=email)
    model_config.DEFAULT_CONFIG.write_text(yaml.safe_dump(settings), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["model_config.py", "literature-status"])
    assert model_config.main() == 0
    output = capsys.readouterr()
    assert output.out == f"semantic_scholar=keyed openalex={status}\n"
    assert output.err == ""


@pytest.mark.parametrize(
    "url",
    ["file:///tmp/works", "https://user:secret@example.test", "https://example.test?token=secret"],
)
def test_invalid_literature_search_endpoints_are_rejected(settings: dict, url: str) -> None:
    settings["literature"]["openalex_base_url"] = url
    model_config.DEFAULT_CONFIG.write_text(yaml.safe_dump(settings), encoding="utf-8")
    with pytest.raises(ValueError, match="literature.openalex_base_url") as error:
        model_config.load_settings()
    assert "secret" not in str(error.value)


def test_isolated_retrieval_scripts_use_private_yaml_credentials(
    settings: dict, tmp_path: Path
) -> None:
    directory = tmp_path / "runtime"
    model_config.stage_runtime(directory, literature=True)
    assert (
        json.loads((directory / "config.json").read_text(encoding="utf-8"))["literature"]
        == settings["literature"]
    )
    scripts = tmp_path / "isolated-scripts"
    scripts.mkdir()
    source = Path(model_config.__file__).parent / "agents/skills/retrieve-references/scripts"
    for name in ("literature_credentials.py", "openalex_fetch.py", "s2_fetch.py"):
        shutil.copyfile(source / name, scripts / name)
    probe = scripts / "probe.py"
    probe.write_text(
        """
import os,sys,urllib.parse
import openalex_fetch as openalex
import s2_fetch as s2
assert s2.load_literature_settings()['s2_api_key'] == 'literature-secret'
assert openalex.load_literature_settings()['openalex_api_key'] == 'openalex-secret'
sys.argv = ['openalex_fetch', '--out-bib', 'out.bib', '--out-json', 'out.json']
assert openalex.parse_args().mailto == 'configured@example.test'
urls = []
openalex.fetch_json = lambda url: (urls.append(url) or {'results': []})
openalex.search_openalex('paper', limit=1, mailto=openalex.parse_args().mailto)
assert urls[0].startswith('http://localhost:6789/custom-api/works?')
assert urllib.parse.parse_qs(urllib.parse.urlsplit(urls[0]).query)['api_key'] == ['openalex-secret']
assert not {'S2_API_KEY', 'OPENALEX_API_KEY', 'OPENALEX_EMAIL'} & os.environ.keys()
assert os.environ['CODEX_API_KEY'] == 'file-secret'
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(directory / "runtime.py"),
            "run",
            "--config",
            str(directory / "config.json"),
            "--",
            sys.executable,
            str(probe),
        ],
        capture_output=True,
        text=True,
        env=os.environ | {
            "S2_API_KEY": "wrong", "OPENALEX_API_KEY": "wrong", "OPENALEX_EMAIL": "wrong@example.test",
        },
    )
    assert result.returncode == 0, result.stderr
    assert "literature-secret" not in result.stdout + result.stderr
    assert "openalex-secret" not in result.stdout + result.stderr


@pytest.mark.parametrize("container", ["0", "1"])
def test_shared_shell_configuration_and_private_container_launch(
    settings: dict, tmp_path: Path, container: str
) -> None:
    bash = shutil.which("bash")
    if bash is None and Path("D:/Program Files/Git/bin/bash.exe").is_file():
        bash = "D:/Program Files/Git/bin/bash.exe"
    if bash is None:
        pytest.skip("Bash is not installed")
    sandbox = tmp_path / "sandbox"
    library = sandbox / "src/scripts/lib"
    library.mkdir(parents=True)
    (sandbox / "configs").mkdir()
    (sandbox / "configs/models.local.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")
    shutil.copyfile(Path(model_config.__file__), sandbox / "src/model_config.py")
    for name in ("container_agent.sh", "python_bin.sh", "concept_figure.sh"):
        shutil.copyfile(Path(model_config.__file__).parent / "scripts/lib" / name, library / name)
    script = r"""
set -euo pipefail
cd "$1"
if command -v cygpath >/dev/null; then WRAPPER_PYTHON="$(cygpath -u "$2")"; else WRAPPER_PYTHON="$2"; fi
source ./src/scripts/lib/container_agent.sh
AGENT=claude
AGENT_MODEL=wrong
CONTAINER_ENV_FILE=wrong.env
MODEL_LITERATURE_ENABLED=1
configure_shared_model
test "$AGENT" = codex
test "$AGENT_MODEL" = configured-model
test -z "$CONTAINER_ENV_FILE"
test "$("$PYTHON_BIN" "$MODEL_CONFIG_HELPER" literature-status)" = 'semantic_scholar=keyed openalex=keyed'
source ./src/scripts/lib/concept_figure.sh
CONCEPT_FIGURE_MODE_FLAGS='--mode full'
select_concept_figure_backend
test "$CONCEPT_FIGURE_MODE_FLAGS" = '--mode light'
test "$CONCEPT_FIGURE_BACKEND_FLAGS" = '--skip-enhance --panel-inspects 0'
HEADLESS_BIN=fake-headless
_dispatch_inner_command() {
  local config="${AGENT_INNER_CMD[4]}"
  if [ "$3" = 1 ]; then
    test "$config" = /home/agent-home/.model_config/config.json
    config="$6/.model_config/config.json"
  fi
  "$PYTHON_BIN" -c 'import json,sys; from pathlib import Path; c=json.loads(Path(sys.argv[1]).read_text()); assert c["api_key"]=="file-secret"; assert c["base_url"]=="http://localhost:1234/v1"; assert c["literature"]["s2_api_key"]=="literature-secret"' "$config"
  printf '%s\n' "${AGENT_INNER_CMD[@]}"
}
run_agent_prompt "$AGENT" hello "$AGENT_MODEL" '' "$PWD" "$3" unused bridge "$PWD/agent-home"
test -z "$MODEL_RUNTIME_TEMP_DIR"
"""
    result = subprocess.run(
        [bash, "-c", script, "config-test", str(sandbox), sys.executable, container],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "configured-model" in result.stdout
    assert "file-secret" not in result.stdout + result.stderr
    assert "literature-secret" not in result.stdout + result.stderr
