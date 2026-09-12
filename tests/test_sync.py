"""Tests : synchronisation project.yaml → SQLite et agent hors ligne."""

import yaml

from app.ai_agent import AIAgent
from app.config import AppConfig, Config, LLMAgentConfig
from app.database import Database
from app.sync import SyncWorker


def make_worker(tmp_path, root: str):
    cfg = Config(
        app=AppConfig(projects_root_dir=str(tmp_path / root)),
        llm_agent=LLMAgentConfig(api_key=""),  # agent LLM désactivé (hors ligne)
    )
    db = Database(str(tmp_path / "cache.db"))
    agent = AIAgent(cfg.llm_agent)
    return SyncWorker(cfg, db, agent), db


def write_project(root, folder, data):
    folder = root / folder
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "project.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def test_sync_indexe_les_yaml_valides(tmp_path):
    root = tmp_path / "projets"
    write_project(root, "PROJ_A", {
        "id": "PROJ_A", "nom_projet": "Projet A", "statut": "en_cours",
        "coordonnees_gps": {"latitude": 30.0, "longitude": -9.0},
    })
    write_project(root, "PROJ_B", {
        "id": "PROJ_B", "nom_projet": "Projet B", "statut": "devis",
    })
    # Un dossier sans project.yaml → l'agent (hors ligne) écrit un fallback
    (root / "PROJ_C").mkdir()

    worker, db = make_worker(tmp_path, "projets")
    result = worker.sync_once()

    assert result["status"] == "ok"
    assert result["projets_indexes"] == 3
    assert db.stats()["total"] == 3
    assert db.get_project("PROJ_B")["latitude"] == 0.0

    fallback = root / "PROJ_C" / "project.yaml"
    assert fallback.exists()
    data = yaml.safe_load(fallback.read_text(encoding="utf-8"))
    assert data["coordonnees_gps"] == {"latitude": 0.0, "longitude": 0.0}


def test_sync_accepte_yaml_minimal_et_integre_les_mises_a_jour(tmp_path):
    root = tmp_path / "projets"
    folder = root / "PROJ_MINIMAL_Marina"
    folder.mkdir(parents=True)
    yaml_path = folder / "project.yaml"
    yaml_path.write_text(yaml.safe_dump({
        "nom_projet": "Marina initiale",
        "adresse": "Port de Tanger",
    }), encoding="utf-8")

    worker, db = make_worker(tmp_path, "projets")
    worker.sync_once()

    project = db.get_project("PROJ_MINIMAL_Marina")
    assert project["nom_projet"] == "Marina initiale"
    assert project["adresse"] == "Port de Tanger"

    yaml_path.write_text(yaml.safe_dump({
        "nom_projet": "Marina rénovée",
        "adresse": "Nouvelle adresse, Tanger",
    }), encoding="utf-8")
    worker.sync_once()

    updated = db.get_project("PROJ_MINIMAL_Marina")
    assert updated["nom_projet"] == "Marina rénovée"
    assert updated["adresse"] == "Nouvelle adresse, Tanger"


def test_sync_integre_un_changement_d_id(tmp_path):
    root = tmp_path / "projets"
    folder = root / "PROJ_A_Ancien"
    folder.mkdir(parents=True)
    yaml_path = folder / "project.yaml"
    yaml_path.write_text(yaml.safe_dump({
        "id": "ANCIEN_ID",
        "nom_projet": "Projet A",
    }), encoding="utf-8")

    worker, db = make_worker(tmp_path, "projets")
    worker.sync_once()
    assert db.get_project("ANCIEN_ID") is not None

    yaml_path.write_text(yaml.safe_dump({
        "id": "NOUVEL_ID",
        "nom_projet": "Projet A renommé",
    }), encoding="utf-8")
    worker.sync_once()

    assert db.get_project("ANCIEN_ID") is None
    assert db.get_project("NOUVEL_ID")["nom_projet"] == "Projet A renommé"


def test_sync_ignore_yaml_invalide_sans_planter(tmp_path):
    root = tmp_path / "projets"
    write_project(root, "PROJ_OK", {"id": "OK1", "nom_projet": "Valide"})
    bad = root / "PROJ_BAD"
    bad.mkdir()
    (bad / "project.yaml").write_text("statut: valeur_invalide", encoding="utf-8")

    worker, db = make_worker(tmp_path, "projets")
    result = worker.sync_once()

    assert result["status"] == "ok"
    assert result["projets_indexes"] == 1
    assert result["erreurs"] == 1
    assert db.stats()["total"] == 1


def test_sync_supprime_de_sqlite_un_dossier_supprime(tmp_path):
    root = tmp_path / "projets"
    write_project(root, "PROJ_A", {"id": "PROJ_A", "nom_projet": "Projet A"})
    write_project(root, "PROJ_B", {"id": "PROJ_B", "nom_projet": "Projet B"})

    worker, db = make_worker(tmp_path, "projets")
    worker.sync_once()
    assert db.stats()["total"] == 2

    import shutil
    shutil.rmtree(root / "PROJ_B")
    worker.sync_once()

    assert db.stats()["total"] == 1
    assert db.get_project("PROJ_B") is None


