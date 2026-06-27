#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Foot Live — petite fenêtre "always-on-top" qui suit les scores de
http://foothunter.wiriath.com:6767/resultats/saison3/

- Rafraîchissement automatique (plus besoin de F5)
- Affiche tous les matchs (plus besoin de cliquer pour déplier)
- Détecte les matchs EN DIRECT via l'indicateur visuel du site (point rouge +
  score en rouge) — fiable même pour un 0-0 ou un match sans but depuis longtemps
- Un score qui change entre deux rafraîchissements => clignote + petit "bip" optionnel
- Choix de la compétition, ou vue "Toutes (live)" pour voir l'action partout
- Zéro dépendance : uniquement la lib standard Python (urllib, json, re, tkinter)

Lancement :  python3 foot_scores.py
Self-test (sans fenêtre) :  python3 foot_scores.py --selftest
"""

import os
import io
import sys
import re
import csv
import json
import time
import threading
import tempfile
import subprocess
import urllib.request
import urllib.parse
from datetime import date

from fh_mercato import *  # noqa: F401,F403  (modèle mercato/simulation)
from fh_mercato import _num

# ----------------------------------------------------------------------------
# Configuration / constantes
# ----------------------------------------------------------------------------
BASE_URL = "http://foothunter.wiriath.com:6767"
API_BASE = "/api"                     # routes JSON exposées par l'API Foothunter
OFFLINE_FALLBACK_SEASON = 3           # saison par défaut si l'API est injoignable
# Saison suivie. Priorité : FOOT_LIVE_SEASON (override explicite) ; sinon auto-détectée
# depuis l'API au démarrage via refresh_current_season() — prête pour la saison 4 sans
# toucher au code ; à défaut OFFLINE_FALLBACK_SEASON.
def _env_season():
    env = os.environ.get("FOOT_LIVE_SEASON")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return OFFLINE_FALLBACK_SEASON

SEASON = _env_season()
SAISON_PATH = os.environ.get("FOOT_LIVE_SAISON_PATH") or f"/resultats/saison{SEASON}"
ALL_KEY = "★ Toutes (live)"          # entrée spéciale du sélecteur de compétition
LIVE_GRACE = 200                      # secondes pendant lesquelles un match reste "LIVE" après un changement
HTTP_TIMEOUT = 25
USER_AGENT = "FootScores/1.0 (desktop widget)"

SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
GITHUB_REPO = os.environ.get("FOOT_LIVE_GITHUB_REPO", "ikitof/foothunter-watcher")
UPDATE_RELEASE_TAG = os.environ.get("FOOT_LIVE_UPDATE_TAG", "main-latest")
UPDATE_ASSET_NAME = os.environ.get("FOOT_LIVE_UPDATE_ASSET", "FootLive.exe")
UPDATE_TIMEOUT = 20
PLAYER_DATA_NAME = "data_joueurs.csv"
WHATS_NEW_NAME = "WHATS_NEW.md"

try:
    from build_info import APP_COMMIT, APP_BRANCH, APP_BUILD_TIME
except Exception:
    APP_COMMIT = ""
    APP_BRANCH = ""
    APP_BUILD_TIME = ""


def resource_path(name):
    """Chemin d'une ressource, compatible source Python et exécutable PyInstaller."""
    base = getattr(sys, "_MEIPASS", SOURCE_DIR)
    return os.path.join(base, name)


def _config_dir():
    if not getattr(sys, "frozen", False):
        return SOURCE_DIR
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
        return os.path.join(base, "Foot Live")
    return os.path.join(os.path.expanduser("~"), ".config", "foot-live")


CONFIG_PATH = os.path.join(_config_dir(), "foot_scores_config.json")


def write_config_file(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f)


def _env_truthy(name):
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def is_windows_frozen():
    return os.name == "nt" and bool(getattr(sys, "frozen", False))


def auto_update_enabled():
    return is_windows_frozen() and not _env_truthy("FOOT_LIVE_DISABLE_AUTO_UPDATE")


def current_build_commit():
    if APP_COMMIT:
        return APP_COMMIT.strip()
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=SOURCE_DIR,
            stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        return ""


def whats_new_build_id():
    """Identifiant stable du build utilisé pour n'afficher la note qu'une fois."""
    return current_build_commit() or APP_BUILD_TIME.strip()


def should_show_whats_new(cfg, build_id=None, enabled=None):
    """True si la note de version du build courant n'a pas encore été vue."""
    if enabled is None:
        enabled = is_windows_frozen() or _env_truthy("FOOT_LIVE_SHOW_WHATS_NEW")
    build_id = build_id if build_id is not None else whats_new_build_id()
    return bool(enabled and build_id and cfg.get("whats_new_seen_build") != build_id)


