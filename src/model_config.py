"""File-only model configuration and native agent launch adapters.

Only YAML loading needs PyYAML. The private runtime staged into containers
reads JSON and uses the standard library; secrets never enter command arguments.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Mapping
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "configs/models.local.yaml"
AGENTS = {"codex", "claude", "gemini", "opencode", "pi", "cursor"}
PROTOCOLS = {
    "openai-completions": "@ai-sdk/openai-compatible",
    "openai-responses": "@ai-sdk/openai",
    "anthropic-messages": "@ai-sdk/anthropic",
}


def _yaml_mapping(path: Path) -> dict[str, Any]:
    import yaml

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        # Parser errors include source lines, which can contain API keys.
        raise ValueError(f"Invalid model configuration YAML: {path}") from None
    if not isinstance(data, dict):
        raise ValueError(f"Model configuration must be a mapping: {path}")
    return data


def validate_connection(data: Mapping[str, Any], *, require_key: bool = True) -> None:
    for field in ("model", "base_url", "api_key"):
        value = data.get(field)
        if not isinstance(value, str) or any(c in value for c in "\r\n\0"):
            raise ValueError(f"Model configuration: {field} must be a single-line string")
        if (field != "api_key" or require_key) and not value.strip():
            raise ValueError(
                f"Model configuration: {field} is required; edit configs/models.local.yaml"
            )
    try:
        url = urlsplit(data["base_url"])
    except ValueError:
        raise ValueError("Model configuration: invalid base_url") from None
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
        raise ValueError("Model configuration: base_url must be an HTTP(S) URL without credentials")


def load_settings(path: Path | None = None) -> dict[str, Any]:
    """Read the local YAML configuration; never consult dotenv or env vars."""
    data = _yaml_mapping(path or DEFAULT_CONFIG)
    allowed = {
        "agent",
        "model",
        "base_url",
        "api_key",
        "reasoning_effort",
        "api",
        "review",
        "figures",
        "literature",
    }
    if set(data) - allowed:
        raise ValueError("Unknown model configuration field")
    validate_connection(data, require_key=False)
    if not isinstance(data.get("agent"), str) or data["agent"] not in AGENTS:
        raise ValueError("Model configuration: unsupported agent")
    if not isinstance(data.get("api"), str) or data["api"] not in PROTOCOLS:
        raise ValueError("Model configuration: unsupported api protocol")
    if data.get("reasoning_effort") not in (None, "low", "medium", "high", "xhigh"):
        raise ValueError("Model configuration: invalid reasoning_effort")
    if data["agent"] == "gemini" and data.get("reasoning_effort"):
        raise ValueError("Gemini does not support reasoning_effort")
    if data["agent"] == "cursor" and data["base_url"].rstrip("/") != "https://api.cursor.com":
        raise ValueError("Cursor does not support custom base_url; select another agent")
    for section, keys in (
        ("review", {"model", "base_url", "api_key"}),
        ("figures", {"enabled", "model", "audit_model", "base_url", "api_key"}),
    ):
        value = data.get(section)
        if not isinstance(value, dict) or set(value) - keys:
            raise ValueError(f"Model configuration: invalid {section} section")
        validate_connection(value, require_key=False)
    figures = data["figures"]
    if not isinstance(figures.get("enabled"), bool):
        raise ValueError("Model configuration: figures.enabled must be a boolean")
    if not isinstance(figures.get("audit_model"), str) or not re.fullmatch(
        r"[A-Za-z0-9_.:/-]+", figures["audit_model"]
    ):
        raise ValueError("Model configuration: invalid figures.audit_model")
    literature = data.get("literature")
    if isinstance(literature, dict):
        literature.setdefault("openalex_api_key", "")
    fields = {"s2_api_key", "openalex_api_key", "openalex_email", "openalex_base_url"}
    if not isinstance(literature, dict) or set(literature) != fields:
        raise ValueError("Model configuration: invalid literature section")
    for field in fields:
        value = literature[field]
        if not isinstance(value, str) or any(c in value for c in "\r\n\0"):
            raise ValueError(
                f"Model configuration: literature.{field} must be a single-line string"
            )
    try:
        url = urlsplit(literature["openalex_base_url"])
    except ValueError:
        raise ValueError("Model configuration: invalid literature.openalex_base_url") from None
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError(
            "Model configuration: literature.openalex_base_url must be an HTTP(S) search endpoint"
        )
    return data


def load_literature_settings() -> dict[str, str]:
    return dict(load_settings()["literature"])


def bind_agent_config(agent: Any) -> None:
    """Keep public run metadata consistent with the shared model configuration."""
    data = load_settings()
    agent.name = data["agent"]
    agent.model = data["model"]
    agent.profile = None
    agent.reasoning_effort = data.get("reasoning_effort")
    if hasattr(agent, "pool"):
        agent.pool = []
        agent.models = {}


def _private_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8"
    ) as stream:
        json.dump(value, stream)
    path.chmod(0o600)


def stage_runtime(directory: Path, *, figures: bool = False, literature: bool = False) -> None:
    data = load_settings()
    validate_connection(data)
    data.pop("review")
    if not figures:
        data.pop("figures")
    if not literature:
        data.pop("literature")
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    _private_json(directory / "config.json", data)
    shutil.copyfile(Path(__file__), directory / "runtime.py")


def clean_environment(environment: Mapping[str, str]) -> dict[str, str]:
    prefixes = (
        "OPENAI_",
        "ANTHROPIC_",
        "CLAUDE_",
        "CODEX_",
        "GEMINI_",
        "GOOGLE_",
        "AWS_",
        "AZURE_",
        "CURSOR_",
        "OPENCODE_",
        "PI_",
        "HEADLESS_",
        "AUTOILLUSTRATE_",
        "ENHANCE_",
        "AUDIT_",
        "UNIT_TEST_",
    )
    exact = {
        "OPENROUTER_API_KEY",
        "DEEPSEEK_API_KEY",
        "MOONSHOT_API_KEY",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "S2_API_KEY",
        "OPENALEX_API_KEY",
        "OPENALEX_EMAIL",
    }
    return {
        key: value
        for key, value in environment.items()
        if key not in exact and not key.startswith(prefixes)
    }


def prepare_agent(
    data: Mapping[str, Any], home: Path, environment: Mapping[str, str]
) -> dict[str, str]:
    """Apply the configured model, endpoint and key to the actual agent CLI."""
    validate_connection(data)
    env = clean_environment(environment)
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_DATA_HOME=str(home / ".local/share"),
        XDG_CACHE_HOME=str(home / ".cache"),
        HEADLESS_BILLING="auto",
        HEADLESS_MODELS_DEV_CACHE="",
    )
    agent, model, key, url = (data[k] for k in ("agent", "model", "api_key", "base_url"))
    if agent == "codex":
        directory = home / ".codex"
        directory.mkdir(parents=True)
        # A custom provider makes Headless preserve native endpoint auth under auto.
        (directory / "config.toml").write_text(
            f'model = {json.dumps(model)}\nmodel_provider = "configured"\n[model_providers.configured]\nname = "Configured endpoint"\nbase_url = {json.dumps(url)}\nenv_key = "CODEX_API_KEY"\nwire_api = "responses"\n',
            encoding="utf-8",
        )
        env.update(CODEX_HOME=str(directory), CODEX_API_KEY=key)
    elif agent == "claude":
        # A fresh config directory prevents subscription or Bedrock routing.
        env.update(
            CLAUDE_CONFIG_DIR=str(home / ".claude"),
            ANTHROPIC_API_KEY=key,
            ANTHROPIC_BASE_URL=url,
            ANTHROPIC_MODEL=model,
            ANTHROPIC_DEFAULT_OPUS_MODEL=model,
            ANTHROPIC_DEFAULT_SONNET_MODEL=model,
            ANTHROPIC_DEFAULT_HAIKU_MODEL=model,
            CLAUDE_CODE_SUBAGENT_MODEL=model,
        )
        _private_json(home / ".claude/settings.json", {"model": model})
        # Headless inspects this metadata before platform keychains.
        _private_json(home / ".claude/.config.json", {"primaryApiKey": key})
    elif agent == "gemini":
        env.update(GEMINI_API_KEY=key, GOOGLE_GEMINI_BASE_URL=url, GEMINI_MODEL=model)
        _private_json(
            home / ".gemini/settings.json",
            {"security": {"auth": {"selectedType": "gemini-api-key"}}},
        )
    elif agent == "pi":
        escaped_key = key.replace("$", "$$")
        if escaped_key.startswith("!"):
            escaped_key = "$!" + escaped_key[1:]
        directory = home / ".pi/agent"
        env.update(PI_CODING_AGENT_DIR=str(directory), PI_CODING_AGENT_PROVIDER="configured")
        _private_json(
            directory / "models.json",
            {
                "providers": {
                    "configured": {
                        "baseUrl": url,
                        "apiKey": escaped_key,
                        "api": data["api"],
                        "models": [{"id": model}],
                    }
                }
            },
        )
    elif agent == "opencode":
        path = home / ".config/opencode/opencode.json"
        _private_json(
            path,
            {
                "model": f"configured/{model}",
                "small_model": f"configured/{model}",
                "enabled_providers": ["configured"],
                "provider": {
                    "configured": {
                        "npm": PROTOCOLS[data["api"]],
                        "models": {model: {"name": model}},
                        "options": {"baseURL": url, "apiKey": key},
                    }
                },
            },
        )
        env["OPENCODE_CONFIG"] = str(path)
    elif agent == "cursor":
        env["CURSOR_API_KEY"] = key
    else:
        raise ValueError("Unsupported configured agent")
    if "literature" in data:
        _private_json(home / ".agentic_literature.json", data["literature"])
    return env


def configured_command(command: list[str], data: Mapping[str, Any]) -> list[str]:
    result = list(command)
    if data["agent"] in {"pi", "opencode"} and "--model" in result:
        result[result.index("--model") + 1] = f"configured/{data['model']}"
    return result


def _run(config: Path, command: list[str], *, figures: bool = False) -> int:
    data = json.loads(config.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="configured-agent-") as home:
        env = prepare_agent(data, Path(home), os.environ)
        if figures and data["figures"]["enabled"]:
            connection = data["figures"]
            validate_connection(connection)
            env.update(OPENAI_API_KEY=connection["api_key"], OPENAI_BASE_URL=connection["base_url"])
        if figures:
            for key in (
                "AUTOILLUSTRATE_HEADLESS_BIN",
                "AUTOILLUSTRATE_PYTHON",
                "AUTOILLUSTRATE_CHROME",
            ):
                if key in os.environ:
                    env[key] = os.environ[key]
        return subprocess.call(configured_command(command, data), env=env)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("shell", "stage", "figures", "run", "launch", "literature-status")
    )
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--figures", action="store_true")
    parser.add_argument("--literature", action="store_true")
    args, command = parser.parse_known_args()
    if command[:1] == ["--"]:
        command = command[1:]
    try:
        if args.action in {"run", "launch"}:
            if not command:
                raise ValueError("Missing configured agent command")
            if args.action == "run":
                if args.config is None:
                    raise ValueError("Missing private runtime configuration")
                return _run(args.config, command, figures=args.figures)
            with tempfile.TemporaryDirectory(prefix="model-config-") as directory:
                stage_runtime(Path(directory))
                return _run(Path(directory) / "config.json", command)
        data = load_settings()
        if args.action == "shell":
            for key, value in {
                "AGENT": data["agent"],
                "AGENT_MODEL": data["model"],
                "AGENT_PROFILE": "",
                "AGENT_REASONING_EFFORT": data.get("reasoning_effort") or "",
            }.items():
                print(f"{key}={shlex.quote(value)}")
        elif args.action == "stage":
            if args.directory is None:
                raise ValueError("stage requires --directory")
            stage_runtime(args.directory, figures=args.figures, literature=args.literature)
        elif args.action == "literature-status":
            s2_status = "keyed" if data["literature"]["s2_api_key"].strip() else "disabled"
            if data["literature"]["openalex_api_key"].strip():
                openalex_status = "keyed"
            elif data["literature"]["openalex_email"].strip():
                openalex_status = "polite-pool"
            else:
                openalex_status = "anonymous"
            print(f"semantic_scholar={s2_status} openalex={openalex_status}")
        elif data["figures"]["enabled"]:
            figure = data["figures"]
            validate_connection(figure)
            print(
                f"--enhance-model {shlex.quote(figure['model'])} --audit-vlm {shlex.quote(figure['audit_model'])} --unit-test-vlm openai-single"
            )
        else:
            print("--skip-enhance --panel-inspects 0")
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"Model configuration: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
