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

## Lancer
```bash
# depuis la racine du repo
docker build -f web/Dockerfile -t foothunter-analyzer .
docker run -d -p 8000:8000 -v analyzer-data:/data --name analyzer foothunter-analyzer
# ou : cd web && docker compose up -d --build
```
La SPA est sur `http://localhost:8000/`, l'API documentée sur `/api/docs`.

## Reverse-proxy TLS (analyzer.wiriath.com)
Le conteneur sert du HTTP sur 8000 ; mettre un reverse-proxy devant pour le HTTPS.
Exemple **Caddy** (HTTPS auto via Let's Encrypt) :
```
analyzer.wiriath.com {
    reverse_proxy localhost:8000
}
```
(ouvrir 443 sur le routeur → la machine ; ne PAS exposer 8000 directement.)

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

## Endpoints
`GET /api/state` · `GET /api/live` · `GET /api/competition/{nom}` · `GET /api/teams/{nom}` ·
`GET /api/scout?poste=&leagues=&adv=&pmax=` · `GET /api/players` · `GET /api/palmares` ·
`POST /api/mercato/evaluate` · `POST /api/mercato/save` · `GET /api/mercato/{code}`
