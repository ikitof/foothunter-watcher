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
import re
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


_palmares = {"data": None, "season": None, "ts": 0.0}
_palmares_lock = threading.Lock()


def palmares_cached():
    with _palmares_lock:
        now = time.time()
        if (_palmares["data"] is None or _palmares["season"] != core.SEASON
                or now - _palmares["ts"] > 1800):   # invalide aussi au changement de saison
            _palmares["data"] = core.palmares_data(top=5)
            _palmares["season"] = core.SEASON
            _palmares["ts"] = now
        return _palmares["data"]


def _league_domstats(league):
    """Stats par domaine des équipes d'un championnat (saison courante)."""
    if not league:
        return {}
    groups, _ = core.fetch_competition(league)
    return core.team_domain_stats(groups)


# Stats d'équipe par domaine PAR SAISON et PAR COMPÉTITION, depuis les matchs joués de chaque
# saison (terminées + courante). Sert aux « stats du rôle PAR SAISON » de la fiche joueur : on
# regarde l'équipe où il était CETTE saison-là, pas son club actuel. Cache invalidé au
# changement de saison (comme player_pool) ou après 30 min.
_sdm = {"data": None, "season": None, "ts": 0.0}
_sdm_lock = threading.Lock()


def _season_dom_map():
    with _sdm_lock:
        now = time.time()
        if (_sdm["data"] is None or _sdm["season"] != core.SEASON
                or now - _sdm["ts"] > 1800):
            out = {}

            def add(sn, matches):
                bycomp = {}
                for o in (matches or []):
                    bycomp.setdefault(o.get("competition") or "?", []).append(o)
                out[sn] = {c: core.season_domstats_from_api(ms) for c, ms in bycomp.items()}

            for skey, lst in (core.api_all_matchs() or {}).items():     # saisons terminées
                sn = int("".join(ch for ch in skey if ch.isdigit()) or "-1")
                if sn >= 0:
                    add(sn, lst)
            add(core.SEASON, core.api_season_matches(core.SEASON))      # saison courante
            _sdm["data"] = out
            _sdm["season"] = core.SEASON
            _sdm["ts"] = now
        return _sdm["data"]


def _team_season_dom(season, team):
    """(championnat, {équipe: domstats}) où jouait `team` cette saison-là (championnat
    prioritaire : round-robin -> classement comparable), ou (None, None) si introuvable."""
    smap = _season_dom_map().get(season) or {}
    for comp, dom in smap.items():
        if comp in merc.SCOUT_LEAGUES and team in dom:
            return comp, dom
    return None, None


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
    return {k: m.get(k) for k in ("a", "b", "mid", "status", "poss", "occ", "site_live",
                                  "imminent_dom", "imminent_ext")}


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
            "game_base": core.BASE_URL,
            "scout_leagues": list(merc.SCOUT_LEAGUES), "majors": list(merc.MAJOR_LEAGUES)}


@app.get("/api/live")
def live():
    """Tous les matchs EN DIRECT, toutes compétitions (occas_* = but imminent)."""
    return {"matches": core.api_live_matchs()}


@app.get("/api/competition/{name}")
def competition(name: str):
    groups, _ = core.fetch_competition(name)
    # leaderboard() = classement brut (team/played/points/gf/ga/gd...) attendu par la SPA,
    # calculé depuis les résultats — y compris partiel pour la saison en cours.
    standings = core.leaderboard(groups)
    for s in standings:   # forme = 5 derniers résultats V/N/D (ordre chronologique)
        played = (core.team_history(groups, s["team"]) or {}).get("played") or []
        s["form"] = [m.get("res") for m in played][-5:]
    return {
        "name": name,
        "groups": [{"label": g["label"], "matches": [_match(m) for m in g["matches"]]}
                   for g in groups],
        "standings": standings,
    }