def test_needs_ai_ne_reescrit_pas_sans_llm(tmp_path):
    """Hors ligne : un project.yaml existant n'est jamais réécrit."""
    root = tmp_path / "projets"
    folder = root / "PROJ_A"
    folder.mkdir(parents=True)
    path = folder / "project.yaml"
    path.write_text(yaml.safe_dump({"id": "A", "nom_projet": "Original"}), encoding="utf-8")
    # Fichier plus récent que project.yaml
    (folder / "nouveau_doc.txt").write_text("contenu", encoding="utf-8")

    worker, _ = make_worker(tmp_path, "projets")
    assert worker._needs_ai(folder) is False
    worker.sync_once()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["nom_projet"] == "Original"


def test_sync_nactive_jamais_la_recreation(tmp_path, monkeypatch):
    root = tmp_path / "projets"
    folder = root / "PROJ_A"
    folder.mkdir(parents=True)
    (folder / "project.yaml").write_text(
        yaml.safe_dump({"id": "A", "nom_projet": "Original"}),
        encoding="utf-8",
    )

    worker, _ = make_worker(tmp_path, "projets")
    calls = []

    def recreate(folder, use_llm=False):
        calls.append((folder.name, use_llm))
        return folder / "project.yaml"

    monkeypatch.setattr(worker.agent, "process_folder", recreate)
    worker.sync_once()

    assert calls == [("PROJ_A", False)]


def test_extraction_manuelle_recree_les_yaml_si_active(tmp_path, monkeypatch):
    root = tmp_path / "projets"
    folder = root / "PROJ_A"
    folder.mkdir(parents=True)
    (folder / "project.yaml").write_text(
        yaml.safe_dump({"id": "A", "nom_projet": "Original"}),
        encoding="utf-8",
    )

    worker, _ = make_worker(tmp_path, "projets")
    worker.cfg.app.recreate_yaml = True
    worker.agent.api_key = "test"
    calls = []

    monkeypatch.setattr(worker, "_needs_ai", lambda current_folder: False)

    def recreate(current_folder, use_llm=False):
        calls.append((current_folder.name, use_llm))
        return current_folder / "project.yaml"

    monkeypatch.setattr(worker.agent, "process_folder", recreate)
    monkeypatch.setattr(worker, "sync_once", lambda: {})
    worker._ai_job()

    assert calls == [("PROJ_A", True)]


def test_job_extraction_ai_traite_et_indexe(tmp_path, monkeypatch):
    """Le robot AI (manuel) extrait dossier par dossier et indexe à chaque pas."""
    from types import SimpleNamespace
    import app.ai_agent as mod

    # Faux LLM : renvoie une métadonnée exploitable, sans réseau.
    captured: dict = {}

    class _FakeCompletions:
        def create(self, **kwargs):
            captured["messages"] = kwargs.get("messages")
            message = SimpleNamespace(content='{"id": "PROJ_A", "nom_projet": "Extrait par LLM", '
                                                '"promoteur": "LLM Corp", "statut": "en_cours", '
                                                '"coordonnees_gps": {"latitude": 30.1, "longitude": -8.2}}')
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class _FakeClient:
        def __init__(self, completions, **kwargs):
            self.chat = SimpleNamespace(completions=completions)

    monkeypatch.setattr("openai.OpenAI", lambda **kw: _FakeClient(_FakeCompletions(), **kw))
    monkeypatch.setattr(mod.AIAgent, "_server_reachable", lambda self: True)

    root = tmp_path / "projets"
    folder = root / "PROJ_A"
    folder.mkdir(parents=True)
    (folder / "note.txt").write_text("Promoteur : LLM Corp.", encoding="utf-8")

    worker, db = make_worker(tmp_path, "projets")
    # make_worker désactive l'agent (api_key="") → on le réactive pour le job.
    worker.agent.api_key = "test"
    status = worker.start_ai_extraction()
    assert status["running"] is True

    # Attente de fin du thread (max 10 s).
    import time as _time
    for _ in range(100):
        if not worker.ai_status["running"]:
            break
        _time.sleep(0.1)
    assert worker.ai_status["running"] is False
    assert worker.ai_status["done"] == 1
    assert worker.ai_status["errors"] == []

    # Le LLM a bien écrit les métadonnées, et SQLite est à jour.
    data = yaml.safe_load((folder / "project.yaml").read_text(encoding="utf-8"))
    assert data["promoteur"] == "LLM Corp"
    assert db.get_project("PROJ_A")["promoteur"] == "LLM Corp"


def test_job_extraction_ai_pas_deux_fois_en_parallele(tmp_path):
    """Un seul job d'extraction à la fois (le 2e appel renvoie l'état courant)."""
    root = tmp_path / "projets"
    (root / "PROJ_A").mkdir(parents=True)
    worker, _ = make_worker(tmp_path, "projets")
    worker.agent.api_key = "test"
    worker.ai_status["running"] = True  # simule un job en cours
    status = worker.start_ai_extraction()
    assert status["running"] is True
    assert worker.ai_status["done"] == 0  # pas de nouveau job démarré