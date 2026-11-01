# Agentic RAG (TP3) vs RAG statique (TP2)

| Scénario | Système | Statut | Appels LLM | Outils | Tokens | Durée (s) |
|---|---|---|---|---|---|---|
| RAG simple | Agent | vérifiée | 4 | 1 | 4898 | 85.2 |
| RAG simple | RAG statique | répond | 1 | 1 | 1768 | 15.1 |
| Multi-étapes | Agent | vérifiée | 5 | 2 | 3723 | 64.5 |
| Multi-étapes | RAG statique | répond | 1 | 1 | 1359 | 10.9 |
| Information insuffisante | Agent | non vérifiée | 9 | 4 | 9130 | 167.7 |
| Information insuffisante | RAG statique | répond | 1 | 1 | 1619 | 16.6 |

## RAG simple

**Question** : Le juge judiciaire peut-il apprécier le caractère réel et sérieux du licenciement d'un salarié protégé autorisé par l'inspecteur du travail ?

**Agent** (agent → tools → agent → answer → verify → final) : Non, le juge judiciaire ne peut pas apprécier le caractère réel et sérieux du licenciement d'un salarié protégé autorisé par l'inspecteur du travail

Sources : Cass. Soc., 2018-09-12, n° 16-26.853, Cass. Soc., 2024-12-11, n° 23-10.439, Cass. Soc., 2020-11-04, n° 19-11.865

**RAG statique** : Non, le juge judiciaire ne peut apprécier le caractère réel et sérieux du licenciement d'un salarié protégé autorisé par l'inspecteur du travail, car il ne s'agit pas d'un licenciement économique, mais d'un licenciement protégé qui doit être apprécié par le juge administratif ou le ministre compétent.

Sources : aucune

## Multi-étapes

**Question** : Dans l'affaire n° 23-20.161 sur un bail rural, que reprochait le bailleur à l'arrêt d'appel et qu'a décidé la Cour de cassation ?

**Agent** (agent → tools → agent → tools → agent → answer → verify → final) : Le bailleur reprochait à l'arrêt d'appel qu'il n'avait pas été entendu ou appelé à la procédure, ce qui violait l'article 14 du code de procédure civile. La Cour de cassation a décidé que ce moyen n'est pas fondé et a rejeté le pourvoi.

Sources : Cass. Civ. 3e, 2025-03-13, n° 23-20.161

**RAG statique** : Le preneur à bail rural n'est tenu de mettre en cause à l'instance que ce dernier, qui a seul, par cet acte, manifesté sa volonté de rompre le bail.

Sources : aucune

## Information insuffisante

**Question** : Qu'est-ce que la théorie de l'imprévision et la Cour de cassation l'a-t-elle appliquée à un contrat ?

**Agent** (agent → tools → agent → answer → verify → agent → tools → agent → tools → agent → tools → agent → answer → verify → final) : La Cour de cassation a appliqué la théorie de l'imprévision au contrat de travail, mais la rupture du contrat se prescrit par douze mois Î compter de la notification de la rupture.

Sources : Cass. Civ. 3e, 2019-10-17, n° 18-19.611, Cass. Soc., 2023-09-27, n° 21-25.973

**RAG statique** : La Cour de cassation a appliqué la théorie de l'imprévision dans un contrat de travail, en considérant que les liens plus étroits avec un autre pays ne permettent pas dçevrer des dispositions impératives plus favorables.

Sources : aucune