@app.get("/api/teams/{name}")
def teams(name: str):
    groups, _ = core.fetch_competition(name)
    tds = core.team_domain_stats(groups)
    return {"name": name, "teams": list(tds.values())}  # chaque dict contient déjà "team"


@app.get("/api/team/{name}")
def team(name: str):
    """Détail d'une équipe : effectif (7 joueurs, ordre des postes) + agrégats + répartition
    salariale par domaine + classement par catégorie dans son championnat (attaque, défense,
    possession, finition, arrêts…)."""
    squad = sorted((p for p in player_pool() if p.get("nom_equipe") == name),
                   key=lambda p: merc.TEAM_POSTES.index(p["poste"])
                   if p.get("poste") in merc.TEAM_POSTES else 99)
    salaries = [(p.get("poste"), p.get("salaire")) for p in squad]
    league = _club_league_map().get(name)
    rankings = core.rank_in_league(_league_domstats(league), name) if league else None
    return {"team": name, "squad": squad, "aggregate": merc.squad_aggregate(squad),
            "investment": merc.team_domain_investment(salaries),
            "domains": merc.DOMAINS, "domain_labels": merc.DOMAIN_LABELS,
            "league": league, "rankings": rankings,
            "percent": list(core.PERCENT_STATS)}


@app.get("/api/scout")
def scout(poste: str, leagues: str = "", adv: str = "tous", pmax: float = 1e12):
    if poste not in merc.ROLE_RELEVANCE:
        raise HTTPException(400, "poste inconnu")
    if pmax != pmax or pmax <= 0:   # NaN (champ vidé) ou ≤0 -> illimité (pas de filtre muet)
        pmax = 1e12
    lg = [x for x in (leagues.split(",") if leagues else []) if x] or list(merc.MAJOR_LEAGUES)
    lg = [x for x in lg if x in merc.SCOUT_LEAGUES]
    rows = core.role_scout_multi(lg, poste, player_pool())
    rows = [r for r in rows if r.get("salaire") is not None and r["salaire"] <= pmax]
    rows.sort(key=lambda r: (r.get("stat") is None, -(r.get("stat") or 0)))
    stat_key, stat_label, counters = merc.ROLE_RELEVANCE[poste]
    role_defs = core.POSTE_STATS.get(poste) or []
    return {"poste": poste, "leagues": lg, "stat_label": stat_label, "stat_key": stat_key,
            "counters": counters, "adv_key": "opp_dec" if adv == "decisifs" else "opp",
            # toutes les stats d'équipe pertinentes pour le poste (colonnes du tableau),
            # avec la part (%) que ce poste apporte à chaque stat (matrice domaines, manuel)
            "role_keys": [{"label": lbl, "key": k, "higher": hb,
                           "contrib": merc.poste_stat_contribution(poste, k)}
                          for lbl, k, hb in role_defs],
            "role_percent": list(core.PERCENT_STATS),
            "rows": rows[:300]}


@app.get("/api/players")
def players():
    return {"season": core.SEASON, "players": player_pool(),
            "postes": list(merc.TEAM_POSTES)}


_evo = {"data": None, "careers": None, "season": None, "ts": 0.0}
_evo_lock = threading.Lock()


def _club_league_map():
    """club -> championnat (SCOUT_LEAGUES) pour la saison courante, via le calendrier."""
    m = {}
    for comp, phases in (core.api_calendrier(core.SEASON) or {}).items():
        if comp not in merc.SCOUT_LEAGUES:   # championnats seulement (pas les coupes)
            continue
        for fixtures in phases.values():
            for fx in fixtures:
                for k in ("nom_equipe_dom", "nom_equipe_ext"):
                    t = fx.get(k)
                    if t:
                        m.setdefault(t, comp)
    return m


