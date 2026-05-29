# ⚽ Foot Live

Petite fenêtre **toujours au-dessus** qui suit les scores de
`http://foothunter.wiriath.com:6767/resultats/saison2/` **automatiquement** —
plus besoin de faire F5 ni de cliquer pour déplier les matchs.

![icone](foot-live.png)

## Ce que ça fait

- **Rafraîchissement auto** toutes les 10 à 120 s (réglable).
- **Tous les matchs affichés** (score, possession, tirs) sans rien déplier.
- **Détection « EN DIRECT »** : un score qui change entre deux rafraîchissements
  est repéré tout seul → la ligne **clignote en jaune**, le score passe en **rouge**,
  et un petit **bip** retentit (désactivable).
- Vue **« ★ Toutes (live) »** : voit l'action de **toutes** les compétitions d'un coup,
  ou choisis une seule compétition (Premier League, Champions League, …).
- **Classement** affiché pour les championnats.
- Fenêtre **petite, épinglée au-dessus, déplaçable**, qui mémorise sa taille,
  sa position et tes réglages.

## Lancer

Aucune installation : ça n'utilise que Python 3 + Tkinter (déjà présents).
Place le dossier où tu veux, puis depuis ce dossier :

```bash
python3 foot_scores.py
# ou
./run.sh
```

### Icône sur le bureau / menu d'applications

Le script `install.sh` détecte automatiquement le chemin du dossier — aucun
chemin à modifier à la main.

```bash
# dans le menu d'applications :
./install.sh

# OU une icône cliquable sur le bureau :
./install.sh ~/Bureau     # ou  ./install.sh ~/Desktop
```

## Utilisation

| Contrôle           | Effet                                                            |
|--------------------|-----------------------------------------------------------------|
| Liste déroulante   | Choisir la compétition (ou « ★ Toutes (live) »)                 |
| `toutes les N s`   | Intervalle de rafraîchissement                                  |
| `↻`                | Rafraîchir tout de suite                                        |
| `live`             | N'afficher que le direct + la journée/tour en cours            |
| `épingler`         | Garder la fenêtre au-dessus des autres                          |
| `bip`              | Sonner quand un score change                                    |

Les sections affichées :

- **🔴 EN DIRECT** — matchs dont le score vient de changer (fiable à 100 %).
- **🟢 En cours / récents** — résultats de la journée ou du tour en cours.
- **📅 Aujourd'hui** — matchs programmés ce jour (vue « Toutes »).

## Réglages

Tes préférences sont enregistrées dans `foot_scores_config.json` (à côté du script).
Supprime ce fichier pour repartir des réglages par défaut.

## Vérifier que tout marche (sans ouvrir la fenêtre)

```bash
python3 foot_scores.py --selftest
```

## Comment ça marche (technique)

Le site est une appli **NiceGUI** (FastAPI + Vue). Le HTML initial contient déjà
tout l'arbre des éléments en JSON (`parseElements(...)`) — donc une simple requête
HTTP suffit pour récupérer les scores, sans WebSocket ni navigateur.
Le script ne fait que : `GET` la page → parser ce JSON → comparer les scores au
tour précédent → afficher. Zéro dépendance externe.
