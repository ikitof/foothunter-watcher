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

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

# Auth (optionnelle) : importée de façon défensive pour que l'app démarre même si les libs
# ne sont pas installées (dev) ou si les secrets Google ne sont pas configurés.
try:
    from starlette.middleware.sessions import SessionMiddleware
except Exception:                       # pragma: no cover
    SessionMiddleware = None
try:
    from authlib.integrations.starlette_client import OAuth
except Exception:                       # pragma: no cover
    OAuth = None

import foot_scores as core
import fh_mercato as merc
import fh_bets as bets

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
# Auth Google (flux OAuth côté serveur). AUTH_ENABLED=False si les secrets manquent : l'app
# fonctionne alors en lecture seule (les endpoints d'écriture renvoient 401, /api/auth/* 503).
# ----------------------------------------------------------------------------
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
PUBLIC_URL = os.environ.get("FH_PUBLIC_URL", "https://analyzer.wiriath.com").rstrip("/")
SESSION_SECRET = os.environ.get("FH_SESSION_SECRET") or secrets.token_hex(32)
# Raccourci de login POUR LE DEV UNIQUEMENT (jamais activer en prod) : /api/auth/login?dev=NOM
AUTH_STUB = os.environ.get("FH_AUTH_STUB") == "1"
AUTH_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and OAuth is not None)

if SessionMiddleware is not None:       # cookie de session signé (survit à la redirection Google)
    # https_only par défaut (prod TLS derrière Caddy) ; FH_INSECURE_COOKIE=1 pour tester en local http.
    app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, session_cookie="fh_session",
                       same_site="lax", https_only=(os.environ.get("FH_INSECURE_COOKIE") != "1"),
                       max_age=30 * 86400)