def _evo_state():
    """Carrières + table d'évolution, reconstruites depuis /infos_all_joueurs sur toutes les
    saisons. Identité = (nom, poste) — l'« id » de l'API est un EMPLACEMENT réutilisé d'une
    saison à l'autre (un même id a hébergé jusqu'à 3 joueurs différents), donc grouper par id
    fabriquait des « carrières » mélangeant plusieurs joueurs (~87% des lignes faussées).
    Cache invalidé au changement de saison (comme player_pool) ou après 30 min."""
    with _evo_lock:
        now = time.time()
        if (_evo["data"] is None or _evo["season"] != core.SEASON
                or now - _evo["ts"] > 1800):
            seasons = list(range(core.SEASON + 1))
            leagues = _club_league_map()
            careers = core.build_player_careers(
                {sn: core.api_all_joueurs(sn) for sn in seasons})
            rows = []
            for c in careers.values():
                cel = c["cel"]
                if not cel:
                    continue
                pres = sorted(cel)                                  # saisons avec célébrité
                club = c["club"].get(max(c["club"])) if c["club"] else None
                rows.append({
                    "key": c["nom"] + "|" + c["poste"],             # clé stable pour v-for
                    "nom": c["nom"], "poste": c["poste"], "club": club,
                    "league": leagues.get(club),
                    "celebrite": cel,
                    "salaire_cur": c["sal"].get(max(c["sal"])) if c["sal"] else None,
                    # variation = dernière − première saison ; None si une seule saison
                    # (un joueur nouveau ne doit pas afficher « 0 » comme un vétéran stable)
                    "var": round(cel[pres[-1]] - cel[pres[0]], 1) if len(pres) >= 2 else None,
                    "peak": max(cel.values()),
                    "debut": pres[0],                               # 1re saison connue
                    "is_new": len(pres) == 1 and pres[0] == core.SEASON,
                })
            rows.sort(key=lambda e: -e["peak"])
            _evo["careers"] = careers
            _evo["data"] = {"seasons": seasons, "players": rows,
                            "leagues": sorted({v for v in leagues.values() if v})}
            _evo["season"] = core.SEASON
            _evo["ts"] = now
        return _evo


def evolution_cached():
    return _evo_state()["data"]


@app.get("/api/evolution")
def evolution():
    return evolution_cached()


def _role_history(career):
    """Stats du rôle PAR SAISON pour un joueur : pour chaque saison de sa carrière, les stats
    d'ÉQUIPE pertinentes pour son poste, calculées au club où il était CETTE saison-là (et non
    son club actuel), avec le rang de cette équipe dans son championnat. Factuel — manuel : tout
    domaine sauf Arrêts dépend de PLUSIEURS joueurs, donc ce sont des stats d'équipe, jamais
    individuelles. Renvoie {season: {league, stats:{key:val}, ranks:{key:{rank,n}}}}."""
    poste = (career.get("poste") or "").upper()
    defs = core.POSTE_STATS.get(poste)
    if not defs:
        return {}
    cats = [(k, lbl, hb) for lbl, k, hb in defs]
    hist = {}
    for s in career["seasons"]:
        club = career["club"].get(s)
        if not club:
            continue
        comp, dom = _team_season_dom(s, club)
        if not dom or club not in dom:
            continue
        teamrow = dom[club]
        ranks = {r["key"]: {"rank": r["rank"], "n": r["n"]}
                 for r in (core.rank_in_league(dom, club, cats) or [])}
        hist[s] = {"league": comp,
                   "stats": {k: teamrow.get(k) for _lbl, k, _hb in defs},
                   "ranks": ranks}
    return hist


