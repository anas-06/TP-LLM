# TP : extraction d'informations avec un petit LLM

Mini-application locale qui extrait des **relations factuelles** (sujet, relation, objet)
d'un texte avec **Llama 3.2 1B** servi en local (Ollama ou llama.cpp). Elle sert aussi à
comparer plusieurs stratégies de prompting sur le jeu de données **Re-DocRED**.

```
┌──────────────────────┐   HTTP/JSON   ┌───────────────────────────┐   HTTP   ┌──────────────────────┐
│ Interface (frontend/)│ ───────────▶ │ Backend FastAPI (backend/) │ ───────▶ │ LLM local            │
│ saisie du texte,     │              │ construction du prompt,    │          │ Llama 3.2 1B         │
│ paramètres, résultat │ ◀─────────── │ appel au modèle, validation│ ◀─────── │ (Ollama / llama.cpp) │
└──────────────────────┘              └───────────────────────────┘          └──────────────────────┘
```

Le frontend n'appelle jamais le modèle : tout passe par l'API du backend (`/api/...`).

## Installation

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# LLM local (Ollama) : https://ollama.com/download
ollama pull llama3.2:1b
ollama serve            # si le service ne tourne pas déjà (port 11434)

# Données : téléchargées depuis le dépôt GitHub de Re-DocRED
# (fichiers déjà fournis dans data/, commande utile pour les régénérer)
python -m scripts.prepare_data --n-eval 100 --n-fewshot 100
```

Pour utiliser **llama.cpp** à la place d'Ollama :

```bash
llama-server -m Llama-3.2-1B-Instruct-Q4_K_M.gguf --port 8080
export LLM_PROVIDER=llamacpp LLM_BASE_URL=http://localhost:8080
```

Variables d'environnement : `LLM_PROVIDER` (`ollama` | `llamacpp`), `LLM_BASE_URL`,
`LLM_MODEL` (défaut `llama3.2:1b`), `LLM_TIMEOUT`.

## Lancer l'application

```bash
uvicorn backend.main:app --reload
# puis ouvrir http://localhost:8000
```

Dans l'interface, vous pouvez :
- saisir un texte libre ou **sélectionner un document** du jeu d'évaluation (la référence
  Re-DocRED s'affiche alors, avec précision, rappel et F1 pour ce document) ;
- choisir la **stratégie**, le jeu de relations, la **température**, la seed, top_p, top_k,
  le nombre de tokens, le nombre d'exemples few-shot, et activer ou non le mode JSON et le
  rejet des relations invalides ;
- lancer l'extraction et consulter les relations retenues ou rejetées (avec le motif du
  rejet), le raisonnement du modèle, la sortie brute et **le prompt exact envoyé** ;
- comparer, sur le même texte, toutes les stratégies ou plusieurs températures.

API : `POST /api/extract`, `POST /api/compare`, `GET /api/examples`, `GET /api/config`,
`GET /api/health` (documentation interactive sur `/docs`).

## Organisation du code

| Fichier | Rôle |
|---|---|
| `backend/llm_client.py` | Appel au LLM local (Ollama `/api/chat` ou llama.cpp `/v1/chat/completions`), paramètres de génération explicites (`GenerationParams`) |
| `backend/prompts.py` | Les 5 stratégies de prompting et la sélection des exemples few-shot |
| `backend/extraction.py` | Pipeline : prompt → LLM → analyse → validation |
| `backend/validation.py` | Validation JSON et schéma (Pydantic), ancrage des entités dans le texte, normalisation |
| `backend/relations.py` | Schéma des 96 relations Re-DocRED (codes Wikidata → noms lisibles), sous-ensemble « core », synonymes |
| `backend/dataset.py` | Conversion de Re-DocRED (détokénisation, entités, triplets) |
| `backend/metrics.py` | Précision, rappel, F1, taux de JSON valide, d'hallucinations, de relations hors schéma |
| `backend/main.py` | API FastAPI et service des fichiers statiques |
| `frontend/` | Interface HTML/JS, sans dépendance |
| `scripts/prepare_data.py` | Téléchargement et préparation des données |
| `scripts/evaluate.py` | Campagne d'évaluation comparative |
| `tests/` | Tests unitaires et d'API, avec un faux LLM (`pytest`) |

## Stratégies de prompting (questions 2 à 5)

Les stratégies sont **cumulatives**, pour isoler l'effet de chaque ajout :

| Stratégie | Contenu du prompt |
|---|---|
| `zero_shot` | Consigne minimale et format JSON attendu. Pas de liste de relations : le modèle invente ses libellés, qui sont normalisés par une table de synonymes commune (`employed_by` → `employer`, etc.). |
| `context` | + rôle du système, **liste des relations autorisées avec leur définition et leur sens** (sujet → objet), règles : ne rien inventer, recopier les noms exacts, liste vide si rien ne s'applique. |
| `few_shot` | `context` + *k* exemples (3 par défaut) sous forme de paires user/assistant. Ils sont tirés du **split dev**, jamais du split d'évaluation, et relus à la main (`CURATED_DEMOS`). Chaque exemple est tronqué à 3 phrases et à 8 relations maximum pour tenir dans le contexte d'un modèle de 1B. |
| `cot` | `few_shot` + **raisonnement avant la réponse** : les phrases sont numérotées et le modèle remplit un champ `"reasoning"` (entités, puis numéro de la phrase qui justifie chaque fait) avant `"relations"`. Les exemples contiennent eux aussi un raisonnement. |
| `decompose` | **Décomposition en 2 appels** : (1) extraction des entités nommées, seules les entités présentes dans le texte étant conservées ; (2) extraction des relations, en imposant que le sujet et l'objet soient pris dans cette liste. |

## Sortie structurée et validation (question 7)

1. **Contrainte côté serveur** : `format: "json"` (Ollama) ou `response_format` (llama.cpp),
   désactivable pour mesurer son effet.
2. **Validation côté code** (`backend/validation.py`) :
   - `json_valid` : `json.loads` réussit sur la sortie brute. Sinon, on tente une récupération
     (blocs de code, texte autour du JSON) qui est signalée. Si la sortie a été **coupée**
     par la limite de tokens (fréquent quand un petit modèle boucle), les relations déjà
     complètes sont conservées ;
   - `schema_valid` : la sortie respecte `{"relations": [{"subject", "relation", "object"}]}`
     (modèle Pydantic) ;
   - pour chaque relation : normalisation du libellé, **relation dans le schéma**,
     **sujet et objet présents dans le texte**, sujet ≠ objet, pas de doublon.
3. Les relations qui échouent à ces contrôles sont **rejetées**, mais restent visibles dans
   l'interface avec leur motif. Une relation dont une entité n'apparaît pas dans le texte est
   comptée comme une **hallucination**.

## Évaluation (questions 6 et 8)

- **Données** : le split *dev* de Re-DocRED sert à construire les prompts et le split *test*
  à l'évaluation. Les deux ensembles sont disjoints.
- **Relations** : par défaut, les 25 relations « core » (personnes, organisations, lieux,
  dates, œuvres). La référence est filtrée sur ce même ensemble. `--relations all` utilise
  les 96 relations.
- **Correspondance** : une prédiction (s, r, o) est correcte s'il existe un triplet de
  référence (h, r, t) où s est **l'une des mentions** de h et o l'une des mentions de t
  (comparaison sans casse, sans ponctuation et sans article initial).
- **Métriques** : précision, rappel et F1 (micro-moyennes), F1 et précision **sans
  validation** (pour mesurer l'apport du filtrage), taux de JSON valide, taux de schéma
  valide, taux d'hallucination, taux de relations hors schéma, nombre de relations par
  document et latence.
- **Contrôle des paramètres** : même modèle, mêmes documents, et top_p, top_k, max_tokens,
  mode JSON et nombre d'exemples fixés et enregistrés dans `results/<run>/config.json`.
  Seule la variable étudiée change. Avec une température > 0, plusieurs seeds permettent
  de mesurer la variance (moyenne ± écart-type).

```bash
# Comparaison des stratégies à température 0 (déterministe)
python -m scripts.evaluate --n-docs 30 --name strategies_t0

