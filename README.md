# SamaConcept · GeoProjects

Application web **100 % conteneurisée** de cartographie et de recherche des projets
du bureau d'études, développée selon le cahier des charges
[`plan de traville.md`](plan%20de%20traville.md).

| Composant | Technologie |
| --- | --- |
| Frontend | HTML5 / Bootstrap 5 + MapLibre GL (carte vectorielle OpenFreeMap/OSM) |
| Backend | Python 3.11 / FastAPI |
| Extraction AI | Client OpenAI-compatible (GPT-4o, Ollama, vLLM, LM Studio) |
| Cache | SQLite (lecture rapide, indexée) |
| Déploiement | Docker & Docker Compose |

## Démarrage rapide (Docker)

```bash
# 1. Pointer le dossier des projets dans docker-compose.yml
#    (volume /chemin/local/projets:/data/projects)

# 2. (Optionnel) Activer l'agent AI : renseigner api_key dans config.yaml
#    ou exporter OPENAI_API_KEY

./start.sh --docker          # build + lancement en arrière-plan, logs en direct
./stop.sh --docker           # arrêt et suppression des conteneurs

# Équivalent manuel : docker compose up --build
# → http://localhost:8000
```

Le conteneur surveille `/data/projects` (intervalle `sync_interval_minutes`),
génère les `project.yaml` manquants via l'agent AI et alimente le cache SQLite
persisté dans `./data_app/`.

> **Note** : la synchronisation du démarrage et le bouton **⟳ Réindexer**
> sont **rapides et sans LLM** (lecture des `project.yaml` → SQLite). Le robot
> AI, très gourmand en ressources, est volontairement lancé **à la main**
> (bouton **🤖 Extraire (AI)** ou `POST /api/extract`).

## Démarrage local (hors Docker)

```bash
./start.sh                   # crée .venv si besoin, installe les dépendances,
                             # puis lance uvicorn --reload sur config.local.yaml
                             # (données d'exemple fournies) → http://localhost:8000
./stop.sh                    # arrêt propre du serveur local (SIGTERM puis SIGKILL)
```

Le fichier de configuration est choisi par `CONFIG_PATH` (défaut :
`config.local.yaml`) : `CONFIG_PATH=config.yaml ./start.sh`.

### Manuel (équivalent)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
CONFIG_PATH=config.local.yaml uvicorn main:app --reload

# `start.sh` installe automatiquement Tesseract si nécessaire :
# macOS : Homebrew ; Linux : apt/dnf/pacman ; Windows : winget/Chocolatey.

# Tests :
pytest -q
```

Avec Docker, Tesseract et les langues française/anglaise sont installés
automatiquement par le `Dockerfile`.

## Configuration (`config.yaml`)

```yaml
app:
  projects_root_dir: "/data/projects"   # dossier racine des projets
  projects_base_url: ""                 # optionnel : URL externe des dossiers
                                        # (ex : http://192.168.1.50:8080/data/projects).
                                        # Vide = servis par l'app via /projects
  sync_interval_minutes: 15             # intervalle du worker de fond
  recreate_yaml: false                  # le bouton d'extraction réécrit aussi les YAML existants
  db_path: "data/sqlite.db"             # cache SQLite

llm_agent:
  base_url: "https://api.openai.com/v1" # compatible OpenAI (Ollama, vLLM, LM Studio…)
  api_key: ""                           # vide = mode hors ligne
  model: "gpt-4o-mini"
  temperature: 0.1
  timeout_seconds: 240                  # délai max par appel LLM
  max_retries: 0                        # réessais SDK (0 = repli immédiat)
```

### Exemple LM Studio local (`config.local.yaml`)

Avec LM Studio démarré en local (onglet *Developer* → *Start Server*, port 1234) :

```yaml
llm_agent:
  base_url: "http://localhost:1234/v1"
  api_key: "lm-studio"                  # clé factice : active l'agent AI
  model: "prism-ml/bonsai-27b"          # modèle chargé dans LM Studio
  temperature: 0.1
  timeout_seconds: 600                  # modèle local de raisonnement → appel lent
  max_retries: 0