@app.get("/api/player/{nom}")
def player(nom: str, poste: str = ""):
    """Détail FACTUEL d'un joueur (identité (nom, poste)) : club/célé/salaire/âge actuels,
    historique par saison (club, célébrité, salaire, âge), transferts, percentiles de
    célébrité/salaire parmi les joueurs du même poste, et stats d'ÉQUIPE pertinentes pour le
    rôle PAR SAISON (au club de l'époque + rang en championnat). Reste joignable même pour un
    joueur qui n'est plus dans la saison courante (ex. retraité/remplacé)."""
    nom = (nom or "").strip()
    state = _evo_state()
    careers = state["careers"]
    poste = (poste or "").upper()
    # carrière(s) portant ce nom (désambiguïsation par poste si fourni ou si homonymes)
    cands = [c for (cn, cp), c in careers.items() if cn == nom and (not poste or cp == poste)]
    if not cands:
        cands = [c for (cn, cp), c in careers.items() if cn == nom]
    if not cands:
        raise HTTPException(404, "joueur introuvable")
    car = max(cands, key=lambda c: len(c["seasons"]))   # la carrière la plus fournie
    cel = car["cel"]
    last = max(cel) if cel else None
    cur_club = car["club"].get(max(car["club"])) if car["club"] else None
    # percentiles de célébrité / salaire parmi les joueurs du MÊME poste (saison courante)
    pool = player_pool()
    pct = {}
    if last == core.SEASON:
        for fld in ("celebrite", "salaire"):
            pc = core.position_percentile(pool, car["poste"], fld, (cel.get(last) if fld == "celebrite"
                                          else car["sal"].get(last)))
            if pc:
                pct[fld] = pc
    role_defs = core.POSTE_STATS.get(car["poste"]) or []
    role_hist = _role_history(car)
    return {
        "nom": car["nom"], "poste": car["poste"], "club": cur_club,
        "league": (role_hist.get(last) or {}).get("league"),
        "celebrite": cel.get(last) if last is not None else None,
        "salaire": car["sal"].get(max(car["sal"])) if car["sal"] else None,
        "montant_transfert": car["mt"].get(max(car["mt"])) if car["mt"] else None,  # prix d'achat
        "age": car["age"].get(max(car["age"])) if car["age"] else None,
        "history": cel, "clubs": car["club"], "salaries": car["sal"], "ages": car["age"],
        "montants": car["mt"],
        "seasons": state["data"]["seasons"], "career_seasons": car["seasons"],
        "transfers": core.career_transfers(car),
        "debut": car["seasons"][0] if car["seasons"] else None,
        "debut_age": car["age"].get(car["seasons"][0]) if car["seasons"] else None,
        "peak": max(cel.values()) if cel else None,
        "peak_season": max(cel, key=cel.get) if cel else None,
        "active": last == core.SEASON,
        "percentiles": pct,
        # stats du rôle (équipe) par saison, au club de l'époque
        "role_keys": [{"label": lbl, "key": k, "higher": hb} for lbl, k, hb in role_defs],
        "role_percent": list(core.PERCENT_STATS),
        "role_hist": role_hist,
        "namesakes": sorted({c["poste"] for c in cands}) if len(cands) > 1 else [],
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
    modes = body.get("modes") or {}        # {poste: 'louer'|'acheter'} (défaut 'louer')
    total, salaries = 0.0, []
    for poste, p in squad.items():
        p = p or {}
        cost = merc.recruit_cost(modes.get(poste, "louer"),
                                 p.get("salaire"), p.get("montant_transfert"))
        if cost:
            total += cost
        salaries.append((poste, p.get("salaire")))   # domaine = masse salariale ANNUELLE
    return {
        "aggregate": merc.squad_aggregate([p for p in squad.values() if p]),
        "investment": merc.team_domain_investment(salaries),
        "domains": merc.DOMAINS, "domain_labels": merc.DOMAIN_LABELS,
        "total_cost": round(total, 2),
    }


@app.post("/api/mercato/save")
def mercato_save(body: dict = Body(...)):
    code = (body.get("code") or "").strip() or secrets.token_hex(3)
    if not re.fullmatch(r"[A-Za-z0-9]{1,16}", code):   # ASCII alnum only (URL-safe, sans ambiguïté)
        raise HTTPException(400, "code invalide")
    payload = json.dumps({"squad": body.get("squad") or {}, "modes": body.get("modes") or {}})
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
