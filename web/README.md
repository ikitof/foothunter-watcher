# Foothunter Analyzer — front web

Front web hébergé (https://analyzer.wiriath.com) reprenant les fonctionnalités de l'app :
**live de tous les matchs**, **explorer de stats par rôle**, **simulateur de mercato**, et
**palmarès**. Image Docker **indépendante** du desktop/mobile.

## Architecture
- **SPA** (Vue 3, `static/index.html`) servie par le backend, donc **même origine**.
- **Backend** FastAPI (`server.py`) qui **réutilise le cœur** (`foot_scores` + `fh_mercato`).
- La SPA appelle uniquement `/api/...` (même origine) → le backend appelle le cœur → l'API
  du jeu. Le navigateur ne touche jamais l'API HTTP du jeu : pas de *mixed-content*
  (page https vs API http), pas de CORS.

```
navigateur ──https──> analyzer.wiriath.com (SPA + /api) ──> foot_scores ──> API du jeu
```

## Lancer (recommandé : tout-en-un avec TLS)
`docker compose` démarre **deux** conteneurs : `analyzer` (le front, port interne 8000) et
`caddy` (reverse-proxy HTTPS auto via Let's Encrypt, ports 80/443).
```bash
cd web && docker compose up -d --build
```
Pré-requis : DNS `analyzer.wiriath.com` → IP publique de la machine, et ports **80 + 443**
routés depuis le routeur vers la machine (Caddy gère le certificat tout seul, cf. `Caddyfile`).

### Dev local (sans TLS)
```bash
# depuis la racine du repo
docker build -f web/Dockerfile -t foothunter-analyzer .
docker run -d -p 8000:8000 -v analyzer-data:/data --name analyzer foothunter-analyzer
```
La SPA est alors sur `http://localhost:8000/`, l'API documentée sur `/api/docs`.

### Caddy installé sur l'hôte (au lieu du conteneur)
Si tu préfères un Caddy système, remplace dans `Caddyfile` `analyzer:8000` par `localhost:8000`
(et expose le port 8000 du conteneur analyzer). Ne JAMAIS exposer 8000 directement à Internet.

## Variables d'environnement
| Var | Défaut | Rôle |
|-----|--------|------|
| `FH_WEB_DATA` | `/data` | dossier des mercatos sauvegardés (sqlite `mercato.db`) |
| `FH_WEB_ORIGINS` | `https://analyzer.wiriath.com` | origines CORS autorisées (séparées par `,`) |

## Sauvegarde de mercato (sans compte ni mot de passe)
- **Auto** : l'effectif est conservé dans le `localStorage` du navigateur (retrouvé au retour).
- **Partage / multi-appareils** : bouton « Sauvegarder » → un **code court** ; le mercato est
  stocké côté serveur (sqlite) sous ce code. On le recharge en saisissant le code, ou via
  l'URL `https://analyzer.wiriath.com/?m=CODE` (bouton « copier le lien »).

## Fonctionnalités (parité avec l'app desktop/mobile)
Live (tous les matchs, occasion = ⚡ + surbrillance ambre sur l'équipe qui peut marquer),
résultats + calendrier + classement par compétition, **stats d'équipe** (buts/encaissés/
possession/conversion/arrêts/clean/occasions), **explorer** de joueurs par rôle (multi-ligues,
adversité tous/décisifs), **évolution de célébrité** par saison + historique de clubs,
**mercato** (7 postes, bilan + investissement par domaine, sauvegarde par code), **palmarès**.

## Endpoints
`GET /api/state` · `GET /api/live` · `GET /api/competition/{nom}` · `GET /api/teams/{nom}` ·
`GET /api/scout?poste=&leagues=&adv=&pmax=` · `GET /api/players` · `GET /api/evolution` ·
`GET /api/palmares` · `POST /api/mercato/evaluate` · `POST /api/mercato/save` ·
`GET /api/mercato/{code}`
