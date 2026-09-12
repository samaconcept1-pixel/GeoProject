"""Chargement de la configuration globale (config.yaml) et surcharges env."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


class AppConfig(BaseModel):
    """Section ``app`` du fichier config.yaml."""

    projects_root_dir: str = "/data/projects"
    # URL de base d'accès HTTP aux dossiers projets, ex. "http://192.168.1.50:8080/data/projects".
    # Vide = aucun lien cliquable exposé dans l'interface.
    projects_base_url: str = ""
    sync_interval_minutes: int = 15
    recreate_yaml: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    db_path: str = "data/sqlite.db"


class LLMAgentConfig(BaseModel):
    """Section ``llm_agent`` : client OpenAI-compatible (GPT-4o, Ollama, vLLM, LM Studio…)."""

    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    temperature: float = 0.1
    # Désactive le raisonnement des modèles qui supportent ce paramètre.
    reasoning_effort: str = "none"
    # Réponse courte : les métadonnées attendues tiennent dans un petit JSON.
    max_tokens: int = 800
    # Les serveurs locaux (LM Studio, Ollama…) peuvent être lents au premier
    # appel : chargement du modèle + raisonnement du LLM avant la réponse.
    timeout_seconds: float = 240.0
    # Réessais SDK désactivés : un réessai multiplierait l'attente avant le
    # repli sur le project.yaml minimal.
    max_retries: int = 0


class Config(BaseModel):
    """Configuration globale de l'application."""

    app: AppConfig = AppConfig()
    llm_agent: LLMAgentConfig = LLMAgentConfig()

    @property
    def effective_api_key(self) -> str:
        """La variable d'environnement OPENAI_API_KEY prime sur config.yaml."""
        return os.environ.get("OPENAI_API_KEY", "") or self.llm_agent.api_key


def load_config(path: Optional[str] = None) -> Config:
    """Charge config.yaml. Fichier absent ou partiel → valeurs par défaut.

    Chemin surchargé par la variable d'environnement ``CONFIG_PATH``.
    """
    cfg_path = Path(path) if path else Path(os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH))
    if cfg_path.is_file():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        return Config.model_validate(raw)
    return Config()