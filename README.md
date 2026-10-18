# Suivi PEA — agent hebdomadaire

Chaque dimanche soir, `suivi.py` :
1. récupère les cours de `watchlist.json` (Yahoo Finance via `yfinance`) ;
2. recalcule valeur intrinsèque et cible avec la méthode de la watchlist (BPA × PER juste, marge par bucket A/B/C) ;
3. détecte les signaux (sous la cible, proche de la cible, forte variation hebdo, position vs PRU) ;
4. fait rédiger la synthèse par Claude Haiku 5.5 avec recherche web (modèle modifiable via la variable d'environnement `CLAUDE_MODEL`) (pourquoi les valeurs ont bougé, nouvelles de NIBE ; si `ANTHROPIC_API_KEY` est défini), puis l'envoie par mail.

Outil personnel, pas un conseil en investissement. Aucun ordre n'est passé.

## Secrets GitHub à créer (Settings → Secrets and variables → Actions)
- `GMAIL_USER` : l'adresse Gmail qui envoie
- `GMAIL_APP_PASSWORD` : mot de passe d'application Google (compte Google → Sécurité → Validation en 2 étapes → Mots de passe des applications)
- `MAIL_TO` : adresse qui reçoit (peut être la même)
- `ANTHROPIC_API_KEY` : optionnel ; sans elle, le mail contient le récapitulatif chiffré sans synthèse rédigée

## Utilisation
- Test manuel : onglet Actions → « Suivi PEA hebdo » → Run workflow.
- En local : `pip install -r requirements.txt && python suivi.py --dry-run` (affiche le mail sans l'envoyer).
- Modifier la watchlist, les marges, les seuils ou tes positions : `watchlist.json`.
