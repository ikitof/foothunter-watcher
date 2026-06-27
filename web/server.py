#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backend web « Foothunter Analyzer ».

API JSON qui RÉUTILISE le cœur (foot_scores + fh_mercato) — zéro dépendance au desktop
(tkinter) / mobile (kivy) — et sert la SPA. Architecture : la SPA (même origine, servie
par ce backend) appelle /api/... ; ce backend appelle le cœur, qui appelle l'API du jeu.
Le navigateur ne touche jamais l'API HTTP du jeu (pas de mixed-content https->http, pas de
CORS). Image Docker indépendante du reste de l'app.
"""
import json
import os
import secrets
import threading
import time
from contextlib import closing
import sqlite3

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import foot_scores as core
import fh_mercato as merc

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
DATA_DIR = os.environ.get("FH_WEB_DATA", os.path.join(HERE, "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "mercato.db")

app = FastAPI(title="Foothunter Analyzer", docs_url="/api/docs", redoc_url=None)

# Même origine en prod (la SPA est servie ici) -> CORS surtout utile en dev. Configurable.
_origins = [o.strip() for o in os.environ.get(
    "FH_WEB_ORIGINS", "https://analyzer.wiriath.com").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_origins,
                   allow_methods=["GET", "POST"], allow_headers=["*"])


# ----------------------------------------------------------------------------
# Caches (le cœur a déjà un cache TTL sur les appels API du jeu ; on cache en plus le pool
# joueurs et le palmarès, plus lourds à recomposer).
# ----------------------------------------------------------------------------
_pool = {"season": None, "data": None, "ts": 0.0}
_pool_lock = threading.Lock()


def player_pool():
    with _pool_lock:
        now = time.time()
        if (_pool["data"] is None or _pool["season"] != core.SEASON
                or now - _pool["ts"] > 600):
            _pool["data"] = core.api_all_joueurs(core.SEASON)
            _pool["season"] = core.SEASON
            _pool["ts"] = now
        return _pool["data"]


_palmares = {"data": None, "ts": 0.0}
_palmares_lock = threading.Lock()


def palmares_cached():
    with _palmares_lock:
        now = time.time()
        if _palmares["data"] is None or now - _palmares["ts"] > 1800:
            _palmares["data"] = core.palmares_data(top=5)
            _palmares["ts"] = now
        return _palmares["data"]


def _db():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS mercato "
                "(code TEXT PRIMARY KEY, payload TEXT, created REAL, updated REAL)")
    return con


@app.on_event("startup")
def _startup():
    try:
        core.refresh_current_season()
    except Exception:
        pass


def _match(m):
    return {k: m.get(k) for k in ("a", "b", "mid", "status", "poss", "occ", "site_live")}


# ----------------------------------------------------------------------------
# Endpoints data (réutilisent le cœur). Handlers SYNC -> FastAPI les exécute dans un
# threadpool, donc les I/O réseau bloquantes du cœur ne gèlent pas la boucle asyncio.
# ----------------------------------------------------------------------------
@app.get("/api/healthz")
def healthz():
    return {"ok": True, "season": core.SEASON}


@app.get("/api/state")
def state():
    try:
        core.refresh_current_season()
    except Exception:
        pass
    return {"season": core.SEASON, "competitions": core.fetch_competitions(core.SEASON),
            "game_base": core.BASE_URL}


@app.get("/api/live")
def live():
    """Tous les matchs EN DIRECT, toutes compétitions (occas_* = but imminent)."""
    return {"matches": core.api_live_matchs()}


@app.get("/api/competition/{name}")
def competition(name: str):
    groups, _ = core.fetch_competition(name)
    # leaderboard() = classement brut (team/played/points/gf/ga/gd...) attendu par la SPA,
    # calculé depuis les résultats — y compris partiel pour la saison en cours.
    return {
        "name": name,
        "groups": [{"label": g["label"], "matches": [_match(m) for m in g["matches"]]}
                   for g in groups],
        "standings": core.leaderboard(groups),
    }


@app.get("/api/teams/{name}")
def teams(name: str):
    groups, _ = core.fetch_competition(name)
    tds = core.team_domain_stats(groups)
    return {"name": name, "teams": list(tds.values())}  # chaque dict contient déjà "team"


@app.get("/api/team/{name}")
def team(name: str):
    """Détail d'une équipe : effectif (7 joueurs, ordre des postes) + agrégats."""
    squad = sorted((p for p in player_pool() if p.get("nom_equipe") == name),
                   key=lambda p: merc.TEAM_POSTES.index(p["poste"])
                   if p.get("poste") in merc.TEAM_POSTES else 99)
    return {"team": name, "squad": squad, "aggregate": merc.squad_aggregate(squad)}


@app.get("/api/scout")
def scout(poste: str, leagues: str = "", adv: str = "tous", pmax: float = 1e12):
    if poste not in merc.ROLE_RELEVANCE:
        raise HTTPException(400, "poste inconnu")
    lg = [x for x in (leagues.split(",") if leagues else []) if x] or list(merc.MAJOR_LEAGUES)
    lg = [x for x in lg if x in merc.SCOUT_LEAGUES]
    rows = core.role_scout_multi(lg, poste, player_pool())
    rows = [r for r in rows if r.get("salaire") is not None and r["salaire"] <= pmax]
    rows.sort(key=lambda r: (r.get("stat") is None, -(r.get("stat") or 0)))
    stat_key, stat_label, counters = merc.ROLE_RELEVANCE[poste]
    return {"poste": poste, "leagues": lg, "stat_label": stat_label,
            "counters": counters, "adv_key": "opp_dec" if adv == "decisifs" else "opp",
            "rows": rows[:300]}