# Effet de la température sur la meilleure stratégie, 3 seeds par valeur
python -m scripts.evaluate --n-docs 30 --strategies few_shot \
    --temperatures 0 0.3 0.7 1.0 --seeds 1 2 3 --name temperature

# Effet du nombre d'exemples ou du mode JSON
python -m scripts.evaluate --n-docs 30 --strategies few_shot --n-shots 1 --name shots1
python -m scripts.evaluate --n-docs 30 --strategies few_shot --no-json-mode --name nojson

# Documents plus courts (plus faciles pour un 1B) : 4 premières phrases
python -m scripts.evaluate --n-docs 30 --max-sents 4 --name short
```

Une campagne interrompue (Ctrl+C, serveur arrêté) se reprend sans refaire les appels
déjà faits : relancez la même commande en ajoutant `--resume`. Les appels en échec
sont alors retentés.

Chaque campagne produit `results/<nom>/summary.md` (tableau prêt à copier dans le
compte rendu), `summary.json`, `config.json` et `predictions.jsonl` (sorties brutes et
relations pour chaque document, utiles à l'analyse d'erreurs).

Un document Re-DocRED compte environ 200 mots et une vingtaine de relations « core ». Il
faut donc s'attendre à un **rappel faible** pour un modèle de 1B : ce qui compte ici, c'est
la **comparaison relative** des stratégies, mesurée avec le même protocole.

## Tests

```bash
pytest -q
```

Les tests remplacent le LLM par un faux client. Ils couvrent la validation, la construction
des prompts, la séparation dev/test, le pipeline des 5 stratégies, les métriques, la charge
utile envoyée à Ollama et l'API.

## Compte rendu : résultats à compléter

> Les chiffres ci-dessous doivent provenir de vos exécutions de `scripts/evaluate.py`
> avec `llama3.2:1b`. Ce dépôt n'inclut pas de résultats pré-calculés.

- **Stratégie retenue** : …
- **Effet du contexte** (`zero_shot` → `context`) : …
- **Effet du few-shot** (`context` → `few_shot`) : …
- **Effet du raisonnement / de la décomposition** (`few_shot` → `cot` / `decompose`) : …
- **Effet de la température** : …
- **Effet de la validation côté code** (F1 avec et sans validation, taux d'hallucination) : …
