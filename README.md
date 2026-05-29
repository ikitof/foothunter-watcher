# ⚽ Foot Live

Petite fenêtre **toujours au-dessus** qui suit les scores de
`http://foothunter.wiriath.com:6767/resultats/saison2/` **automatiquement** —
plus besoin de faire F5 ni de cliquer pour déplier les matchs.

![icone](foot-live.png)

## Ce que ça fait

- **Rafraîchissement auto** toutes les 10 à 120 s (réglable).
- **Tous les matchs affichés** (score, possession, tirs) sans rien déplier.
- **Détection « EN DIRECT »** : reprend l'indicateur du site (point rouge + score
  en rouge) → **fiable même pour un 0-0** ou un match sans but depuis longtemps.
  Quand un score change, la ligne **clignote en jaune** et un petit **bip** retentit
  (désactivable).
- Vue **« ★ Toutes (live) »** : voit l'action de **toutes** les compétitions d'un coup,
  ou choisis une seule compétition (Premier League, Champions League, …).
- **Clique sur une équipe** (dans un match ou le classement) pour ouvrir son
  **historique** : chaque match (score, possession, occasions) et ses **moyennes**
  (buts pour/contre, possession, occasions) + son bilan **V-N-D** et ses points.
- **Page « 📊 stats »** : un **classement complet triable** (points, buts, diff,
  possession et occasions moyennes) + des faits marquants (meilleure attaque,
  meilleure défense, possession, occasions). Reflète la compétition choisie, ou
  **toutes** en vue « ★ Toutes (live) ». Trie en cliquant un en-tête de colonne.
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
| Clic sur une équipe| Ouvrir son historique de matchs + ses statistiques moyennes     |
| `📊 stats`         | Ouvrir le classement triable + les faits marquants              |
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
Le script ne fait que : `GET` la page → parser ce JSON → repérer les matchs en
direct grâce au **marqueur visuel du site** (point rouge `bg-red-500` + score
`text-red-600`) → comparer les scores au tour précédent (pour le clignotement et
le bip) → afficher. Zéro dépendance externe.