def load_whats_new():
    try:
        with open(resource_path(WHATS_NEW_NAME), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _http_get_url(url, timeout=HTTP_TIMEOUT, binary=False):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json,*/*",
        "Cache-Control": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "replace")


def latest_published_build():
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{UPDATE_RELEASE_TAG}"
    data = json.loads(_http_get_url(url, timeout=UPDATE_TIMEOUT))
    commit = (data.get("target_commitish") or "").strip()
    asset_url = ""
    asset_size = 0
    for asset in data.get("assets") or []:
        if asset.get("name") == UPDATE_ASSET_NAME:
            asset_url = asset.get("browser_download_url") or ""
            asset_size = int(asset.get("size") or 0)
            break
    if not commit or not asset_url:
        raise ValueError("build Windows GitHub introuvable")
    return commit, asset_url, asset_size


def _same_commit(a, b):
    return bool(a and b and (a == b or a.startswith(b) or b.startswith(a)))


def download_update_exe(commit, url, expected_size=0):
    """Télécharge le build Windows publié par la release roulante `main-latest`.

    Rejette un téléchargement tronqué : la taille reçue doit correspondre
    exactement à celle annoncée par GitHub (`asset["size"]`). Sinon l'exe relancé
    échouerait à charger sa DLL Python (bundle incomplet). Le fichier partiel est
    supprimé en cas d'échec pour ne jamais installer un exe corrompu.
    """
    suffix = commit[:12] if commit else str(int(time.time()))
    target = os.path.join(tempfile.gettempdir(), f"FootLive-{suffix}.exe")
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Cache-Control": "no-cache",
    })
    try:
        with urllib.request.urlopen(req, timeout=UPDATE_TIMEOUT) as r, open(target, "wb") as f:
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
        size = os.path.getsize(target)
        if expected_size and size != expected_size:
            raise ValueError(f"téléchargement incomplet ({size}/{expected_size} octets)")
        if size < 100 * 1024:
            raise ValueError("exécutable téléchargé trop petit")
        with open(target, "rb") as f:
            if f.read(2) != b"MZ":
                raise ValueError("fichier téléchargé invalide")
    except Exception:
        try:
            os.remove(target)
        except OSError:
            pass
        raise
    return target


def launch_self_update(new_exe_path):
    """Remplace l'exe courant après fermeture, puis relance l'application. Renvoie True
    si le script de mise à jour a bien démarré (sinon l'appelant NE doit pas fermer l'app).

    Robustesse (le .cmd tourne après la mort du process courant) :
    - Les chemins passent par des VARIABLES D'ENVIRONNEMENT (FL_SRC/FL_DST/FL_PID), jamais
      écrits dans le .cmd : un nom d'utilisateur accentué (C:\\Users\\Hélène\\…) ne peut plus
      corrompre le batch (cmd.exe lit un .cmd en codepage OEM, pas UTF-8).
    - Le déplacement est réessayé (l'exe peut rester verrouillé une ou deux secondes après
      la fermeture : scan antivirus à la fermeture), puis bascule sur `copy`.
    - Si tout échoue, on relance QUAND MÊME le nouvel exe depuis %TEMP% : l'utilisateur
      n'est jamais laissé sans application (plus de « crash » perçu).
    - Journalisé dans %TEMP%\\FootLive-update.log pour diagnostic à distance.
    """
    if not is_windows_frozen():
        return False
    current_exe = os.path.abspath(sys.executable)
    pid = os.getpid()
    script = os.path.join(tempfile.gettempdir(), f"FootLive-update-{pid}.cmd")
    # Batch 100% ASCII (les chemins viennent de l'environnement) -> aucun souci d'encodage.
    batch = (
        "@echo off\r\n"
        "setlocal enabledelayedexpansion\r\n"
        'set "LOG=%TEMP%\\FootLive-update.log"\r\n'
        'echo [%DATE% %TIME%] maj pid=%FL_PID% src=%FL_SRC% dst=%FL_DST%> "%LOG%"\r\n'
        ":wait\r\n"
        'tasklist /FI "PID eq %FL_PID%" | find "%FL_PID%" >nul\r\n'
        "if not errorlevel 1 ( timeout /t 1 /nobreak >nul & goto wait )\r\n"
        "set /a tries=0\r\n"
        ":try\r\n"
        'move /Y "%FL_SRC%" "%FL_DST%" >>"%LOG%" 2>&1\r\n'
        'if not errorlevel 1 ( echo move ok>>"%LOG%" & start "" "%FL_DST%" & goto done )\r\n'
        "set /a tries+=1\r\n"
        "if !tries! lss 30 ( timeout /t 1 /nobreak >nul & goto try )\r\n"
        'echo move KO apres !tries! essais, tentative copy>>"%LOG%"\r\n'
        'copy /Y "%FL_SRC%" "%FL_DST%" >>"%LOG%" 2>&1\r\n'
        'if not errorlevel 1 ( echo copy ok>>"%LOG%" & start "" "%FL_DST%" & goto done )\r\n'
        'echo move+copy KO, lancement du nouvel exe depuis temp>>"%LOG%"\r\n'
        'start "" "%FL_SRC%"\r\n'
        ":done\r\n"
        'del "%~f0" >nul 2>nul\r\n'
    )
    try:
        with open(script, "w", encoding="ascii") as f:
            f.write(batch)
        env = dict(os.environ, FL_SRC=new_exe_path, FL_DST=current_exe, FL_PID=str(pid))
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(["cmd.exe", "/c", script], close_fds=True,
                         creationflags=creationflags, env=env)
        return True
    except Exception:
        return False

# Postes affichés sur les pages d'équipe (sert à apparier joueur ↔ poste).
PLAYER_POSTES = ("GAR", "DC", "LAT", "MDEF", "MOFF", "AIL", "AC")

DEFAULT_COMPETITIONS = [
    "Premier League", "Liga", "Bundesliga", "Serie A", "Ligue 1",
    "Champions League", "Europa League", "Liga Nos", "Eredivisie", "Süper Lig",
    "Jupiler Pro League", "Copa del Rey", "Coppa Italia", "Coupe de France",
    "Croky Cup", "DFB-Pokal", "FA Cup", "KNVB Beker", "Taça de Portugal",
    "Türk Kupasi", "Championship", "Liga 2", "Bundesliga 2", "Serie B", "Ligue 2",
]


# ----------------------------------------------------------------------------
# Réseau + parsing du DOM NiceGUI embarqué dans la page
# ----------------------------------------------------------------------------
def http_get(path):
    """GET une page (chemin sans slash final pour éviter la redirection 307)."""
    url = BASE_URL + path
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


# ----------------------------------------------------------------------------
# Client API JSON Foothunter (/api/...) — SOURCE UNIQUE des données : résultats, live,
# calendrier, historique de célébrité, effectifs, liste des compétitions et saison
# courante. Plus aucun scraping HTML (le HTML n'est plus parsé du tout).
# ----------------------------------------------------------------------------
_api_cache = {}          # path -> (timestamp, valeur) ; TTL court pour mutualiser un cycle
_api_locks = {}          # path -> Lock (single-flight : un seul téléchargement par chemin)
_api_locks_guard = threading.Lock()


def _api_get_json(path, ttl=0):
    """GET JSON depuis l'API. ``ttl``>0 met le résultat en cache : la vue « Toutes »
    interroge ~25 compétitions d'affilée — on ne veut ni re-télécharger la saison à
    chaque fois ni la marteler en parallèle (single-flight via un verrou par chemin).
    Lève en cas d'échec réseau/HTTP/JSON (une saison inexistante renvoie HTTP 500)."""
    if not ttl:
        return json.loads(http_get(path))
    hit = _api_cache.get(path)
    if hit and (time.time() - hit[0]) < ttl:
        return hit[1]
    with _api_locks_guard:
        lock = _api_locks.setdefault(path, threading.Lock())
    with lock:
        hit = _api_cache.get(path)          # re-vérifie après attente du verrou
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]
        data = json.loads(http_get(path))
        _api_cache[path] = (time.time(), data)
        return data


def api_season_matches(season_number):
    """Matchs JOUÉS d'une saison via /api/matchs_par_saison. [] si absente/injoignable."""
    try:
        d = _api_get_json(f"{API_BASE}/matchs_par_saison?season_number={int(season_number)}", ttl=5)
        return d.get("resultats") or []
    except Exception:
        return []


def api_all_matchs():
    """Saisons TERMINÉES via /api/all_matchs -> {'saison0': [...], ...}. {} si injoignable.
    Le nombre de clés == numéro de la saison courante (les saisons finies sont 0..N-1)."""
    try:
        d = _api_get_json(f"{API_BASE}/all_matchs", ttl=600)
        return d.get("resultats") or {}
    except Exception:
        return {}


def api_live_matchs():
    """Matchs EN DIRECT via /api/live_matchs -> [match, ...]. [] si aucun/injoignable."""
    try:
        d = _api_get_json(f"{API_BASE}/live_matchs", ttl=5)
        return d.get("resultats") or []
    except Exception:
        return []


def api_all_joueurs(season_number=None):
    """Tous les joueurs d'une saison via /api/infos_all_joueurs -> [{id, nom, poste,
    nom_equipe, age, celebrite, salaire}]. [] si injoignable. Source de données joueurs
    (remplace l'export CSV websocket + le scraping des fiches)."""
    if season_number is None:
        season_number = SEASON
    try:
        d = _api_get_json(f"{API_BASE}/infos_all_joueurs?season_number={int(season_number)}", ttl=600)
        return d.get("resultats") or []
    except Exception:
        return []


def api_joueur_saison(nom_joueur, season_number=None):
    """Infos d'un joueur pour une saison via /api/infos_joueur_saison -> dict ({} si KO)."""
    if season_number is None:
        season_number = SEASON
    try:
        q = urllib.parse.quote(str(nom_joueur))
        d = _api_get_json(f"{API_BASE}/infos_joueur_saison?nom_joueur={q}&season_number={int(season_number)}")
        return d.get("resultats") or {}
    except Exception:
        return {}


def api_calendrier(season_number=None):
    """Calendrier (matchs PROGRAMMÉS) via /api/calendrier_par_compet ->
    {compétition: {phase: [{nom_equipe_dom, nom_equipe_ext}, ...]}}. {} si injoignable.
    Ni date ni score (non exposés par l'API) : sert aux affiches à venir pas encore jouées."""
    if season_number is None:
        season_number = SEASON
    try:
        d = _api_get_json(f"{API_BASE}/calendrier_par_compet?season_number={int(season_number)}", ttl=600)
        return d.get("resultats") or {}
    except Exception:
        return {}




def role_scout_rows(competition, poste, pool):
    """Joueurs d'un poste dans une compétition, avec la stat pertinente de leur équipe
    pour ce rôle (ex. GAR -> Arrêts %) et l'« adversité » : célébrité moyenne des postes
    adverses pertinents affrontés (ex. finisseurs adverses pour un GAR). L'adversité
    capture le niveau de la ligue ET des adversaires, à partir des données réelles.
    Deux variantes factuelles : `opp` sur tous les matchs, `opp_dec` sur les seuls matchs
    « décisifs » (encaissés pour un rôle défensif, marqués sinon).
    Renvoie [{nom, team, celebrite, salaire, age, stat, opp, opp_dec}]."""
    poste = (poste or "").upper()
    if poste not in ROLE_RELEVANCE:
        return []
    stat_key, _label, counters = ROLE_RELEVANCE[poste]
    defensive = poste in DEFENSIVE_ROLES
    groups, _ = fetch_competition(competition)
    tds = team_domain_stats(groups)
    # par équipe : [(adversaire, buts_marqués, buts_encaissés)] sur les matchs joués
    matches = {}
    for g in groups:
        for m in g["matches"]:
            if m.get("status") != "result" or m.get("site_live"):
                continue
            sc = _pair(m.get("mid"))
            a, b = m.get("a"), m.get("b")
            if sc and a and b:
                matches.setdefault(a, []).append((b, sc[0], sc[1]))
                matches.setdefault(b, []).append((a, sc[1], sc[0]))
    roster = {}
    for p in pool:
        roster.setdefault(p.get("nom_equipe"), {})[(p.get("poste") or "").upper()] = _num(p.get("celebrite"))
    rows = []
    for p in pool:
        if (p.get("poste") or "").upper() != poste:
            continue
        team = p.get("nom_equipe")
        if team not in tds:
            continue
        allq, decq = [], []
        for opp, gf, ga in matches.get(team, []):
            r = roster.get(opp, {})
            cs = [r[c] for c in counters if r.get(c) is not None]
            if not cs:
                continue
            q = sum(cs) / len(cs)
            allq.append(q)
            if (ga > 0) if defensive else (gf > 0):
                decq.append(q)
        rows.append({
            "nom": p.get("nom"), "team": team, "celebrite": _num(p.get("celebrite")),
            "salaire": _num(p.get("salaire")), "age": _num(p.get("age")),
            "stat": tds.get(team, {}).get(stat_key), "competition": competition,
            "opp": round(sum(allq) / len(allq), 1) if allq else None,
            "opp_dec": round(sum(decq) / len(decq), 1) if decq else None,
        })
    return rows


def role_scout_multi(competitions, poste, pool):
    """role_scout_rows agrégé sur plusieurs ligues : union des joueurs (une équipe
    n'appartenant qu'à un championnat, aucun joueur n'est dédoublé). Chaque ligne porte
    sa 'competition'. Permet de voir tous les joueurs d'un rôle au-delà d'une seule ligue."""
    seen = {}
    for comp in competitions:
        for r in role_scout_rows(comp, poste, pool):
            seen.setdefault((r["nom"], r["team"]), r)
    return list(seen.values())


# ----------------------------------------------------------------------------
# Palmarès : records marquants sur les matchs joués (saisons terminées + courante).
# 100% factuel — uniquement des matchs réellement joués et leurs stats.
# Les saisons 0 et 1 se jouaient en ligues AMATEUR -> exclues du palmarès (records
# faussés). On ne compte donc que les saisons >= PALMARES_MIN_SEASON.
# ----------------------------------------------------------------------------
PALMARES_MIN_SEASON = 2
# (clé, titre affiché, sous-titre). L'ordre est l'ordre d'affichage. Records de MATCH
# d'abord, puis records d'ÉQUIPE sur une saison (championnats uniquement).
PALMARES_CATEGORIES = [
    ("buts",      "🥅 Festival de buts",     "Le plus de buts dans un match"),
    ("stomp",     "💥 Démonstration",        "La plus grosse différence de buts"),
    ("away",      "✈️ Exploit à l'extérieur", "La plus large victoire à l'extérieur"),
    ("underdog",  "🐜 Exploit du petit",     "Le petit budget gagne malgré le plus gros écart"),
    ("holdup",    "🦹 Hold-up",              "Victoire avec le moins de possession"),
    ("realisme",  "🎯 Réalisme",             "Victoire en créant le moins d'occasions"),
    ("sterile",   "😤 Domination stérile",   "Le plus de possession… sans gagner"),
    ("malchance", "💔 Soir sans réussite",   "Le plus d'occasions… et la défaite"),
    ("nul",       "🎭 Nul spectaculaire",    "Le match nul le plus prolifique"),
    ("folie",     "🎰 Match de folie",       "Le plus d'occasions dans un match"),
    ("attaque",      "⚔️ Meilleure attaque", "Le plus de buts par match sur une saison"),
    ("pire_attaque", "🥶 Pire attaque",      "Le moins de buts par match sur une saison"),
    ("defense",      "🛡️ Meilleure défense", "Le moins de buts encaissés par match sur une saison"),
    ("pire_defense", "🪣 Pire défense",      "Le plus de buts encaissés par match sur une saison"),
    ("malin",        "💡 Le club malin",     "Le plus de points par M€ de masse salariale"),
    ("flop",         "💸 Plus gros flop",    "La masse salariale la plus chère payée au point"),
]


def _palmares_norm(matches):
    """[(saison, match_API)] -> [dict normalisé] en ne gardant que les matchs joués."""
    out = []
    for sn, m in matches:
        sd, se = _num(m.get("Score dom")), _num(m.get("Score ext"))
        if sd is None or se is None:
            continue
        out.append({
            "sn": sn, "dom": m.get("Equipe dom") or "?", "ext": m.get("Equipe ext") or "?",
            "sd": int(sd), "se": int(se),
            "od": _num(m.get("Occas dom")) or 0, "oe": _num(m.get("Occas ext")) or 0,
            "pd": _num(m.get("Posses dom")) or 0, "pe": _num(m.get("Posses ext")) or 0,
            "comp": m.get("competition") or "?", "phase": m.get("Phase") or "",
        })
    return out


def compute_palmares(matches, budgets, top=3, min_team_matches=6):
    """Records par catégorie sur une liste de matchs joués (logique pure, sans réseau).
    `matches` : [(saison:int, match_API)] ; `budgets` : {saison: {équipe: masse_salariale}}
    (underdog + club malin). Records de match + records d'équipe sur une saison (limités aux
    championnats — round-robin comparable — avec au moins `min_team_matches` matchs). Renvoie
    {clé: [{head, desc, ctx}, ...]} (≤ `top` chacun, triés du meilleur record au moins bon)."""
    rows = _palmares_norm(matches)

    def line(r):
        return f"{r['dom']} {r['sd']}-{r['se']} {r['ext']}"

    def ctx(r):
        c = f"S{r['sn']} · {r['comp']}"
        return c + (f" · {r['phase']}" if r['phase'] else "")

    def winner(r):  # (gagnant, score_gagnant, perdant, score_perdant) — match décidé
        return (r['dom'], r['sd'], r['ext'], r['se']) if r['sd'] > r['se'] \
            else (r['ext'], r['se'], r['dom'], r['sd'])

    cats = {k: [] for k, _t, _s in PALMARES_CATEGORIES}
    for r in rows:
        sd, se = r['sd'], r['se']
        decided = sd != se
        cats['buts'].append((sd + se, f"{sd + se} buts", line(r), ctx(r)))
        if decided:
            wn, ws, ln, ls = winner(r)
            d = ws - ls
            cats['stomp'].append((d, f"+{d}", line(r), ctx(r)))
            # exploit à l'extérieur : l'équipe visiteuse l'emporte
            if se > sd:
                cats['away'].append((d, f"+{d}", f"{r['ext']} gagne {se}-{sd} chez {r['dom']}", ctx(r)))
            # underdog : le moins riche gagne, classé par l'écart de budget
            b = budgets.get(r['sn']) or {}
            bw, bl = b.get(wn), b.get(ln)
            if bw is not None and bl is not None and bw < bl:
                cats['underdog'].append((bl - bw, f"+{bl - bw:.0f} M€",
                    f"{wn} ({bw:.0f}M€) bat {ln} ({bl:.0f}M€) {ws}-{ls}", ctx(r)))
            # hold-up : gagne avec la possession la plus faible (valeur = 100 - poss)
            wp = r['pd'] if sd > se else r['pe']
            if wp > 0:
                cats['holdup'].append((100 - wp, f"{wp:.0f}% balle",
                    f"{wn} s'impose {ws}-{ls} face à {ln}", ctx(r)))
            # réalisme : gagne en créant le moins d'occasions (valeur = -occ du gagnant)
            wocc = r['od'] if sd > se else r['oe']
            locc = r['oe'] if sd > se else r['od']
            if wocc >= 1:
                cats['realisme'].append((-wocc, f"{wocc:.0f} occ",
                    f"{wn} gagne {ws}-{ls} avec {wocc:.0f} occasions", ctx(r)))
            # malchance : le perdant a créé plus d'occasions que le gagnant
            if locc > wocc:
                cats['malchance'].append((locc, f"{locc:.0f} occ",
                    f"{ln} perd {ls}-{ws} malgré {locc:.0f} occasions", ctx(r)))
        else:
            cats['nul'].append((sd + se, f"{sd}-{se}", f"{r['dom']} / {r['ext']}", ctx(r)))
        # domination stérile : plus de possession mais ne gagne pas
        if r['pd'] != r['pe']:
            dteam, dpos = (r['dom'], r['pd']) if r['pd'] > r['pe'] else (r['ext'], r['pe'])
            dwon = (dteam == r['dom'] and sd > se) or (dteam == r['ext'] and se > sd)
            if not dwon and dpos > 0:
                cats['sterile'].append((dpos, f"{dpos:.0f}% balle",
                    f"{dteam} domine mais ne gagne pas — {line(r)}", ctx(r)))
        tot_occ = r['od'] + r['oe']
        if tot_occ > 0:
            cats['folie'].append((tot_occ, f"{tot_occ:.0f} occ", line(r), ctx(r)))

    # records d'équipe sur une saison : agrégat (matchs, BP, BC, pts) par (saison, ligue,
    # équipe), limité aux championnats (round-robin -> comparable entre équipes).
    agg = {}
    for r in rows:
        if r['comp'] not in SCOUT_LEAGUES:
            continue
        for team, gf, ga in ((r['dom'], r['sd'], r['se']), (r['ext'], r['se'], r['sd'])):
            a = agg.setdefault((r['sn'], r['comp'], team), [0, 0, 0, 0])
            a[0] += 1
            a[1] += gf
            a[2] += ga
            a[3] += 3 if gf > ga else (1 if gf == ga else 0)
    for (sn, comp, team), (n, gf, ga, pts) in agg.items():
        if n < min_team_matches:
            continue
        c = f"S{sn} · {comp}"
        gpm, gapm = gf / n, ga / n
        cats['attaque'].append((gpm, f"{gpm:.1f} b/m", f"{team} — {gf} buts en {n} matchs", c))
        cats['pire_attaque'].append((-gpm, f"{gpm:.1f} b/m", f"{team} — {gf} buts en {n} matchs", c))
        cats['defense'].append((-gapm, f"{gapm:.1f} enc/m", f"{team} — {ga} encaissés en {n} matchs", c))
        cats['pire_defense'].append((gapm, f"{gapm:.1f} enc/m", f"{team} — {ga} encaissés en {n} matchs", c))
        bud = (budgets.get(sn) or {}).get(team)
        if bud and bud > 0:
            cats['malin'].append((pts / bud, f"{pts / bud:.2f} pt/M€", f"{team} — {pts} pts pour {bud:.0f}M€", c))
            cats['flop'].append((bud / max(pts, 1), f"{bud / max(pts, 1):.0f} M€/pt",
                f"{team} — {bud:.0f}M€ pour {pts} pts", c))

    out = {}
    for k in cats:
        best = sorted(cats[k], key=lambda t: t[0], reverse=True)[:top]
        out[k] = [{"head": h, "desc": d, "ctx": c} for _v, h, d, c in best]
    return out


def palmares_data(top=3, min_season=PALMARES_MIN_SEASON):
    """Récupère les matchs joués (saisons terminées + courante) et les budgets par saison,
    puis calcule les records. Exclut les saisons < min_season (0-1 = ligues amateur, records
    faussés). RÉSEAU : à appeler hors du thread UI. Renvoie (records, nb_matchs_analysés)."""
    matches = []
    for skey, lst in (api_all_matchs() or {}).items():
        sn = int("".join(c for c in skey if c.isdigit()) or "-1")
        if sn < min_season:
            continue
        for m in (lst or []):
            matches.append((sn, m))
    if SEASON >= min_season:
        try:
            for m in api_season_matches(SEASON):
                matches.append((SEASON, m))
        except Exception:
            pass
    budgets = {}
    for sn in {s for s, _m in matches}:
        b = {}
        for p in api_all_joueurs(sn):
            t, s = p.get("nom_equipe"), _num(p.get("salaire"))
            if t and s is not None:
                b[t] = b.get(t, 0.0) + s
        budgets[sn] = b
    return compute_palmares(matches, budgets, top=top), len(matches)




def _api_pair_str(x, y, suffix=""):
    """'x - y' (suffixe optionnel, ex. '%'), ou None si une moitié manque — pour ne pas
    produire 'None - None' (cas d'un match live aux stats partielles)."""
    if x is None or y is None:
        return None
    return f"{x}{suffix} - {y}{suffix}"


def _api_match_to_dict(o):
    """Objet match JOUÉ de l'API (/matchs_par_saison, /all_matchs) -> dict interne
    (a/b/mid/status/poss/occ/site_live). mid/poss/occ : chaînes parsables par _pair, ou None."""
    return dict(
        a=o.get("Equipe dom"), b=o.get("Equipe ext"),
        mid=_api_pair_str(o.get("Score dom"), o.get("Score ext")),
        status="result",
        poss=_api_pair_str(o.get("Posses dom"), o.get("Posses ext"), "%"),
        occ=_api_pair_str(o.get("Occas dom"), o.get("Occas ext")),
        site_live=False,
    )


def _live_index(name):
    """Matchs EN DIRECT d'une compétition -> {(dom, ext): objet live}. Le flux
    /live_matchs a SES PROPRES champs : nom_equipe_dom/ext, score_dom/ext, occas_dom/ext
    où occas_* est un BOOLÉEN « but imminent » (pas un compte), sans Phase ni possession."""
    idx = {}
    for o in api_live_matchs():
        if o.get("competition") == name:
            idx[(o.get("nom_equipe_dom"), o.get("nom_equipe_ext"))] = o
    return idx


def _live_match_to_dict(lo):
    """Objet live -> dict match (match en direct absent des résultats joués)."""
    return dict(
        a=lo.get("nom_equipe_dom"), b=lo.get("nom_equipe_ext"),
        mid=_api_pair_str(lo.get("score_dom"), lo.get("score_ext")),
        status="result", poss=None, occ=None, site_live=True,
        imminent_dom=bool(lo.get("occas_dom")), imminent_ext=bool(lo.get("occas_ext")),
    )


def _apply_live(match, lo):
    """Superpose le live (score frais + but imminent) sur un match déjà présent."""
    mid = _api_pair_str(lo.get("score_dom"), lo.get("score_ext"))
    if mid is not None:
        match["mid"] = mid
    match["site_live"] = True
    match["imminent_dom"] = bool(lo.get("occas_dom"))
    match["imminent_ext"] = bool(lo.get("occas_ext"))


def season_domstats_from_api(matches):
    """Stats par domaine et par équipe d'une saison depuis les objets match de l'API
    (équivalent JSON de season_domstats_from_csv)."""
    grp = [{"label": "saison", "matches": [_api_match_to_dict(o) for o in matches]}]
    return team_domain_stats(grp)


def _standings_from_leaderboard(groups):
    """Classement aux mêmes clés que la table HTML (Rang/Équipe/Points/Diff/Buts),
    calculé depuis les résultats — l'API n'expose pas de classement."""
    rows = leaderboard(groups)
    if not rows:
        return None
    return [{"Rang": i, "Équipe": r["team"], "Points": r["points"],
             "Diff": f"{r['gd']:+d}", "Buts": r["gf"]}
            for i, r in enumerate(rows, 1)]


def fetch_competitions(season_number=None):
    """Liste des compétitions à proposer dans le sélecteur. Part de DEFAULT_COMPETITIONS
    (ordre d'affichage stable, jamais vide même en tout début de saison où l'API n'a pas
    encore de match joué) et y ajoute toute compétition vue dans l'API et absente."""
    if season_number is None:
        season_number = SEASON
    names = list(DEFAULT_COMPETITIONS)
    for o in api_season_matches(season_number):
        c = o.get("competition")
        if c and c not in names:
            names.append(c)
    return names


def refresh_current_season():
    """Recale SEASON/SAISON_PATH sur la saison courante = nombre de saisons terminées
    dans /api/all_matchs (best-effort, sans réseau au démarrage). FOOT_LIVE_SEASON et
    FOOT_LIVE_SAISON_PATH restent prioritaires. Renvoie la saison retenue."""
    global SEASON, SAISON_PATH
    if os.environ.get("FOOT_LIVE_SEASON"):
        return SEASON
    n = len(api_all_matchs())
    if n > 0:
        SEASON = n
        if not os.environ.get("FOOT_LIVE_SAISON_PATH"):
            SAISON_PATH = f"/resultats/saison{SEASON}"
    return SEASON


def fetch_competition(name):
    """Récupère une compétition. Résultats + live + calendrier 100% via l'API (plus de
    scraping HTML). Le calendrier (/calendrier_par_compet) ne donne ni date ni score :
    les matchs à venir s'affichent sans score. Renvoie (groups, standings) au format interne
    (groups = [{label, matches:[...]}]) pour rester compatible partout."""
    groups_by_phase = {}
    order = []

    def bucket(phase):
        if phase not in groups_by_phase:
            groups_by_phase[phase] = []
            order.append(phase)
        return groups_by_phase[phase]

    # Matchs en direct (flux dédié, champs distincts), indexés par (dom, ext) — le live
    # n'a pas de Phase, donc pas de clé Phase.
    live = _live_index(name)

    # 1) Résultats joués (API), avec le score frais + « but imminent » si en direct.
    for o in api_season_matches(SEASON):
        if o.get("competition") != name:
            continue
        m = _api_match_to_dict(o)
        lo = live.pop((o.get("Equipe dom"), o.get("Equipe ext")), None)
        if lo is not None:
            _apply_live(m, lo)
        bucket(o.get("Phase") or "").append(m)

    # 2) Matchs en direct pas encore dans les résultats (flux live, sans Phase).
    for lo in live.values():
        bucket("En direct").append(_live_match_to_dict(lo))

    # 3) Calendrier (matchs programmés) via l'API /calendrier_par_compet — sans date ni
    #    score. On n'ajoute que les rencontres absentes des résultats/live : ce sont les
    #    matchs pas encore joués. Dédoublonnage sur (a, b).
    seen = {(m.get("a"), m.get("b")) for ms in groups_by_phase.values() for m in ms}
    for phase, fixtures in (api_calendrier(SEASON).get(name) or {}).items():
        for fx in fixtures:
            a, b = fx.get("nom_equipe_dom"), fx.get("nom_equipe_ext")
            if not a or not b or (a, b) in seen:
                continue
            bucket(phase).append(dict(a=a, b=b, mid=None, status="scheduled",
                                      poss=None, occ=None, site_live=False))
            seen.add((a, b))

    groups = [{"label": ph, "matches": groups_by_phase[ph]}
              for ph in order if groups_by_phase[ph]]
    return groups, _standings_from_leaderboard(groups)


def fetch_players():
    """Liste globale des joueurs de la saison courante via l'API (remplace le scraping de
    /joueurs). Champs : id, nom, poste, nom_equipe, age, celebrite, salaire."""
    return api_all_joueurs(SEASON)


def fetch_team_squad(team, season_number=None):
    """Effectif d'une équipe via l'API (remplace le scraping /equipes + fiches joueurs).
    /infos_all_joueurs couvre les 140 équipes (7 joueurs chacune) -> filtrage direct.
    Renvoie [{nom_equipe, nom, poste, celebrite, salaire, age}]."""
    sn = season_number if season_number is not None else SEASON
    return [dict(nom_equipe=p.get("nom_equipe"), nom=p.get("nom"), poste=p.get("poste"),
                 celebrite=_num(p.get("celebrite")), salaire=_num(p.get("salaire")),
                 age=_num(p.get("age")))
            for p in api_all_joueurs(sn) if p.get("nom_equipe") == team]


def fetch_player_history(season_to=None):
    """Historique par joueur (célébrité + clubs par saison) reconstruit depuis l'API
    /infos_all_joueurs sur toutes les saisons (clé = id stable). Remplace l'export CSV
    websocket de /joueurs. Renvoie {players, histories, clubs, seasons} (clés de saison
    = int) :
      histories[nom] = {saison: célébrité} · clubs[nom] = {saison: club}
      players = [{nom, poste, nom_equipe, age, celebrite, salaire}] (dernière saison connue)."""
    if season_to is None:
        season_to = SEASON
    seasons = list(range(season_to + 1))
    by_id = {}
    for sn in seasons:
        for p in api_all_joueurs(sn):
            pid = p.get("id")
            if pid is None:
                continue
            e = by_id.setdefault(pid, {"nom": None, "poste": None,
                                       "cel": {}, "club": {}, "age": {}, "sal": {}})
            e["nom"] = p.get("nom") or e["nom"]
            e["poste"] = p.get("poste") or e["poste"]
            c, a, s = _num(p.get("celebrite")), _num(p.get("age")), _num(p.get("salaire"))
            if c is not None:
                e["cel"][sn] = c
            if p.get("nom_equipe"):
                e["club"][sn] = p["nom_equipe"]
            if a is not None:
                e["age"][sn] = a
            if s is not None:
                e["sal"][sn] = s
    players, histories, clubs = [], {}, {}
    for e in by_id.values():
        nom = e["nom"]
        if not nom or not e["cel"]:
            continue
        histories[nom] = e["cel"]
        if e["club"]:
            clubs[nom] = e["club"]
        players.append({
            "nom": nom, "poste": e["poste"],
            "nom_equipe": e["club"].get(max(e["club"])) if e["club"] else None,
            "age": e["age"].get(max(e["age"])) if e["age"] else None,
            "celebrite": e["cel"].get(max(e["cel"])),
            "salaire": e["sal"].get(max(e["sal"])) if e["sal"] else None,
        })
    return {"players": players, "histories": histories, "clubs": clubs, "seasons": seasons}


# ----------------------------------------------------------------------------
# Logique "live" (indépendante de l'UI, testable)
# ----------------------------------------------------------------------------
def current_group_index(groups):
    """Index du groupe en cours.

    Priorité au groupe qui contient un match EN DIRECT (marqueur du site) ; sinon
    le 1er groupe contenant un match programmé ; sinon le dernier.
    """
    for i, g in enumerate(groups):
        if any(m.get("site_live") for m in g["matches"]):
            return i
    for i, g in enumerate(groups):
        if any(m["status"] == "scheduled" for m in g["matches"]):
            return i
    return len(groups) - 1 if groups else None


def match_key(comp, group_label, m):
    return (comp, group_label, m.get("a"), m.get("b"))


def today_str():
    return date.today().strftime("%d/%m/%Y")


def _pair(s):
    """'26% - 74%' ou '1 - 1' -> (int, int) (côté A, côté B), sinon None."""
    m = re.match(r"^\s*(\d+)\s*%?\s*-\s*(\d+)\s*%?\s*$", s or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def team_history(groups, team):
    """Historique d'une équipe sur toutes les journées fournies (une compétition).

    Renvoie {'played': [...], 'upcoming': [...], 'stats': {...}}, tout du point de
    vue de l'équipe. Chaque match joué : {label, opp, home, gf, ga, score, res,
    poss, occ} où res = 'V'/'N'/'D' et poss/occ = la part de l'équipe (None si le
    site ne la fournit pas, typiquement pendant un match en cours).
    """
    played, upcoming = [], []
    for g in groups:
        for m in g["matches"]:
            a, b = m.get("a"), m.get("b")
            if team not in (a, b):
                continue
            home = (a == team)
            opp = b if home else a
            if m.get("status") == "result" and not m.get("site_live"):
                sc = _pair(m.get("mid"))
                if not sc:
                    continue
                gf, ga = sc if home else (sc[1], sc[0])
                pp = _pair(m.get("poss"))
                oo = _pair(m.get("occ"))
                played.append(dict(
                    label=g["label"], opp=opp, home=home, gf=gf, ga=ga,
                    score=m.get("mid"),
                    res="V" if gf > ga else ("N" if gf == ga else "D"),
                    poss=(pp[0] if home else pp[1]) if pp else None,
                    occ=(oo[0] if home else oo[1]) if oo else None,
                ))
            elif m.get("status") == "scheduled":
                upcoming.append(dict(label=g["label"], opp=opp, home=home,
                                     date=m.get("mid")))

    def avg(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    stats = dict(
        played=len(played),
        wins=sum(1 for p in played if p["res"] == "V"),
        draws=sum(1 for p in played if p["res"] == "N"),
        losses=sum(1 for p in played if p["res"] == "D"),
        gf=sum(p["gf"] for p in played),
        ga=sum(p["ga"] for p in played),
        avg_gf=avg([p["gf"] for p in played]),
        avg_ga=avg([p["ga"] for p in played]),
        avg_poss=avg([p["poss"] for p in played]),
        avg_occ=avg([p["occ"] for p in played]),
    )
    stats["points"] = stats["wins"] * 3 + stats["draws"]
    return dict(played=played, upcoming=upcoming, stats=stats)


def leaderboard(groups):
    """Classement agrégé par équipe sur les journées fournies.

    Renvoie une liste de dicts (un par équipe) triée par points, puis différence
    de buts, puis buts marqués (décroissant) :
    {team, played, wins, draws, losses, points, gf, ga, gd, avg_gf, avg_ga,
     avg_poss, avg_occ}. Les moyennes valent None si le site n'a fourni aucune
     donnée (possession/occasions) pour l'équipe.
    """
    teams = {}

    def slot(name):
        if name not in teams:
            teams[name] = dict(team=name, played=0, wins=0, draws=0, losses=0,
                               points=0, gf=0, ga=0, _poss=[], _occ=[])
        return teams[name]

    for g in groups:
        for m in g["matches"]:
            if m.get("status") != "result" or m.get("site_live"):
                continue              # un match en cours ne compte pas comme résultat final
            sc = _pair(m.get("mid"))
            a, b = m.get("a"), m.get("b")
            if not sc or not a or not b:
                continue
            pp, oo = _pair(m.get("poss")), _pair(m.get("occ"))
            for name, gf, ga, side in ((a, sc[0], sc[1], 0), (b, sc[1], sc[0], 1)):
                t = slot(name)
                t["played"] += 1
                t["gf"] += gf
                t["ga"] += ga
                if gf > ga:
                    t["wins"] += 1
                    t["points"] += 3
                elif gf == ga:
                    t["draws"] += 1
                    t["points"] += 1
                else:
                    t["losses"] += 1
                if pp:
                    t["_poss"].append(pp[side])
                if oo:
                    t["_occ"].append(oo[side])

    def avg(vals):
        return round(sum(vals) / len(vals), 1) if vals else None

    out = []
    for t in teams.values():
        poss, occ = t.pop("_poss"), t.pop("_occ")
        t["gd"] = t["gf"] - t["ga"]
        t["avg_gf"] = round(t["gf"] / t["played"], 1) if t["played"] else None
        t["avg_ga"] = round(t["ga"] / t["played"], 1) if t["played"] else None
        t["avg_poss"] = avg(poss)
        t["avg_occ"] = avg(occ)
        out.append(t)
    out.sort(key=lambda t: (t["points"], t["gd"], t["gf"]), reverse=True)
    return out


def _median(vals):
    """Médiane d'une liste (None ignorés), arrondie ; None si vide."""
    vals = sorted(v for v in vals if v is not None)
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    return round(vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2, 2)


def squad_stats(players):
    """Stats d'effectif par équipe à partir d'une liste de joueurs.

    Renvoie {nom_equipe: {count, avg_salary, med_salary, avg_celeb, med_celeb,
    avg_age, med_age}}. Les valeurs sont None si la donnée n'est pas disponible.
    """
    by_team = {}
    for p in players:
        team = p.get("nom_equipe")
        if team:
            by_team.setdefault(team, []).append(p)

    def avg(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    out = {}
    for team, ps in by_team.items():
        sal = [p.get("salaire") for p in ps]
        cel = [p.get("celebrite") for p in ps]
        age = [p.get("age") for p in ps]
        out[team] = dict(
            count=len(ps),
            avg_salary=avg(sal), med_salary=_median(sal),
            avg_celeb=avg(cel), med_celeb=_median(cel),
            avg_age=avg(age), med_age=_median(age),
        )
    return out


def position_percentile(players, poste, field, value):
    """Situe `value` parmi les joueurs de `poste` pour `field` (table globale).

    Renvoie {n, median, mean, pct} (pct = % de joueurs du poste sous `value`),
    ou None si pas d'échantillon / pas de valeur.
    """
    pool = [p.get(field) for p in players
            if p.get("poste") == poste and p.get(field) is not None]
    if not pool or value is None:
        return None
    n = len(pool)
    return {"n": n, "median": _median(pool), "mean": round(sum(pool) / n, 2),
            "pct": round(sum(1 for v in pool if v < value) / n * 100)}


def celebrity_evolution_rows(players, histories, season_from, season_to, poste=None):
    """Joueurs comparables entre deux saisons, avec leur delta de célébrité."""
    out = []
    for player in players:
        if poste and player.get("poste") != poste:
            continue
        hist = histories.get(player.get("nom")) or {}
        before, after = hist.get(season_from), hist.get(season_to)
        if before is None or after is None:
            continue
        out.append({
            "nom": player.get("nom"),
            "poste": player.get("poste"),
            "team": player.get("nom_equipe"),
            "before": before,
            "after": after,
            "delta": round(after - before, 1),
            "player": player,
        })
    return out


def role_evolution_summary(rows):
    """Résumé des évolutions par poste, trié par évolution moyenne."""
    grouped = {}
    for row in rows:
        if row.get("poste"):
            grouped.setdefault(row["poste"], []).append(row)
    out = []
    for poste, role_rows in grouped.items():
        out.append({
            "poste": poste,
            "count": len(role_rows),
            "avg": round(sum(r["delta"] for r in role_rows) / len(role_rows), 1),
            "rise": max(role_rows, key=lambda r: r["delta"]),
            "drop": min(role_rows, key=lambda r: r["delta"]),
        })
    return sorted(out, key=lambda r: r["avg"], reverse=True)


# Le modèle du jeu : un match est un entonnoir Possession -> Création -> Occasion
# -> But, et chaque poste alimente des domaines (manuel p.20). On dérive donc des
# stats d'équipe par domaine, et on les présente selon le poste du joueur.
POSTE_STATS = {
    "GAR":  [("% d'arrêts", "save", True), ("Arrêts / match", "arrets_pm", True),
             ("Clean sheets", "clean", True), ("Buts encaissés / match", "ga_pm", False)],
    "DC":   [("Occasions concédées / match", "occ_against_pm", False),
             ("Buts encaissés / match", "ga_pm", False), ("Clean sheets", "clean", True)],
    "LAT":  [("Occasions concédées / match", "occ_against_pm", False),
             ("Buts encaissés / match", "ga_pm", False), ("Occasions créées / match", "occ_for_pm", True)],
    "MDEF": [("Possession moy.", "poss", True), ("Occasions concédées / match", "occ_against_pm", False),
             ("Buts encaissés / match", "ga_pm", False)],
    "MOFF": [("Occasions créées / match", "occ_for_pm", True), ("Possession moy.", "poss", True),
             ("Taux de finition", "conv", True)],
    "AIL":  [("Occasions créées / match", "occ_for_pm", True), ("Taux de finition", "conv", True),
             ("Buts / match", "gf_pm", True)],
    "AC":   [("Buts / match", "gf_pm", True), ("Taux de finition", "conv", True),
             ("Occasions créées / match", "occ_for_pm", True)],
}
PERCENT_STATS = {"save", "conv", "poss"}   # affichés en %


def team_domain_stats(groups):
    """Stats d'équipe par domaine de jeu sur une compétition (toutes journées).

    Pour chaque équipe : matchs joués, buts ±/match, occasions ±/match, possession
    moyenne, clean sheets, taux de finition (buts/occasions) et taux d'arrêt
    (1 − buts encaissés/occasions concédées). Les métriques liées aux occasions
    n'utilisent que les matchs où le site les publie (les matchs en cours ne les
    ont pas encore). Renvoie {équipe: {...}}.
    """
    teams = {}

    def slot(t):
        if t not in teams:
            teams[t] = dict(played=0, gf=0, ga=0, clean=0, occ_n=0,
                            occ_for=0, occ_against=0, occ_gf=0, occ_ga=0,
                            poss_n=0, poss_sum=0)
        return teams[t]

    for g in groups:
        for m in g["matches"]:
            if m.get("status") != "result" or m.get("site_live"):
                continue              # un match en cours ne compte pas comme résultat final
            sc = _pair(m.get("mid"))
            a, b = m.get("a"), m.get("b")
            if not sc or not a or not b:
                continue
            occ, poss = _pair(m.get("occ")), _pair(m.get("poss"))
            for t, gf, ga, i in ((a, sc[0], sc[1], 0), (b, sc[1], sc[0], 1)):
                s = slot(t)
                s["played"] += 1
                s["gf"] += gf
                s["ga"] += ga
                if ga == 0:
                    s["clean"] += 1
                if occ:
                    s["occ_n"] += 1
                    s["occ_for"] += occ[i]
                    s["occ_against"] += occ[1 - i]
                    s["occ_gf"] += gf
                    s["occ_ga"] += ga
                if poss:
                    s["poss_n"] += 1
                    s["poss_sum"] += poss[i]

    def r1(x):
        return round(x, 1)

    out = {}
    for t, s in teams.items():
        n, on, pn = s["played"], s["occ_n"], s["poss_n"]
        out[t] = dict(
            team=t, played=n, clean=s["clean"], diff=s["gf"] - s["ga"],
            gf_pm=r1(s["gf"] / n) if n else None,
            ga_pm=r1(s["ga"] / n) if n else None,
            occ_for_pm=r1(s["occ_for"] / on) if on else None,
            occ_against_pm=r1(s["occ_against"] / on) if on else None,
            arrets_pm=r1((s["occ_against"] - s["occ_ga"]) / on) if on else None,
            poss=r1(s["poss_sum"] / pn) if pn else None,
            conv=r1(s["occ_gf"] / s["occ_for"] * 100) if s["occ_for"] else None,
            save=r1((1 - s["occ_ga"] / s["occ_against"]) * 100) if s["occ_against"] else None,
        )
    return out


def league_players(domstats, rosters, poste):
    """Joueurs d'un poste dans une ligue, avec les stats d'équipe par domaine.

    domstats = {équipe: stats} (team_domain_stats), rosters = {équipe: [joueurs]}.
    Renvoie [{nom, team, player, stats}] pour chaque joueur du poste dont l'équipe
    figure dans domstats. Non trié — l'UI trie selon la métrique choisie.
    """
    out = []
    for team, stats in domstats.items():
        for p in rosters.get(team) or []:
            if p.get("poste") == poste:
                out.append({"nom": p.get("nom"), "team": team, "player": p, "stats": stats})
    return out


def season_domstats_from_csv(text):
    """Stats par domaine et par équipe pour une saison, depuis un CSV de matchs.

    Colonnes attendues : Equipe dom, Equipe ext, Score dom/ext, Occas dom/ext,
    Posses dom/ext (toutes compétitions confondues). Renvoie {équipe: stats}.
    """
    matches = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            matches.append(dict(
                a=row["Equipe dom"], b=row["Equipe ext"],
                mid=f"{row['Score dom']} - {row['Score ext']}", status="result",
                occ=f"{row['Occas dom']} - {row['Occas ext']}",
                poss=f"{row['Posses dom']}% - {row['Posses ext']}%",
            ))
        except (KeyError, TypeError):
            continue
    return team_domain_stats([{"label": "saison", "matches": matches}])


class LiveTracker:
    """Mémorise le dernier état des matchs et la date du dernier changement de score."""

    def __init__(self):
        self.last_mid = {}        # key -> dernier 'mid' (score/date) vu
        self.changed_at = {}      # key -> timestamp du dernier changement

    def update(self, comp, groups, now=None):
        """Marque chaque match (m['changed'], m['live']) et renvoie la liste des keys qui ont changé."""
        if now is None:
            now = time.time()
        changed_keys = []
        cur_idx = current_group_index(groups)
        today = today_str()
        for gi, g in enumerate(groups):
            for m in g["matches"]:
                k = match_key(comp, g["label"], m)
                prev = self.last_mid.get(k)
                changed = prev is not None and prev != m["mid"]
                if changed:
                    self.changed_at[k] = now
                    changed_keys.append(k)
                self.last_mid[k] = m["mid"]
                # Live = signal fiable du site (point/score rouge). Repli : un score
                # qui a changé récemment (utile si le site tarde à marquer le match).
                recent = (now - self.changed_at[k]) < LIVE_GRACE if k in self.changed_at else False
                m["changed"] = changed
                m["live"] = bool(m.get("site_live")) or recent
                # "en cours / récent" : un résultat dans la journée/tour en cours
                m["current"] = (gi == cur_idx and m["status"] == "result")
                m["is_today"] = (m["status"] == "scheduled" and m["mid"] == today)
        return changed_keys


# ----------------------------------------------------------------------------
# Self-test (sans interface graphique)
# ----------------------------------------------------------------------------
def selftest_offline():
    """Tests déterministes (sans réseau) de la détection 'live'."""
    # 1) un match 0-0 marqué live par le site est 'live' SANS aucun changement de score,
    #    et un match terminé (non marqué) ne l'est pas.
    tr = LiveTracker()
    groups = [{"label": "Journée 1", "matches": [
        dict(a="A", b="B", mid="0 - 0", status="result", poss=None, occ=None, site_live=True),
        dict(a="C", b="D", mid="2 - 1", status="result", poss=None, occ=None, site_live=False),
    ]}]
    tr.update("X", groups)
    assert groups[0]["matches"][0]["live"] is True, "0-0 marqué live non détecté"
    assert groups[0]["matches"][1]["live"] is False, "match terminé marqué live à tort"

    # 3) current_group_index privilégie le groupe contenant un match live.
    g2 = [
        {"label": "J1", "matches": [dict(status="result", site_live=False)]},
        {"label": "J2", "matches": [dict(status="result", site_live=True)]},
        {"label": "J3", "matches": [dict(status="scheduled", site_live=False)]},
    ]
    assert current_group_index(g2) == 1, "le groupe live devrait être 'en cours'"
    print("  ✓ tests hors-ligne OK (marqueur visuel, 0-0 live, groupe en cours)")

    # 4) team_history : bilan et moyennes du point de vue de l'équipe (dom. + ext.).
    g3 = [
        {"label": "J1", "matches": [dict(a="Alpha", b="Beta", mid="2 - 1",
            status="result", poss="60% - 40%", occ="5 - 3", site_live=False)]},
        {"label": "J2", "matches": [dict(a="Gamma", b="Alpha", mid="0 - 0",
            status="result", poss="45% - 55%", occ="2 - 4", site_live=False)]},
        {"label": "J3", "matches": [dict(a="Alpha", b="Gamma", mid="30/05/2026",
            status="scheduled", poss=None, occ=None, site_live=False)]},
    ]
    h = team_history(g3, "Alpha")
    st = h["stats"]
    assert (st["played"], st["wins"], st["draws"], st["losses"]) == (2, 1, 1, 0)
    assert st["points"] == 4 and st["gf"] == 2 and st["ga"] == 1
    assert st["avg_poss"] == 57.5 and st["avg_occ"] == 4.5  # (60+55)/2 ; (5+4)/2
    assert len(h["upcoming"]) == 1 and h["upcoming"][0]["opp"] == "Gamma"
    print("  ✓ team_history OK (V/N/D, points, moyennes dom./ext., à venir)")

    # 5) leaderboard : agrégat trié par points/diff, moyennes par équipe.
    g4 = [
        {"label": "J1", "matches": [
            dict(a="Alpha", b="Beta", mid="2 - 1", status="result",
                 poss="55% - 45%", occ="4 - 2", site_live=False),
            dict(a="Gamma", b="Delta", mid="0 - 0", status="result",
                 poss=None, occ=None, site_live=False)]},
        {"label": "J2", "matches": [
            dict(a="Beta", b="Gamma", mid="3 - 0", status="result",
                 poss=None, occ=None, site_live=False),
            dict(a="Alpha", b="Delta", mid="1 - 1", status="result",
                 poss=None, occ=None, site_live=False)]},
    ]
    lb = leaderboard(g4)
    assert [r["team"] for r in lb] == ["Alpha", "Beta", "Delta", "Gamma"], lb
    top = lb[0]
    assert (top["played"], top["wins"], top["draws"], top["losses"]) == (2, 1, 1, 0)
    assert top["points"] == 4 and top["gf"] == 3 and top["ga"] == 2 and top["gd"] == 1
    assert top["avg_poss"] == 55.0   # une seule donnée de possession (55), l'autre None
    beta = next(r for r in lb if r["team"] == "Beta")
    assert beta["points"] == 3 and beta["gd"] == 2
    print("  ✓ leaderboard OK (tri points/diff, moyennes)")

    # 6) squad_stats : moyenne ET médiane du salaire / de la célébrité / de l'âge.
    players = [
        dict(nom_equipe="Alpha", salaire=10.0, celebrite=50.0, age=20),
        dict(nom_equipe="Alpha", salaire=20.0, celebrite=60.0, age=24),
        dict(nom_equipe="Alpha", salaire=30.0, celebrite=100.0, age=28),
        dict(nom_equipe="Beta", salaire=10.0, celebrite=40.0, age=30),
        dict(nom_equipe="Beta", salaire=20.0, celebrite=None, age=None),  # valeurs manquantes ignorées
    ]
    sq = squad_stats(players)
    assert sq["Alpha"]["count"] == 3
    assert sq["Alpha"]["avg_salary"] == 20.0 and sq["Alpha"]["med_salary"] == 20  # impair
    assert sq["Alpha"]["avg_celeb"] == 70.0 and sq["Alpha"]["med_celeb"] == 60
    assert sq["Alpha"]["avg_age"] == 24.0 and sq["Alpha"]["med_age"] == 24
    assert sq["Beta"]["med_salary"] == 15.0                       # pair -> (10+20)/2
    assert sq["Beta"]["avg_celeb"] == 40.0 and sq["Beta"]["med_celeb"] == 40
    assert sq["Beta"]["avg_age"] == 30.0                          # le None est ignoré
    assert _median([]) is None
    print("  ✓ squad_stats OK (moyenne + médiane salaire/célébrité/âge, pair/impair)")

    # 7) team_domain_stats : taux de finition / d'arrêt, clean sheets, occasions.
    gd = [
        {"label": "J1", "matches": [dict(a="A", b="B", mid="2 - 1", status="result",
            occ="4 - 2", poss="60% - 40%", site_live=False)]},
        {"label": "J2", "matches": [dict(a="B", b="A", mid="0 - 0", status="result",
            occ="1 - 3", poss="45% - 55%", site_live=False)]},
    ]
    ds = team_domain_stats(gd)
    A = ds["A"]
    assert A["played"] == 2 and A["clean"] == 1 and A["diff"] == 1
    assert A["gf_pm"] == 1.0 and A["ga_pm"] == 0.5
    assert A["occ_for_pm"] == 3.5 and A["occ_against_pm"] == 1.5 and A["arrets_pm"] == 1.0
    assert A["poss"] == 57.5
    assert A["conv"] == 28.6        # 2 buts / 7 occasions
    assert A["save"] == 66.7        # 1 - 1 encaissé / 3 occ concédées
    print("  ✓ team_domain_stats OK (finition, arrêts, clean sheets, occasions)")

    # 9) position_percentile : situe une valeur parmi les joueurs d'un poste.
    pool = [{"poste": "GAR", "salaire": s} for s in (10.0, 20.0, 30.0, 40.0)]
    pc = position_percentile(pool, "GAR", "salaire", 25.0)
    assert pc["n"] == 4 and pc["median"] == 25.0 and pc["pct"] == 50
    assert position_percentile(pool, "GAR", "salaire", None) is None
    assert position_percentile(pool, "AC", "salaire", 25.0) is None
    print("  ✓ position_percentile OK")

    # 10) league_players : joueurs d'un poste dans la ligue + stats d'équipe.
    domstats = {"A": {"save": 60.0}, "B": {"save": 40.0}}
    rosters = {"A": [{"poste": "GAR", "nom": "x"}],
               "B": [{"poste": "GAR", "nom": "y"}, {"poste": "AC", "nom": "z"}]}
    gk = league_players(domstats, rosters, "GAR")
    assert {(r["nom"], r["team"], r["stats"]["save"]) for r in gk} == {("x", "A", 60.0), ("y", "B", 40.0)}
    assert league_players(domstats, rosters, "AC") == [
        {"nom": "z", "team": "B", "player": {"poste": "AC", "nom": "z"}, "stats": {"save": 40.0}}]
    print("  ✓ league_players OK")

    # 11) season_domstats_from_csv : agrège un CSV de saison par équipe.
    csv_text = ("competition,Phase,Equipe dom,Equipe ext,Score dom,Score ext,"
                "Occas dom,Occas ext,Posses dom,Posses ext\n"
                "L,J1,A,B,2,1,4,2,60,40\n"
                "L,J2,B,A,0,0,1,3,45,55\n")
    sd = season_domstats_from_csv(csv_text)
    assert sd["A"]["save"] == 66.7 and sd["A"]["conv"] == 28.6 and sd["A"]["clean"] == 1
    print("  ✓ season_domstats_from_csv OK")

    # 13) évolutions de célébrité : filtre poste + résumé par rôle.
    evo_players = [
        {"nom": "A", "poste": "MOFF", "nom_equipe": "X"},
        {"nom": "B", "poste": "MOFF", "nom_equipe": "Y"},
        {"nom": "C", "poste": "GAR", "nom_equipe": "Z"},
    ]
    evo_hist = {"A": {2: 70.0, 3: 75.0}, "B": {2: 80.0, 3: 72.0},
                "C": {2: 50.0, 3: 52.0}}
    evos = celebrity_evolution_rows(evo_players, evo_hist, 2, 3)
    assert [r["delta"] for r in evos] == [5.0, -8.0, 2.0]
    assert [r["nom"] for r in celebrity_evolution_rows(
        evo_players, evo_hist, 2, 3, "MOFF"
    )] == ["A", "B"]
    roles = {r["poste"]: r for r in role_evolution_summary(evos)}
    assert roles["MOFF"]["avg"] == -1.5 and roles["MOFF"]["drop"]["nom"] == "B"
    assert roles["GAR"]["rise"]["nom"] == "C"
    print("  ✓ celebrity_evolution_rows / role_evolution_summary OK")

    # 14) modèle mercato / simulation (coût contrat, prolongation, force par domaine).
    assert contract_cost(2.0, 3) == 6.0 and contract_cost(2.0, 1) == 2.0
    assert extension_cost(4.0, 5.0) == 5.5      # célébrité montée -> +10%
    assert extension_cost(5.0, 4.0) == 5.0      # célébrité baissée -> ancien salaire
    _squad = [{"poste": "GAR", "celebrite": 90, "age": 30, "salaire": 20},
              {"poste": "AC", "celebrite": 80, "age": 25, "salaire": 15}]
    _st = team_domain_strength(_squad)
    assert _st["arrets"] == 90.0 and _st["finition"] == 48.0   # GAR seul -> arrêts ; AC -> finition
    _agg = squad_aggregate(_squad)
    assert _agg["count"] == 2 and _agg["total_salaire"] == 35.0 and _agg["avg_age"] == 27.5
    assert len(TEAM_POSTES) == 7 and "AIL" in TEAM_POSTES   # 1 joueur par poste
    _inv = team_domain_investment([("GAR", 20.0), ("AC", 30.0)])
    assert _inv["arrets"] == 16.0 and _inv["finition"] == 18.0   # 20×80% ; 30×60%
    print("  ✓ modèle mercato OK (coût, supplément, force, investissement/domaine, agrégats)")

    # 15) note de version : affichée une seule fois par build.
    assert should_show_whats_new({}, "abc", enabled=True)
    assert not should_show_whats_new({"whats_new_seen_build": "abc"}, "abc", enabled=True)
    assert should_show_whats_new({"whats_new_seen_build": "old"}, "abc", enabled=True)
    assert not should_show_whats_new({}, "abc", enabled=False)
    notes = load_whats_new()
    assert "Nouveautés" in notes
    assert not any(term in notes for term in ("CSV", "/joueurs", "GitHub", "implémentation"))
    print("  ✓ note de version affichée une seule fois par build")

    # 16) palmarès : records calculés sur des matchs synthétiques (sans réseau).
    _pm = [
        (1, {"Equipe dom": "Petit", "Equipe ext": "Riche", "Score dom": 3, "Score ext": 1,
             "Posses dom": 30, "Posses ext": 70, "Occas dom": 2, "Occas ext": 9,
             "competition": "Ligue 1", "Phase": "Journée 1"}),
        (1, {"Equipe dom": "A", "Equipe ext": "B", "Score dom": 4, "Score ext": 4,
             "Posses dom": 50, "Posses ext": 50, "Occas dom": 5, "Occas ext": 5,
             "competition": "Liga", "Phase": "Journée 2"}),
        (1, {"Equipe dom": "Gros", "Equipe ext": "Faible", "Score dom": 6, "Score ext": 0,
             "Posses dom": 80, "Posses ext": 20, "Occas dom": 10, "Occas ext": 1,
             "competition": "Serie A", "Phase": "Journée 3"}),
        (1, {"Equipe dom": "Hôte", "Equipe ext": "Visiteur", "Score dom": 0, "Score ext": 2,
             "Posses dom": 55, "Posses ext": 45, "Occas dom": 4, "Occas ext": 3,
             "competition": "Bundesliga", "Phase": "Journée 1"}),
    ]
    _pb = {1: {"Petit": 10.0, "Riche": 200.0, "A": 50.0, "B": 50.0, "Gros": 150.0,
               "Faible": 20.0, "Hôte": 80.0, "Visiteur": 120.0}}
    _rec = compute_palmares(_pm, _pb, top=3, min_team_matches=1)
    assert _rec["buts"][0]["head"] == "8 buts"                 # 4-4 (8) > 6-0 > 3-1
    assert _rec["stomp"][0]["head"] == "+6"                    # 6-0
    assert _rec["away"][0]["head"] == "+2" and "Visiteur" in _rec["away"][0]["desc"]
    assert _rec["underdog"][0]["head"] == "+190 M€" and "Petit" in _rec["underdog"][0]["desc"]
    assert _rec["holdup"][0]["head"] == "30% balle"            # Petit gagne avec 30%
    assert _rec["realisme"][0]["head"] == "2 occ"              # Petit gagne avec 2 occasions
    assert _rec["sterile"][0]["head"] == "70% balle"           # Riche 70% mais perd
    assert _rec["malchance"][0]["head"] == "9 occ"             # Riche perd avec 9 occ
    assert _rec["nul"][0]["head"] == "4-4"
    assert _rec["folie"][0]["head"] == "11 occ"
    assert _rec["attaque"][0]["head"] == "6.0 b/m" and "Gros" in _rec["attaque"][0]["desc"]
    assert _rec["defense"][0]["head"] == "0.0 enc/m"           # Gros / Visiteur : 0 encaissé
    assert _rec["malin"][0]["head"] == "0.30 pt/M€" and "Petit" in _rec["malin"][0]["desc"]
    assert _rec["pire_attaque"][0]["head"] == "0.0 b/m"        # Faible / Hôte : 0 but
    assert _rec["pire_defense"][0]["head"] == "6.0 enc/m" and "Faible" in _rec["pire_defense"][0]["desc"]
    assert _rec["flop"][0]["head"] == "200 M€/pt" and "Riche" in _rec["flop"][0]["desc"]
    print("  ✓ palmarès OK (10 records de match + attaque/défense/malin + anti-records par saison)")


def selftest():
    print("→ Tests hors-ligne…")
    selftest_offline()
    print("→ Détection de la saison + API…")
    refresh_current_season()
    print(f"  saison courante détectée = {SEASON}")
    assert api_season_matches(SEASON), "API: aucun match pour la saison courante"
    assert api_all_matchs(), "API: /all_matchs vide"
    comps = fetch_competitions(SEASON)
    print(f"  {len(comps)} compétitions : {', '.join(comps[:6])} …")
    assert comps, "aucune compétition trouvée"

    print("→ Historique joueurs reconstruit via l'API (fetch_player_history)…")
    exported = fetch_player_history()
    assert len(exported["players"]) > 500 and len(exported["seasons"]) >= 2
    assert exported["histories"] and exported["clubs"], "historique/clubs vides"
    print(f"  ✓ {len(exported['players'])} joueurs, saisons {exported['seasons']}")

    print("→ Joueurs via l'API /infos_all_joueurs…")
    joueurs = api_all_joueurs(SEASON)
    assert len(joueurs) > 500, "API: infos_all_joueurs vide"
    assert {"nom", "poste", "celebrite", "salaire", "age"} <= set(joueurs[0])
    print(f"  ✓ {len(joueurs)} joueurs via l'API")

    scout = role_scout_rows(comps[0] if comps else "Premier League", "GAR", joueurs)
    assert scout and any(r["stat"] is not None for r in scout), "scout GAR vide"
    ex = max(scout, key=lambda r: r.get("celebrite") or 0)
    print(f"  ✓ role_scout_rows : {len(scout)} gardiens, ex. {ex['nom']} "
          f"arrêts={ex['stat']} adversité={ex['opp']}")

    tr = LiveTracker()
    for comp in ["Premier League", "Champions League"]:
        print(f"\n→ {comp}")
        groups, standings = fetch_competition(comp)
        tr.update(comp, groups)
        cur = current_group_index(groups)
        print(f"  {len(groups)} groupes, classement={'oui' if standings else 'non'}, "
              f"journée en cours = {groups[cur]['label'] if cur is not None else '?'}")
        n_live = sum(1 for g in groups for m in g["matches"] if m.get("site_live"))
        for gi, g in enumerate(groups):
            gl = sum(1 for m in g["matches"] if m.get("site_live"))
            tag = "  <== EN COURS" if gi == cur else ""
            tag += f"  🔴 {gl} live" if gl else ""
            print(f"   {g['label']} ({len(g['matches'])}){tag}")
        print(f"  → {n_live} match(s) EN DIRECT selon le site (marqueur rouge)")
        # simulate a score change to prove live-detection works (sur un match DÉJÀ JOUÉ :
        # depuis la bascule calendrier API, les matchs programmés ont mid=None).
        played = [mm for gg in groups for mm in gg["matches"]
                  if mm["status"] == "result" and mm["mid"]]
        if played:
            m = played[0]
            old = m["mid"]
            m["mid"] = "9 - 9"
            m["status"] = "result"
            changed = tr.update(comp, groups)
            assert any(k[2] == m["a"] for k in changed), "le changement n'a pas été détecté"
            print(f"  ✓ détection live OK (simulé {m['a']} {old} -> 9 - 9 ; "
                  f"live={m['live']}, changed={m['changed']})")
    print("\nSELFTEST OK ✅")


# ----------------------------------------------------------------------------
# Interface graphique (Tkinter)
# ----------------------------------------------------------------------------


# ----------------------------------------------------------------------------
if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        from fh_gui import run_gui
        run_gui()
