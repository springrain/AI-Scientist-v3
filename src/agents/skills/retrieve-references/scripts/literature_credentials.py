"""Read repository YAML, or the private YAML-derived file in an agent home."""

from __future__ import annotations

import json
from pathlib import Path
import sys


def load_literature_settings() -> dict[str, str]:
    # Agent runs stage this private file from models.local.yaml into an isolated home.
    private = Path.home() / ".agentic_literature.json"
    if private.is_file():
        data = json.loads(private.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("openalex_api_key", "")
        fields = {"s2_api_key", "openalex_api_key", "openalex_email", "openalex_base_url"}
        if (
            not isinstance(data, dict)
            or set(data) != fields
            or any(
                not isinstance(value, str) or any(char in value for char in "\r\n\0")
                for value in data.values()
            )
        ):
            raise ValueError("Invalid private literature configuration")
        return data
    # Standalone repository scripts and host paper retrieval read YAML directly.
    for root in Path(__file__).resolve().parents:
        if (root / "configs/models.local.yaml").is_file() and (root / "src/model_config.py").is_file():
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            from src.model_config import load_literature_settings as load_repository_settings

            return load_repository_settings()
    raise ValueError("Literature configuration is unavailable; configure configs/models.local.yaml")
