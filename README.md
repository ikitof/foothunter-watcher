# ⚽ Foot Live

Petite fenêtre **toujours au-dessus** qui suit les scores de
`http://foothunter.wiriath.com:6767/resultats/saison3/` **automatiquement** —
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
  La carte **« 💰 Effectif »** liste les **joueurs** (poste, nom, célébrité,
  salaire, âge) et donne le **salaire (M€), la célébrité et l'âge — en moyenne et
  médiane**. Disponible pour **toutes** les équipes, y compris la Ligue 2 :
  l'effectif est récupéré au besoin sur la page de l'équipe puis sur la fiche de
  chaque joueur (où figure « Salaire annuel : … »).
- **Clique sur un joueur** (dans la carte Effectif) pour sa **fiche** : des
  **stats pertinentes selon son poste** (gardien → % d'arrêts, clean sheets ;
  attaquant → buts/match, taux de finition ; etc.), avec son **rang dans la
  compétition**, sa comparaison salaire/célébrité au poste, et ses
  **performances par saison** (club + stat clé, saisons passées via les données
  historiques). Stats dérivées du modèle de match du jeu (cf. le manuel).
- **Page « 👤 joueurs »** : les **joueurs d'une ligue par poste**, triables —
  choisis la compétition et le poste, chaque joueur ouvre sa fiche. (Accessible
  aussi en cliquant une stat dans une fiche.)
- **Page « 📈 »** : compare la **célébrité entre deux saisons**, avec
  les plus fortes hausses et baisses, un filtre par poste (GAR, MOFF, AC, …)
  et un résumé des évolutions moyennes et extrêmes pour chaque rôle. Les
  classements affichent aussi l'âge actuel de chaque joueur lorsqu'il est
  disponible. Les valeurs sont exactes et viennent de l'export CSV complet de
  la page `/joueurs`, qui
  inclut aussi les joueurs absents de la table actuelle. L'app actualise
  automatiquement cet export au démarrage et via le bouton `↻` de la page ;
  `data_joueurs.csv` embarqué sert de repli hors ligne.
- **Page « 📊 stats »** : un **classement complet triable** (points, buts, diff,
  possession et occasions moyennes, **salaire et célébrité moyens**) + des faits
  marquants (meilleure attaque, meilleure défense, possession, occasions,
  **plus gros salaires, plus célèbre**). Reflète la compétition choisie ; en vue
  « ★ Toutes (live) », **une seule ligne par équipe avec tous ses matchs
  confondus** (championnat + coupes + Europe). Trie en cliquant un en-tête de
  colonne.
  Salaire et célébrité couvrent **toutes** les équipes : les effectifs sont
  **préchargés en arrière-plan** (la page s'ouvre tout de suite et se remplit au
  fur et à mesure, sans attente).
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

### Saison suivie

La saison courante est **détectée automatiquement** via l'API
(`/api/all_matchs` : le nombre de saisons terminées donne la saison en cours) —
l'app passe donc seule à la saison suivante dès son ouverture, sans rien changer
au code. Les résultats, le live, le classement et l'historique des saisons
passées viennent de l'API ; seul le calendrier des matchs à venir (dates) est
encore lu sur le HTML.

Pour épingler une saison précise (tests, saison non encore détectée), exporte
`FOOT_LIVE_SEASON` :

```bash
FOOT_LIVE_SEASON=4 python3 foot_scores.py
```

`FOOT_LIVE_SEASON` reste prioritaire sur la détection ; `FOOT_LIVE_SAISON_PATH`
force le chemin HTML utilisé pour le calendrier.

### Icône sur le bureau / menu d'applications

Le script `install.sh` détecte automatiquement le chemin du dossier — aucun
chemin à modifier à la main.

```bash
# dans le menu d'applications :
./install.sh

# OU une icône cliquable sur le bureau :
./install.sh ~/Bureau     # ou  ./install.sh ~/Desktop
```

## Windows `.exe`

