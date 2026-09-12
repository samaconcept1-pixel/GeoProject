"""Synchronisation : surveillance de la racine projets, agent AI, cache SQLite.

Le worker tourne en arrière-plan à l'intervalle configuré
(``app.sync_interval_minutes``) et peut être déclenché à la demande
via POST /api/sync.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import ValidationError

from .ai_agent import AIAgent, derive_id
from .config import Config
from .database import Database
from .models import ProjectYaml

logger = logging.getLogger(__name__)


def parse_project_yaml(path: Path) -> ProjectYaml | None:
    """Parse et valide un project.yaml. Malformé → ``None`` (ignoré + log)."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
        if not isinstance(raw, dict):
            raise ValueError("contenu YAML non mappable")
        # Le nom du dossier fournit un identifiant stable pour les YAML
        # minimaux qui contiennent seulement le nom et l'adresse du projet.
        raw.setdefault("id", derive_id(path.parent.name))
        model = ProjectYaml.model_validate(raw)
        if model.derniere_mise_a_jour is None:
            model.derniere_mise_a_jour = datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            )
        return model
    except (yaml.YAMLError, ValidationError, OSError, ValueError) as exc:
        logger.error("project.yaml invalide ignoré (%s) : %s", path, exc)
        return None


class SyncWorker:
    """Surveille /data/projects, indexe les project.yaml dans SQLite.

    Deux opérations bien séparées :
    - **Indexation** (``sync_once``) : lit les project.yaml existants → SQLite.
      Rapide, sans LLM — c'est la synchro du démarrage et du bouton ⟳.
      Un dossier sans project.yaml reçoit un fichier minimal pour rester
      indexable (jamais d'appel LLM automatique).
    - **Extraction AI** (``start_ai_extraction``) : le robot LLM, très gourmand,
      lancé UNIQUEMENT à la demande (bouton 🤖 / CLI). Traite les dossiers un
      par un et met SQLite à jour après chaque dossier (progression visible).
    """

    def __init__(self, cfg: Config, db: Database, agent: AIAgent) -> None:
        self.cfg = cfg
        self.db = db
        self.agent = agent
        self._lock = threading.Lock()
        self._sync_requested = threading.Event()
        self.last_result: dict = {}
        # État du job d'extraction AI (lancé manuellement).
        self._ai_lock = threading.Lock()
        self.ai_status: dict = {"running": False, "done": 0, "total": 0}

    def start_ai_extraction(self) -> dict:
        """Démarre l'extraction LLM en arrière-plan (thread). Déjà en cours → état actuel."""
        with self._ai_lock:
            if self.ai_status.get("running"):
                return self.ai_status
            self.ai_status = {
                "running": True,
                "done": 0,
                "total": 0,
                "current": None,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None,
                "errors": [],
                "skipped": 0,
            }
        thread = threading.Thread(target=self._ai_job, name="ai-extraction", daemon=True)
        thread.start()
        return self.ai_status

    def _ai_job(self) -> None:
        """Corps du job (thread) : extraction LLM dossier par dossier.

        Les dossiers dont les fichiers n'ont pas changé depuis leur
        project.yaml valide sont ignorés (``_needs_ai``) : cliquer deux fois
        ne relance pas toute l'extraction pour rien.
        """
        try:
            root = Path(self.cfg.app.projects_root_dir)
            folders = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
            with self._ai_lock:
                self.ai_status["total"] = len(folders)
            for folder in folders:
                with self._ai_lock:
                    self.ai_status["current"] = folder.name
                try:
                    if (
                        self.agent.enabled
                        and not self.cfg.app.recreate_yaml
                        and not self._needs_ai(folder)
                    ):
                        with self._ai_lock:
                            self.ai_status["skipped"] += 1
                            self.ai_status["done"] += 1
                        continue
                    self.agent.process_folder(folder, use_llm=True)
                except Exception as exc:  # noqa: BLE001 — un dossier en échec ne stoppe pas le job
                    logger.exception("Extraction AI : échec du dossier %s : %s", folder.name, exc)
                    with self._ai_lock:
                        self.ai_status["errors"].append(
                            {"folder": folder.name, "error": str(exc)}
                        )
                # SQLite à jour après CHAQUE dossier → progression visible sur la carte.
                self.sync_once()
                with self._ai_lock:
                    self.ai_status["done"] += 1
        finally:
            with self._ai_lock:
                self.ai_status["running"] = False
                self.ai_status["current"] = None
                self.ai_status["finished_at"] = datetime.now(timezone.utc).isoformat()
            logger.info("Extraction AI terminée : %s", self.ai_status)

    # ------------------------------------------------------------- synchronisation

    def sync_once(self, timeout: float = 0.0) -> dict:
        """Une passe de synchronisation complète (exécutée dans un thread).

        ``timeout > 0`` : attend la libération du verrou (POST /api/sync) ;
        sinon, si une synchro est déjà en cours, renvoie immédiatement "busy".
        """
        if timeout > 0:
            acquired = self._lock.acquire(timeout=timeout)
        else:
            acquired = self._lock.acquire(blocking=False)
        if not acquired:
            return {"status": "busy", "message": "Une synchronisation est déjà en cours"}
        try:
            return self._sync_once()
        finally:
            self._lock.release()
            self._sync_requested.clear()

    def _sync_once(self) -> dict:
        root = Path(self.cfg.app.projects_root_dir)
        if not root.is_dir():
            logger.warning("Dossier racine des projets introuvable : %s", root)
            self.last_result = {
                "status": "error",
                "message": f"dossier racine introuvable : {root}",
            }
            return self.last_result

        folders = sorted(p for p in root.iterdir() if p.is_dir())
        rows: list[dict] = []
        errors = 0

        for folder in folders:
            try:
                # L'indexation automatique ne lance jamais l'extraction LLM.
                # La recréation éventuelle est réservée au bouton d'extraction.
                self.agent.process_folder(folder)
                yaml_path = folder / "project.yaml"
                model = parse_project_yaml(yaml_path)
                if model is not None:
                    rows.append(self._to_row(folder, model))
                else:
                    errors += 1
            except Exception as exc:  # noqa: BLE001
                errors += 1
                logger.exception("Erreur de synchro du dossier %s : %s", folder.name, exc)

        self.db.replace_all(rows)
        self._last_sync = datetime.now(timezone.utc)
        self.last_result = {
            "status": "ok",
            "dossiers_scannes": len(folders),
            "projets_indexes": len(rows),
            "erreurs": errors,
            "derniere_sync": self._last_sync.isoformat(),
        }
        logger.info("Synchro terminée : %s", self.last_result)
        return self.last_result

    def _needs_ai(self, folder: Path) -> bool:
        """Le dossier nécessite-t-il l'agent AI ?

        - pas de project.yaml, ou
        - fichiers plus récents que project.yaml (et agent LLM actif :
          hors ligne, on ne réécrit jamais un project.yaml existant).
        """
        yaml_path = folder / "project.yaml"
        if not yaml_path.exists():
            return True
        if not self.agent.enabled:
            return False
        try:
            yaml_mtime = yaml_path.stat().st_mtime
        except OSError:
            return True
        for f in folder.rglob("*"):
            if f.is_file() and f.name.lower() != "project.yaml":
                try:
                    if f.stat().st_mtime > yaml_mtime:
                        return True
                except OSError:
                    continue
        return False

    def _filesystem_signature(self) -> tuple[tuple[str, int, int], ...]:
        """Retourne une empreinte légère des dossiers et de leurs YAML."""
        root = Path(self.cfg.app.projects_root_dir)
        if not root.is_dir():
            return ()
        signature = []
        for folder in sorted(p for p in root.iterdir() if p.is_dir()):
            yaml_path = folder / "project.yaml"
            try:
                stat = yaml_path.stat()
                signature.append((folder.name, stat.st_mtime_ns, stat.st_size))
            except OSError:
                signature.append((folder.name, 0, 0))
        return tuple(signature)

    @staticmethod
    def _to_row(folder: Path, model: ProjectYaml) -> dict:
        gps = model.coordonnees_gps
        return {
            "id": model.id,
            "folder_name": folder.name,
            "nom_projet": model.nom_projet,
            "ref_administrative": model.ref_administrative,
            "promoteur": model.promoteur,
            "adresse": model.adresse,
            "latitude": gps.latitude,
            "longitude": gps.longitude,
            "statut": model.statut,
            "etape_actuelle": model.etape_actuelle,
            "derniere_mise_a_jour": (
                model.derniere_mise_a_jour.isoformat() if model.derniere_mise_a_jour else None
            ),
        }

    # ------------------------------------------------------------- boucle de fond

    async def run(self) -> None:
        """Boucle asynchrone d'intervalle (la synchro initiale est faite au
        démarrage, avant le premier appel API — cf. lifespan de main.py).

        Polling court (pas de thread bloquant) pour rester annulable et
        répondre immédiatement à POST /api/sync (``request_sync``).
        """
        interval = max(1, self.cfg.app.sync_interval_minutes) * 60
        next_due = time.monotonic() + interval
        previous_signature = self._filesystem_signature()
        while True:
            await asyncio.sleep(min(5.0, max(0.0, next_due - time.monotonic())))
            signature_changed = self._filesystem_signature() != previous_signature
            if self._sync_requested.is_set():
                self._sync_requested.clear()
                await asyncio.to_thread(self.sync_once)
                next_due = time.monotonic() + interval
                previous_signature = self._filesystem_signature()
            elif signature_changed or time.monotonic() >= next_due:
                await asyncio.to_thread(self.sync_once)
                next_due = time.monotonic() + interval
                previous_signature = self._filesystem_signature()

    def request_sync(self) -> None:
        """Réveille la boucle de fond pour une synchronisation immédiate."""
        self._sync_requested.set()