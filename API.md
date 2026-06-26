# API Foothunter — routes utiles & sorties attendues

**Base** : `http://foothunter.wiriath.com:6767`
**Docs interactives** : `/api/docs` (Swagger) · `/api/openapi.json` (spec)

Notes générales :
- Toutes les réponses JSON sont enveloppées dans `{"resultats": …}`.
- Pas d'authentification, **HTTP en clair** (pas de TLS).
- ⚠️ **Aucun endpoint « matchs à venir / calendrier / dates »** : l'API ne renvoie que les
  matchs **joués** + le **live**. Le calendrier des matchs à venir reste à scraper sur le HTML
  (`/resultats/saison<N>/<compétition>`).

---

## 1. `GET /api/all_matchs`
Tous les matchs **joués** des saisons **terminées**, groupés par saison.

- **Paramètres** : aucun
- **Usage** : historique complet ; le nombre de clés = numéro de la saison courante (les saisons finies sont `0..N-1`).

```bash
curl "http://foothunter.wiriath.com:6767/api/all_matchs"
```

```json
{
  "resultats": {
    "saison0": [ /* … 799 matchs … */ ],
    "saison1": [ … ],
    "saison2": [ … ],
    "saison3": [
      {
        "competition": "Bundesliga",
        "Phase": "Journée 1",
        "Equipe dom": "Werder Bremen",
        "Equipe ext": "Borussia Dortmund",
        "Score dom": 1, "Score ext": 1,
        "Occas dom": 3, "Occas ext": 1,
        "Posses dom": 27, "Posses ext": 73
      }
    ]
  }
}
```

---

## 2. `GET /api/matchs_par_saison`
Matchs **joués** d'une saison donnée (y compris la saison en cours — uniquement ce qui est déjà joué).

- **Paramètres** : `season_number` (entier, **requis**)
- **Usage** : résultats de la saison courante pour les scores/classement.

```bash
curl "http://foothunter.wiriath.com:6767/api/matchs_par_saison?season_number=3"
```

```json
{
  "resultats": [
    {
      "competition": "Bundesliga",
      "Phase": "Journée 1",
      "Equipe dom": "Hannover 96",
      "Equipe ext": "RB Leipzig",
      "Score dom": 1, "Score ext": 1,
      "Occas dom": 2, "Occas ext": 1,
      "Posses dom": 62, "Posses ext": 38
    }
  ]
}
```
> Saison en cours sans match joué → `{"resultats": []}`. Aucun match « à venir » n'apparaît jamais.

---

## 3. `GET /api/live_matchs`
Matchs **en direct** (flux dédié, **champs différents** des matchs joués).

- **Paramètres** : aucun
- **Champs spécifiques** : `occas_dom` / `occas_ext` = **booléens** « occasion chaude / but imminent dans les prochaines minutes ». `score_*` = score courant.

```bash
curl "http://foothunter.wiriath.com:6767/api/live_matchs"
```

```json
{
  "resultats": [
    {
      "competition": "Champions League",
      "nom_equipe_dom": "SC Braga",
      "nom_equipe_ext": "AC Milan",
      "score_dom": 2, "score_ext": 2,
      "occas_dom": false, "occas_ext": false
    }
  ]
}
```
> Aucun match en direct → `{"resultats": []}`.

---

## 4. `GET /api/infos_all_joueurs`
Tous les joueurs d'une saison (~980). **`id` stable d'une saison à l'autre** → permet de
reconstruire l'historique d'un joueur (célébrité, club, salaire…) en interrogeant plusieurs saisons.

- **Paramètres** : `season_number` (entier, **requis**)

```bash
curl "http://foothunter.wiriath.com:6767/api/infos_all_joueurs?season_number=3"
```

```json
{
  "resultats": [
    {
      "id": 1,
      "nom": "David Raya",
      "poste": "GAR",
      "nom_equipe": "Arsenal",
      "age": 33,
      "celebrite": 95.0,
      "salaire": 26.25
    }
  ]
}
```
> Postes : `GAR, DC, LAT, MDEF, MOFF, AIL, AC` (1 joueur par poste, 7 par équipe).
> Pas de niveau caché exposé — uniquement ces 7 champs publics.

---

## 5. `GET /api/infos_joueur_saison`
Infos d'**un** joueur pour une saison (objet plat).

- **Paramètres** : `nom_joueur` (chaîne, **requis**, encodée) · `season_number` (entier, **requis**)
- Recherche par **nom exact** (pas de sous-chaîne ni de wildcard).

```bash
curl "http://foothunter.wiriath.com:6767/api/infos_joueur_saison?nom_joueur=Lucas%20Chevalier&season_number=3"
```

```json
{
  "resultats": {
    "id": 281, "nom": "Lucas Chevalier", "poste": "GAR",
    "nom_equipe": "PSG", "age": 27, "celebrite": 98.4, "salaire": 28.8
  }
}
```
> Nom introuvable → `{"resultats": {}}`.

---

## Schémas réutilisés

**Match joué** (`all_matchs`, `matchs_par_saison`) :
| champ | type | ex. |
|---|---|---|
| `competition` | str | `"Ligue 1"` |
| `Phase` | str | `"Journée 6"`, `"Huitièmes"` |
| `Equipe dom` / `Equipe ext` | str | `"PSG"` |
| `Score dom` / `Score ext` | int | `3` |
| `Occas dom` / `Occas ext` | int | nombre d'occasions |
| `Posses dom` / `Posses ext` | int | % possession (somme ≈ 100) |

**Match live** (`live_matchs`) : `competition`, `nom_equipe_dom`, `nom_equipe_ext`, `score_dom` (int), `score_ext` (int), `occas_dom` (bool), `occas_ext` (bool).

**Joueur** (`infos_all_joueurs`, `infos_joueur_saison`) : `id` (int, stable), `nom` (str), `poste` (str), `nom_equipe` (str), `age` (int), `celebrite` (float), `salaire` (float, M€).

---

## Comportement d'erreur
| Cas | Réponse |
|---|---|
| `season_number` non entier (`abc`, `3.5`, `3 OR 1=1`) | **HTTP 422** + détail de validation JSON |
| `season_number` requis manquant | **HTTP 422** |
| saison inexistante / négative / énorme (`99`, `-1`) | **HTTP 500** `Internal Server Error` (corps en clair, pas de stack trace) |
| `nom_joueur` introuvable | **HTTP 200** `{"resultats": {}}` |

---

## Assets statiques (images) — conventions observées dans le HTML
Servis directement (pas dans le JSON ; à construire depuis les noms) :

| Route | Convention | Exemple |
|---|---|---|
| `/compets_logos/<slug>.png` | nom compétition en minuscules, espaces → `_` | `champions_league.png`, `bundesliga_2.png`, `copa_del_rey.png` |
| `/club_logos/<slug>.png` | nom club en minuscules, espaces → `_` | `psg.png` |
| `/images_titres/coupe_<slug>.png` | trophée (palmarès officiel d'un club) | `coupe_ligue_1.png` |
| `/stars/<nom>.webp` | image d'étoile (note d'un joueur) | `full_star.webp` |
| `/drapeaux/<…>` | drapeaux (convention non confirmée) | — |

> Le mapping nom→fichier n'est pas garanti pour tous les libellés (accents, caractères spéciaux) :
> prévoir un repli si l'image renvoie **404**.