```

- La clé `lm-studio` est factice mais **obligatoire** : c'est elle qui active
  l'extraction par LLM (clé vide = mode hors ligne).
- Les modèles de raisonnement locaux peuvent mettre plusieurs minutes par
  dossier au premier appel (chargement du modèle) : augmentez
  `timeout_seconds` si besoin.
- Test live de l'agent contre LM Studio : `RUN_LMSTUDIO_TESTS=1 pytest tests/test_ai_agent.py -k live`.

- Sans clé API, l'application fonctionne **hors ligne** : chaque dossier sans
  `project.yaml` reçoit un fichier minimal (GPS `0.0 / 0.0`) et les fichiers
  existants ne sont jamais réécrits.
- **Serveur IA indisponible** (LM Studio arrêté, clé API renseignée) :
  une sonde rapide (< 3 s) détecte l'indisponibilité — la synchro n'attend plus
  le timeout LLM sur chaque dossier, et un `project.yaml` existant n'est jamais
  écrasé par le fallback minimal (il est conservé jusqu'au retour du serveur).
- La variable d'environnement `OPENAI_API_KEY` prime sur `config.yaml`.

## Fonctionnement

La vue est une **carte vectorielle** (tuiles OpenStreetMap via OpenFreeMap,
sans clé API — nette à tous les niveaux de zoom, centrée sur le Maroc).
L'interface est **responsive** : sur téléphone, deux onglets « 🗺 Carte » et
« 📋 Projets & filtres » remplacent le panneau latéral.

Chaque projet affiche un **lien cliquable vers son dossier** : les dossiers
sont servis en lecture seule par l'application sous `/projects` (lien auto-
dérivé de l'adresse utilisée par le navigateur, ex. `http://192.168.1.50:8000/
projects/PROJ_2026_001_Tour_Alpha/`). Pour pointer vers un serveur de fichiers
externe, renseignez `app.projects_base_url` dans `config.yaml` — l'API renvoie
alors le champ `folder_url` correspondant.

1. **Indexation** (démarrage, intervalle configuré, bouton **⟳ Réindexer**) :
   le worker scanne la racine projets, lit chaque `project.yaml` existant et
   met à jour le cache SQLite — instantané, sans LLM. Un dossier sans
   `project.yaml` reçoit un fichier minimal (GPS `0.0 / 0.0`) pour rester
   indexable ; les yaml existants ne sont jamais réécrits par l'indexation.
2. **Extraction AI — manuelle uniquement** : le robot LLM lit l'arborescence
   et les extraits texte (PDF / Word / txt) de chaque dossier à traiter,
   extrait les métadonnées (nom, promoteur, statut, GPS, réf.
   administrative…) et écrit/actualise `project.yaml`. Très gourmand
   (modèle de raisonnement local : plusieurs minutes par dossier), il se
   lance via le bouton **🤖 Extraire (AI)** (`POST /api/extract`), avec
   progression en direct (`GET /api/extract/status`), ou en CLI :
   `python run_local_pipeline.py --agent <DOSSIER>`.
3. Chaque `project.yaml` **valide** (pydantic) est mis en cache dans SQLite ;
   un fichier malformé est ignoré et consigné dans les logs sans interrompre
   l'application.

## API REST

| Endpoint | Description |
| --- | --- |
| `GET /api/projects` | Projets filtrés (`q`, `statut` répétable, `promoteur`, `etape`, `has_gps`) |
| `GET /api/projects/{id}` | Fiche projet + liste des fichiers de son dossier |
| `GET /api/promoters` | Promoteurs uniques (filtre déroulant) |
| `POST /api/sync` | Réindexation rapide project.yaml → SQLite (sans LLM) |
| `POST /api/extract` | Lance le robot AI (LLM, gourmand) — manuel, en arrière-plan |
| `GET /api/extract/status` | Progression du job d'extraction AI |
| `GET /api/stats` | Nombre de projets groupés par statut |

Logique de filtrage : **ET** entre les critères, **OU** au sein d'un même filtre
(ex. `GET /api/projects?q=Horizon&statut=en_cours&statut=devis&has_gps=true`).

## Données d'exemple

`sample_projects/` contient 4 projets prêts à l'emploi (Tanger, Rabat,
Casablanca, Fès). En local : `CONFIG_PATH=config.local.yaml`.

## Structure du dépôt

```
app/            backend (config, modèles, SQLite, agent AI, synchro, routes)
static/         dashboard frontend (index.html, app.js, style.css)
tests/          tests unitaires et d'intégration (pytest)
main.py         point d'entrée FastAPI
start.sh        démarrage local (venv + uvicorn) ou Docker (--docker)
stop.sh         arrêt local (uvicorn) ou Docker (--docker)
config.yaml     configuration globale (montée dans le conteneur)
Dockerfile      image python:3.11-slim + uvicorn
docker-compose.yml
sample_projects/  projets d'exemple
```