@app.get("/api/players")
def players():
    return {"season": core.SEASON, "players": player_pool(),
            "postes": list(merc.TEAM_POSTES)}


_evo = {"data": None, "ts": 0.0}
_evo_lock = threading.Lock()


def evolution_cached():
    """Historique de célébrité + clubs par joueur, reconstruit depuis /infos_all_joueurs
    sur toutes les saisons (clé = id stable). Web-only : ne touche pas aux scrapers de l'app."""
    with _evo_lock:
        now = time.time()
        if _evo["data"] is None or now - _evo["ts"] > 1800:
            seasons = list(range(core.SEASON + 1))
            by_id = {}
            for sn in seasons:
                for p in core.api_all_joueurs(sn):
                    pid = p.get("id")
                    if pid is None:
                        continue
                    e = by_id.setdefault(pid, {"id": pid, "nom": None, "poste": None,
                                               "celebrite": {}, "clubs": {}})
                    e["nom"] = p.get("nom") or e["nom"]
                    e["poste"] = p.get("poste") or e["poste"]
                    if p.get("celebrite") is not None:
                        e["celebrite"][sn] = p["celebrite"]
                    if p.get("nom_equipe"):
                        e["clubs"][sn] = p["nom_equipe"]
            players = []
            for e in by_id.values():
                if not e["nom"] or not e["celebrite"]:
                    continue
                ls = max(e["clubs"]) if e["clubs"] else None
                e["club"] = e["clubs"].get(ls) if ls is not None else None
                e["peak"] = max(e["celebrite"].values())
                players.append(e)
            players.sort(key=lambda e: -e["peak"])
            _evo["data"] = {"seasons": seasons, "players": players}
            _evo["ts"] = now
        return _evo["data"]


@app.get("/api/evolution")
def evolution():
    return evolution_cached()


@app.get("/api/player/{nom}")
def player(nom: str):
    """Détail d'un joueur : poste, club + célé/salaire/âge actuels, et l'historique
    (célébrité + clubs par saison)."""
    data = evolution_cached()
    cur = next((p for p in player_pool() if p.get("nom") == nom), None)
    hist = next((p for p in data["players"] if p.get("nom") == nom), None)
    if not cur and not hist:
        raise HTTPException(404, "joueur introuvable")
    cel = (hist or {}).get("celebrite") or {}
    last = max(cel) if cel else None
    return {
        "nom": nom,
        "poste": (cur or hist or {}).get("poste"),
        "club": (cur or {}).get("nom_equipe") or (hist or {}).get("club"),
        "celebrite": (cur or {}).get("celebrite") if cur else (cel.get(last) if last is not None else None),
        "salaire": (cur or {}).get("salaire"),
        "age": (cur or {}).get("age"),
        "history": cel,
        "clubs": (hist or {}).get("clubs") or {},
        "seasons": data["seasons"],
    }


@app.get("/api/palmares")
def palmares():
    records, n = palmares_cached()
    return {"n_matches": n, "categories": core.PALMARES_CATEGORIES, "records": records}


# ----------------------------------------------------------------------------
# Mercato : évaluation (cœur) + sauvegarde/restauration ANONYME par code court (sans
# compte ni mot de passe). La SPA garde aussi l'effectif en localStorage côté navigateur.
# ----------------------------------------------------------------------------
@app.post("/api/mercato/evaluate")
def mercato_evaluate(body: dict = Body(...)):
    squad = body.get("squad") or {}        # {poste: player}
    years = body.get("years") or {}        # {poste: 1|2|3}
    signings, total = [], 0.0
    for poste, p in squad.items():
        cost = merc.contract_cost((p or {}).get("salaire"), years.get(poste, 1))
        signings.append((poste, cost))
        if cost:
            total += cost
    return {
        "aggregate": merc.squad_aggregate([p for p in squad.values() if p]),
        "investment": merc.team_domain_investment(signings),
        "domains": merc.DOMAINS, "domain_labels": merc.DOMAIN_LABELS,
        "total_cost": round(total, 2),
    }


@app.post("/api/mercato/save")
def mercato_save(body: dict = Body(...)):
    code = (body.get("code") or "").strip() or secrets.token_hex(3)
    if not code.isalnum() or len(code) > 16:
        raise HTTPException(400, "code invalide")
    payload = json.dumps({"squad": body.get("squad") or {}, "years": body.get("years") or {}})
    if len(payload) > 100_000:
        raise HTTPException(413, "effectif trop volumineux")
    now = time.time()
    with closing(_db()) as con:
        con.execute(
            "INSERT INTO mercato(code,payload,created,updated) VALUES(?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET payload=excluded.payload, updated=excluded.updated",
            (code, payload, now, now))
        con.commit()
    return {"code": code}


@app.get("/api/mercato/{code}")
def mercato_load(code: str):
    with closing(_db()) as con:
        row = con.execute("SELECT payload FROM mercato WHERE code=?", (code,)).fetchone()
    if not row:
        raise HTTPException(404, "code introuvable")
    return json.loads(row[0])


# La SPA (catch-all) APRÈS les routes /api pour qu'elles aient la priorité.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="spa")