Oui, c'est prévu via **PyInstaller**. Le build Windows doit être fait sur Windows
(ou par GitHub Actions) :

```powershell
pwsh ./scripts/build_windows.ps1
```

Le fichier sort dans `dist/FootLive.exe`.

Le build embarque aussi `data_joueurs.csv`, afin que la page d'évolution reste
disponible hors ligne dès le premier lancement.

Le workflow GitHub Actions `.github/workflows/windows-exe.yml` construit aussi
`FootLive.exe` à chaque push sur `main`, l'ajoute comme artefact, puis publie une
release roulante `main-latest` avec l'asset `FootLive.exe`.
La mise à jour automatique devient active après le premier run réussi de ce
workflow, quand la release `main-latest` existe.

### Mise à jour automatique

L'exécutable Windows vérifie au démarrage le dernier build publié depuis `main`
sur GitHub. S'il est plus récent que le commit inclus dans l'exe, il télécharge
automatiquement `FootLive.exe` depuis la release `main-latest`, puis propose de
redémarrer pour remplacer l'exe courant. Après le redémarrage, une fenêtre
**Nouveautés** affiche une seule fois la note du nouveau build. Cette même note,
maintenue dans `WHATS_NEW.md`, est publiée sur la release GitHub.

Il n'y a pas besoin d'installer Git sur le PC de l'utilisateur. En pratique,
l'app ne fait pas un `git pull` directement : elle récupère l'exe reconstruit
depuis `main`. Pour désactiver cette vérification :

```powershell
$env:FOOT_LIVE_DISABLE_AUTO_UPDATE = "1"
.\FootLive.exe
```

## Android `.apk`

L'application Android propose les scores, le classement et les évolutions dans
une interface adaptée au téléphone :

```text
https://github.com/ikitof/foothunter-watcher/releases/download/main-latest/FootLive.apk
```

Télécharge l'APK depuis le téléphone, autorise l'installation depuis le
navigateur lorsque Android le demande, puis ouvre `Foot Live`. L'APK prend en
charge Android 7.0 et les versions plus récentes.

Le serveur actuel utilise HTTP. L'APK autorise donc explicitement le trafic HTTP
vers `foothunter.wiriath.com` pour fonctionner sans attendre la mise en place de
HTTPS. Une alerte dans l'application signale les nouveaux APK ; Android demande
toujours une confirmation avant leur installation.

Le build Android local utilise Buildozer :

```bash
docker pull kivy/buildozer:latest
docker run --rm \
  --volume "$PWD":/home/user/hostcwd \
  --volume "$PWD/.buildozer-global":/root/.buildozer \
  --entrypoint /bin/bash \
  kivy/buildozer:latest \
  -lc "cd /home/user/hostcwd && ./scripts/build_android.sh"
```

Le fichier sort dans `bin/FootLive.apk`. Le workflow GitHub construit et publie
automatiquement l'APK avec l'exécutable Windows.

L'APK de téléchargement direct utilise une clé de signature stable dédiée à ce
projet afin qu'Android accepte les futures versions comme mises à jour.

## Utilisation

| Contrôle           | Effet                                                            |
|--------------------|-----------------------------------------------------------------|
| Liste déroulante   | Choisir la compétition (ou « ★ Toutes (live) »)                 |
| Clic sur une équipe| Ouvrir son historique de matchs + ses statistiques moyennes     |
| Clic sur un joueur | Ouvrir sa fiche (stats par poste, rang, perfs par saison)       |
| `👤 joueurs`       | Joueurs d'une ligue par poste (triable)                         |
| `📈`               | Plus fortes hausses/baisses de célébrité par saison et poste    |
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

Les stats d'effectif (salaire / célébrité) viennent de la page `/joueurs`,
chargée une seule fois au démarrage puis agrégée par équipe.
Les évolutions de célébrité utilisent l'export CSV exact déclenché par le bouton
de téléchargement de cette même page ; le CSV local est remplacé atomiquement
quand une nouvelle version est disponible.
