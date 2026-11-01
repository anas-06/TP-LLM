# Démo vidéo (≈ 5 min)

## Enregistrer l'écran sous Windows

- **Xbox Game Bar** (déjà installé) : ouvrir le navigateur sur l'application, puis `Win + Alt + R`
  pour démarrer / arrêter l'enregistrement. La vidéo est dans `Vidéos\Captures`.
  (`Win + G` → icône micro pour enregistrer aussi la voix.)
- ou **OBS Studio** (source « Capture de fenêtre ») si vous voulez cadrer seulement le navigateur.

Avant d'enregistrer :

```bash
ollama serve                                  # si Ollama ne tourne pas déjà
uvicorn backend.main:app --port 8000          # puis ouvrir http://localhost:8000
```

Fermer les autres applications lourdes : sur CPU, chaque appel au modèle 3B prend 10 à 50 s.
Faire un essai à blanc d'une question (le premier appel charge le modèle en mémoire).

## Déroulé proposé

1. **Présentation (20 s)** — la page d'accueil : 3 onglets (TP1, TP2, TP3), statut en haut à droite
   (modèle local, nombre de décisions, index hybride).
2. **TP2 — recherche (40 s)** — cliquer l'exemple « bail rural congé reprise » : résultats, passages
   surlignés, solution. Basculer Hybride / BM25 / Dense pour montrer les 3 modes. Taper `23-20.161` :
   accès direct par numéro de pourvoi.
3. **TP2 — affaire (60 s)** — ouvrir la décision : sommaire, thèmes, **Résumé** (faits, procédure,
   question de droit, décision, portée) avec les citations [P#] cliquables et les contrôles
   (« Citations valides »). Onglet **Questions** : « Quelle est la décision de la Cour ? » → réponse
   sourcée + passages utilisés.
4. **TP3 — scénario 2, multi-étapes (90 s)** — onglet Agent, cliquer « Multi-étapes », Lancer.
   Commenter en direct : DECISION → `search_cases` → OBSERVATION (D1 = l'affaire) → `read_case(D1)`
   → GÉNÉRATION → CHECK (tous les contrôles) → FINAL. Montrer le nœud actif du graphe, les sources
   « corpus » et les mesures (appels LLM, tokens, durée).
5. **TP3 — scénario 3, information insuffisante (60 s)** — cocher « Validation humaine avant l'appel
   externe », lancer « Information insuffisante » : la recherche ne suffit pas, l'agent veut appeler
   Wikipédia → fenêtre de validation → Autoriser. Montrer la séparation **Sources du corpus** /
   **Sources externes**.
6. **TP3 — scénario 1 + mémoire (30 s)** — scénario « RAG simple » (peu d'étapes), puis question de
   suivi « Et quelle était la solution retenue ? » : l'agent repart de l'affaire en mémoire (D0).
7. **Conclusion (20 s)** — les traces sauvegardées dans `docs/traces/`, les tests `pytest -q`, et la
   question directrice (l'agent décide mieux quand il faut *choisir* une source, le RAG simple reste
   meilleur en latence pour une question de principe).

Astuce : pour ne pas attendre devant la caméra, on peut couper au montage pendant les appels au
modèle, ou rejouer la trace d'un run déjà terminé avec `http://localhost:8000/?run=<run_id>`.