oauth = None
if AUTH_ENABLED:
    oauth = OAuth()
    oauth.register(
        name="google",
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_id=GOOGLE_CLIENT_ID, client_secret=GOOGLE_CLIENT_SECRET,
        client_kwargs={"scope": "openid email profile"})


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
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")          # meilleure concurrence en écriture
    con.execute("CREATE TABLE IF NOT EXISTS mercato "
                "(code TEXT PRIMARY KEY, payload TEXT, created REAL, updated REAL)")
    # Pronostics : comptes (identité = sub Google) + paris (clé = user+saison+compétition+match).
    con.execute("""CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT, google_sub TEXT UNIQUE NOT NULL,
        email TEXT, name TEXT, picture TEXT, created REAL, last_login REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS bets(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, season INTEGER NOT NULL,
        competition TEXT NOT NULL, home TEXT NOT NULL, away TEXT NOT NULL, phase TEXT,
        pred_home INTEGER NOT NULL, pred_away INTEGER NOT NULL,
        act_home INTEGER, act_away INTEGER, points INTEGER,
        locked INTEGER DEFAULT 0, settled INTEGER DEFAULT 0, voided INTEGER DEFAULT 0,
        created REAL, updated REAL, settled_ts REAL,
        UNIQUE(user_id, season, competition, home, away))""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_bets_settle ON bets(season, settled)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_bets_leader ON bets(season, competition, settled)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_bets_user ON bets(user_id, season)")
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


# ----------------------------------------------------------------------------
# Pronostics (paris) : comptes Google, pose de paris sur les matchs à venir, règlement
# automatique (piloté par le polling — l'API n'a ni id ni date de match), classement.
# ----------------------------------------------------------------------------
_MAX_GOALS = 30                          # borne de saisie (anti-abus)


def _upsert_user(sub, email, name, picture):
    now = time.time()
    with closing(_db()) as con:
        con.execute(
            "INSERT INTO users(google_sub,email,name,picture,created,last_login) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(google_sub) DO UPDATE SET email=excluded.email,name=excluded.name,"
            "picture=excluded.picture,last_login=excluded.last_login",
            (sub, email, name, picture, now, now))
        con.commit()
        row = con.execute("SELECT id,name,email,picture FROM users WHERE google_sub=?",
                          (sub,)).fetchone()
    return {"id": row[0], "name": row[1], "email": row[2], "picture": row[3]}


def current_user(request):
    """Utilisateur connecté (via le cookie de session signé) ou None."""
    uid = request.session.get("uid") if hasattr(request, "session") else None
    if not uid:
        return None
    with closing(_db()) as con:
        row = con.execute("SELECT id,name,email,picture FROM users WHERE id=?", (uid,)).fetchone()
    return {"id": row[0], "name": row[1], "email": row[2], "picture": row[3]} if row else None


def _require_user(request):
    u = current_user(request)
    if not u:
        raise HTTPException(401, "connexion requise")
    return u


# Limitation de débit par utilisateur (token bucket en mémoire) pour la pose de paris.
_rl, _rl_lock = {}, threading.Lock()


def _rate_ok(uid, rate=1.0, burst=8):
    now = time.time()
    with _rl_lock:
        tok, last = _rl.get(uid, (burst, now))
        tok = min(burst, tok + (now - last) * rate)
        if tok < 1:
            _rl[uid] = (tok, now)
            return False
        _rl[uid] = (tok - 1, now)
        return True


# --- Règlement automatique (idempotent, paresseux, throttlé — pas de worker) ---
_settle = {"ts": 0.0, "lock": threading.Lock()}


def _played_rows(season):
    """Matchs JOUÉS d'une saison, normalisés pour fh_bets.build_played_index."""
    raw = list(core.api_season_matches(season) or [])
    if not raw:
        raw = list((core.api_all_matchs() or {}).get(f"saison{season}") or [])
    out = []
    for o in raw:
        sd, se = core._num(o.get("Score dom")), core._num(o.get("Score ext"))
        out.append({"competition": o.get("competition"), "home": o.get("Equipe dom"),
                    "away": o.get("Equipe ext"),
                    "home_goals": int(sd) if sd is not None else None,
                    "away_goals": int(se) if se is not None else None})
    return out


def settle_bets(season):
    """Règle les paris de `season` : score final -> points ; live/score partiel -> verrou ;
    saison finie + match jamais joué -> annulé (0 pt, hors classement). Ré-exécutable sans
    double comptage (toutes les écritures sont gardées par settled=0)."""
    played = bets.build_played_index(_played_rows(season))
    live = {(o.get("competition"), o.get("nom_equipe_dom"), o.get("nom_equipe_ext"))
            for o in (core.api_live_matchs() or [])}
    finished = season < core.SEASON
    now = time.time()
    with closing(_db()) as con:
        rows = con.execute("SELECT id,competition,home,away,pred_home,pred_away FROM bets "
                           "WHERE season=? AND settled=0", (season,)).fetchall()
        for bid, comp, home, away, ph, pa in rows:
            key = (comp, home, away)
            if key in played:
                hg, ag = played[key]
                pts = bets.bet_points(ph, pa, hg, ag)
                con.execute("UPDATE bets SET act_home=?,act_away=?,points=?,locked=1,settled=1,"
                            "settled_ts=?,updated=? WHERE id=?", (hg, ag, pts, now, now, bid))
            elif key in live:
                con.execute("UPDATE bets SET locked=1,updated=? WHERE id=?", (now, bid))
            elif finished:
                con.execute("UPDATE bets SET voided=1,settled=1,points=0,settled_ts=?,updated=? "
                            "WHERE id=?", (now, now, bid))
        con.commit()


def maybe_settle():
    if time.time() - _settle["ts"] < 12:                 # throttle (~ TTL du live/résultats)
        return
    if not _settle["lock"].acquire(blocking=False):
        return
    try:
        with closing(_db()) as con:
            seasons = [r[0] for r in con.execute(
                "SELECT DISTINCT season FROM bets WHERE settled=0").fetchall()]
        for s in seasons:
            settle_bets(s)
        _settle["ts"] = time.time()
    except Exception:
        pass
    finally:
        _settle["lock"].release()


# --- Auth Google (flux OAuth côté serveur) ---
@app.get("/api/auth/login")
async def auth_login(request: Request, dev: str = ""):
    if AUTH_STUB and dev:                # RACCOURCI DEV UNIQUEMENT
        u = _upsert_user("dev:" + dev, dev + "@dev.local", dev, "")
        request.session["uid"] = u["id"]
        return RedirectResponse("/")
    if not AUTH_ENABLED:
        raise HTTPException(503, "authentification non configurée")
    return await oauth.google.authorize_redirect(request, PUBLIC_URL + "/api/auth/callback")


@app.get("/api/auth/callback")
async def auth_callback(request: Request):
    if not AUTH_ENABLED:
        raise HTTPException(503, "authentification non configurée")
    try:
        token = await oauth.google.authorize_access_token(request)   # valide code+state+id_token
    except Exception:
        raise HTTPException(400, "échec de la connexion Google")
    info = token.get("userinfo") or {}
    sub = info.get("sub")
    if not sub:
        raise HTTPException(400, "profil Google incomplet")
    u = _upsert_user(sub, info.get("email"),
                     info.get("name") or info.get("email") or "Joueur", info.get("picture"))
    request.session["uid"] = u["id"]
    return RedirectResponse("/")


@app.get("/api/auth/me")
def auth_me(request: Request):
    return {"auth_enabled": bool(AUTH_ENABLED or AUTH_STUB), "user": current_user(request)}


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    try:
        request.session.clear()
    except Exception:
        pass
    return {"ok": True}


# --- Paris ---
def _bettable_status(comp, home, away):
    """Statut du match (home, away) dans `comp` : ('scheduled'|'live'|'result', phase) ou
    (None, None) s'il est introuvable."""
    groups, _ = core.fetch_competition(comp)
    for g in groups:
        for m in g["matches"]:
            if m.get("a") == home and m.get("b") == away:
                if m.get("status") == "scheduled":
                    return "scheduled", g["label"]
                return ("live" if m.get("site_live") else "result"), g["label"]
    return None, None


@app.get("/api/bets/fixtures/{competition}")
def bets_fixtures(competition: str, request: Request):
    maybe_settle()
    u = current_user(request)
    uid = u["id"] if u else None
    groups, _ = core.fetch_competition(competition)
    fixtures = [{"home": m.get("a"), "away": m.get("b"), "phase": g["label"]}
                for g in groups for m in g["matches"] if m.get("status") == "scheduled"]
    mine, my_bets = {}, []
    if uid:
        with closing(_db()) as con:
            for r in con.execute(
                "SELECT home,away,phase,pred_home,pred_away,act_home,act_away,points,locked,settled,voided "
                "FROM bets WHERE user_id=? AND season=? AND competition=?",
                    (uid, core.SEASON, competition)).fetchall():
                my_bets.append({"home": r[0], "away": r[1], "phase": r[2], "pred_home": r[3],
                                "pred_away": r[4], "act_home": r[5], "act_away": r[6],
                                "points": r[7], "locked": bool(r[8]), "settled": bool(r[9]),
                                "voided": bool(r[10])})
                mine[(r[0], r[1])] = {"pred_home": r[3], "pred_away": r[4]}
    for f in fixtures:
        f["my_bet"] = mine.get((f["home"], f["away"]))
    return {"competition": competition, "season": core.SEASON,
            "fixtures": fixtures, "my_bets": my_bets, "authed": bool(uid)}


@app.post("/api/bets")
def bets_place(request: Request, body: dict = Body(...)):
    u = _require_user(request)
    maybe_settle()
    if not _rate_ok(u["id"]):
        raise HTTPException(429, "trop de paris, réessaie dans un instant")
    comp = (body.get("competition") or "").strip()
    home = (body.get("home") or "").strip()
    away = (body.get("away") or "").strip()
    try:
        ph, pa = int(body.get("pred_home")), int(body.get("pred_away"))
    except (TypeError, ValueError):
        raise HTTPException(400, "score invalide")
    if not comp or not home or not away:
        raise HTTPException(400, "match invalide")
    if not (0 <= ph <= _MAX_GOALS and 0 <= pa <= _MAX_GOALS):
        raise HTTPException(400, "score hors limites")
    status, phase = _bettable_status(comp, home, away)
    if status != "scheduled":            # verrou piloté par le polling (live/joué/inexistant)
        raise HTTPException(409, "match verrouillé ou introuvable")
    now = time.time()
    with closing(_db()) as con:
        con.execute(
            "INSERT INTO bets(user_id,season,competition,home,away,phase,pred_home,pred_away,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,season,competition,home,away) DO UPDATE SET "
            "pred_home=excluded.pred_home,pred_away=excluded.pred_away,phase=excluded.phase,"
            "updated=excluded.updated WHERE bets.locked=0 AND bets.settled=0",
            (u["id"], core.SEASON, comp, home, away, phase, ph, pa, now, now))
        con.commit()
    return {"ok": True, "pred_home": ph, "pred_away": pa}


@app.post("/api/bets/remove")
def bets_remove(request: Request, body: dict = Body(...)):
    u = _require_user(request)
    comp = (body.get("competition") or "").strip()
    home = (body.get("home") or "").strip()
    away = (body.get("away") or "").strip()
    with closing(_db()) as con:
        cur = con.execute("DELETE FROM bets WHERE user_id=? AND season=? AND competition=? AND "
                          "home=? AND away=? AND locked=0 AND settled=0",
                          (u["id"], core.SEASON, comp, home, away))
        con.commit()
    if cur.rowcount == 0:
        raise HTTPException(409, "pari verrouillé ou introuvable")
    return {"ok": True}


@app.get("/api/bets/mine")
def bets_mine(request: Request):
    u = _require_user(request)
    maybe_settle()
    with closing(_db()) as con:
        rows = con.execute(
            "SELECT competition,home,away,phase,pred_home,pred_away,act_home,act_away,points,"
            "locked,settled,voided FROM bets WHERE user_id=? AND season=? ORDER BY competition",
            (u["id"], core.SEASON)).fetchall()
    lst = [{"competition": r[0], "home": r[1], "away": r[2], "phase": r[3], "pred_home": r[4],
            "pred_away": r[5], "act_home": r[6], "act_away": r[7], "points": r[8],
            "locked": bool(r[9]), "settled": bool(r[10]), "voided": bool(r[11])} for r in rows]
    total = sum(b["points"] or 0 for b in lst if b["settled"] and not b["voided"])
    return {"season": core.SEASON, "bets": lst, "total": total}


@app.get("/api/leaderboard")
def bets_leaderboard(request: Request, competition: str = ""):
    maybe_settle()
    me = current_user(request)
    me_id = me["id"] if me else None
    q = ("SELECT u.id,u.name,u.picture,COALESCE(SUM(b.points),0) pts,COUNT(*) n,"
         "SUM(CASE WHEN b.points>0 THEN 1 ELSE 0 END) good "
         "FROM bets b JOIN users u ON u.id=b.user_id "
         "WHERE b.season=? AND b.settled=1 AND b.voided=0")
    args = [core.SEASON]
    if competition:
        q += " AND b.competition=?"
        args.append(competition)
    q += " GROUP BY u.id ORDER BY pts DESC, good DESC, n ASC"
    with closing(_db()) as con:
        rows = con.execute(q, args).fetchall()
    out = [{"rank": i, "name": r[1], "picture": r[2], "points": r[3], "n": r[4], "good": r[5],
            "is_me": (r[0] == me_id)} for i, r in enumerate(rows, 1)]
    return {"competition": competition or None, "season": core.SEASON, "rows": out}


# La SPA (catch-all) APRÈS les routes /api pour qu'elles aient la priorité.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="spa")
