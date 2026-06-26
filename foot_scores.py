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
import base64
import hashlib
import socket
import struct
import uuid
import zlib
import threading
import tempfile
import subprocess
import urllib.request
import urllib.parse
from datetime import date
from html import unescape
from concurrent.futures import ThreadPoolExecutor, as_completed

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

# Le site marque un match en cours avec un point rouge (bg-red-500) et un score
# rouge gras (text-red-600). C'est LE signal fiable d'un match "en direct"
# (les barres de possession utilisent bg-red-200 / bg-blue-500, à ne pas confondre).
LIVE_CLASS_MARKERS = ("text-red-600", "bg-red-500")

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

SCORE_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")
DATE_RE = re.compile(r"^\s*\d{1,2}/\d{1,2}/\d{2,4}\s*$")
POSS_RE = re.compile(r"^\s*\d+%\s*-\s*\d+%\s*$")
CELEB_RE = re.compile(r"[Cc]élébrité\s*:\s*([\d.,]+)")
SALARY_RE = re.compile(r"Salaire\s+annuel\s*:\s*([\d.,]+)")
AGE_RE = re.compile(r"Âge\s*:\s*(\d+)")
POSTE_RE = re.compile(r"Poste\s*:\s*([A-Za-z]+)")
CLUB_RE = re.compile(r"Saison\s+(\d+)\s*:\s*(.+)")   # historique des clubs sur la fiche
CELEB_CHART_RE = re.compile(r"data:image/png;base64,([A-Za-z0-9+/=]+)")

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
# Client API JSON Foothunter (/api/...) — source des résultats, du live, de
# l'historique, de la liste des compétitions et de la saison courante. Le HTML
# (parse_elements/parse_matches) ne sert plus qu'aux données absentes de l'API :
# calendrier des matchs à venir + dates, et l'export joueurs/effectifs.
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
# Palmarès : records marquants sur TOUS les matchs joués (saisons terminées +
# courante). 100% factuel — uniquement des matchs réellement joués et leurs stats.
# ----------------------------------------------------------------------------
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


def palmares_data(top=3):
    """Récupère tous les matchs joués (saisons terminées + courante) et les budgets par
    saison, puis calcule les records. RÉSEAU : à appeler hors du thread UI. Renvoie
    (records, nb_matchs_analysés)."""
    matches = []
    for skey, lst in (api_all_matchs() or {}).items():
        sn = int("".join(c for c in skey if c.isdigit()) or "-1")
        for m in (lst or []):
            matches.append((sn, m))
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
    """Objet match JOUÉ de l'API (/matchs_par_saison, /all_matchs) -> dict interne au
    format de parse_matches. mid/poss/occ sont des chaînes parsables par _pair, ou None."""
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


def _read_exact(reader, size):
    data = bytearray()
    while len(data) < size:
        chunk = reader.read(size - len(data))
        if not chunk:
            raise ConnectionError("websocket fermé")
        data.extend(chunk)
    return bytes(data)


def _websocket_send(sock, opcode, payload):
    """Envoie une frame WebSocket client (donc masquée), sans dépendance externe."""
    payload = payload.encode("utf-8") if isinstance(payload, str) else payload
    first = 0x80 | opcode
    size = len(payload)
    if size < 126:
        header = bytes((first, 0x80 | size))
    elif size < 65536:
        header = bytes((first, 0x80 | 126)) + struct.pack(">H", size)
    else:
        header = bytes((first, 0x80 | 127)) + struct.pack(">Q", size)
    mask = os.urandom(4)
    masked = bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
    sock.sendall(header + mask + masked)


def _websocket_receive(reader):
    """Lit un message WebSocket complet et renvoie (opcode, payload)."""
    first, second = _read_exact(reader, 2)
    opcode, final = first & 0x0F, bool(first & 0x80)
    masked, size = bool(second & 0x80), second & 0x7F
    if size == 126:
        size = struct.unpack(">H", _read_exact(reader, 2))[0]
    elif size == 127:
        size = struct.unpack(">Q", _read_exact(reader, 8))[0]
    mask = _read_exact(reader, 4) if masked else b""
    payload = _read_exact(reader, size)
    if masked:
        payload = bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
    if final or opcode in (0x8, 0x9, 0xA):
        return opcode, payload

    chunks = [payload]
    while True:
        next_opcode, chunk = _websocket_receive(reader)
        if next_opcode != 0:
            raise ValueError("fragment WebSocket inattendu")
        chunks.append(chunk)
        # _websocket_receive ne renvoie un fragment de continuation qu'à sa fin.
        return opcode, b"".join(chunks)


def download_players_csv():
    """Déclenche le bouton d'export de `/joueurs` via le protocole NiceGUI.

    NiceGUI n'expose pas une URL CSV : le bouton envoie le contenu comme pièce
    jointe binaire Socket.IO. Cette implémentation minimale reste 100 % stdlib.
    """
    html = http_get("/joueurs")
    client_match = re.search(r"query:\s*\{[^}]*'client_id':\s*'([^']+)'", html)
    elements = parse_elements(html)
    if not client_match or not elements:
        raise ValueError("identifiant NiceGUI absent")

    button_id = listener_id = None
    for eid, element in elements.items():
        props = element.get("props") or {}
        label = props.get("label") or ""
        if element.get("tag") != "q-btn" or "Télécharger stats des joueurs" not in label:
            continue
        event = next((e for e in element.get("events") or [] if e.get("type") == "click"), None)
        if event:
            button_id, listener_id = int(eid), event.get("listener_id")
            break
    if button_id is None or not listener_id:
        raise ValueError("bouton d'export joueurs absent")

    parsed = urllib.parse.urlparse(BASE_URL)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    query = urllib.parse.urlencode({
        "client_id": client_match.group(1),
        "next_message_id": 0,
        "implicit_handshake": "true",
        "document_id": str(uuid.uuid4()),
        "tab_id": str(uuid.uuid4()),
        "old_tab_id": "null",
        "EIO": 4,
        "transport": "websocket",
    })
    endpoint = (parsed.path.rstrip("/") + "/_nicegui_ws/socket.io/?" + query)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    host_header = host if port in (80, 443) else f"{host}:{port}"
    request = (
        f"GET {endpoint} HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        f"Origin: {parsed.scheme}://{host_header}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode("ascii")

    sock = socket.create_connection((host, port), timeout=HTTP_TIMEOUT)
    if parsed.scheme == "https":
        import ssl
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    reader = None
    try:
        sock.sendall(request)
        reader = sock.makefile("rb")
        status = reader.readline().decode("ascii", "replace").strip()
        headers = {}
        while True:
            line = reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            name, value = line.decode("ascii", "replace").split(":", 1)
            headers[name.lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
        ).digest()).decode("ascii")
        if " 101 " not in f" {status} " or headers.get("sec-websocket-accept") != expected:
            raise ConnectionError(f"connexion websocket refusée ({status})")

        _websocket_send(sock, 0x1, "40")
        connected = False
        deadline = time.time() + HTTP_TIMEOUT
        while time.time() < deadline:
            opcode, payload = _websocket_receive(reader)
            if opcode == 0x9:
                _websocket_send(sock, 0xA, payload)
                continue
            if opcode != 0x1:
                continue
            message = payload.decode("utf-8", "replace")
            if message == "2":
                _websocket_send(sock, 0x1, "3")
            elif message.startswith("40"):
                connected = True
                break
        if not connected:
            raise TimeoutError("connexion Socket.IO incomplète")

        packet = "42" + json.dumps(["event", {
            "id": button_id,
            "client_id": client_match.group(1),
            "listener_id": listener_id,
            "args": [],
        }], separators=(",", ":"))
        _websocket_send(sock, 0x1, packet)
        expecting_download = False
        while time.time() < deadline:
            opcode, payload = _websocket_receive(reader)
            if opcode == 0x9:
                _websocket_send(sock, 0xA, payload)
            elif opcode == 0x1:
                message = payload.decode("utf-8", "replace")
                if message == "2":
                    _websocket_send(sock, 0x1, "3")
                elif message.startswith("451-") and '"download"' in message:
                    expecting_download = True
            elif opcode == 0x2 and expecting_download:
                return payload
        raise TimeoutError("export CSV non reçu")
    finally:
        if reader is not None:
            reader.close()
        sock.close()


def parse_elements(html):
    """Extrait l'arbre d'éléments JSON que NiceGUI passe à parseElements(String.raw`...`)."""
    m = re.search(r"parseElements\(String\.raw`(.*?)`\)", html, re.S)
    if not m:
        return None
    return json.loads(m.group(1))


def _first_text(d, eid):
    el = d.get(str(eid))
    if not el:
        return None
    if el.get("text"):
        return el["text"]
    for c in el.get("children") or []:
        t = _first_text(d, c)
        if t:
            return t
    return None


def _all_texts(d, eid, acc=None):
    if acc is None:
        acc = []
    el = d.get(str(eid))
    if not el:
        return acc
    if el.get("text"):
        acc.append(el["text"])
    for c in el.get("children") or []:
        _all_texts(d, c, acc)
    return acc


def _subtree_has_class(d, eid, markers):
    """True si un élément du sous-arbre (enfants + slots) porte une classe de `markers`."""
    el = d.get(str(eid))
    if not el:
        return False
    if any(c in markers for c in el.get("class") or []):
        return True
    for c in el.get("children") or []:
        if _subtree_has_class(d, c, markers):
            return True
    for s in (el.get("slots") or {}).values():
        for sid in s.get("ids", []):
            if _subtree_has_class(d, sid, markers):
                return True
    return False


def parse_matches(d):
    """
    Renvoie une liste de groupes : [{'label': 'Journée 4', 'matches': [match, ...]}, ...]
    où match = {a, b, mid, status, poss, occ, site_live}.
      - status = 'result'    -> mid est un score "x - y"
      - status = 'scheduled' -> mid est une date "jj/mm/aaaa"
      - site_live            -> True si le site marque le match "en direct"
    """
    groups = []

    def walk(eid, group):
        el = d.get(str(eid))
        if not el:
            return
        tag = el.get("tag")
        props = el.get("props") or {}
        slots = el.get("slots") or {}

        # Un MATCH : expansion possédant un slot 'header' (l'entête avec équipes + score)
        if tag == "nicegui-expansion" and "header" in slots:
            hid = slots["header"]["ids"][0]
            grid = d.get(str(hid)) or {}
            cells = grid.get("children") or []
            team_a = _first_text(d, cells[0]) if len(cells) > 0 else None
            mid = _first_text(d, cells[1]) if len(cells) > 1 else None
            team_b = _first_text(d, cells[2]) if len(cells) > 2 else None
            # Le site signale "en direct" via un marqueur rouge dans l'entête.
            site_live = _subtree_has_class(d, hid, LIVE_CLASS_MARKERS)
            poss = occ = None
            for c in el.get("children") or []:
                for t in _all_texts(d, c):
                    if POSS_RE.match(t):
                        poss = t.strip()
                    elif SCORE_RE.match(t):
                        occ = t.strip()
            mid = (mid or "").strip()
            if DATE_RE.match(mid):
                status = "scheduled"
            elif SCORE_RE.match(mid):
                status = "result"
            else:
                status = "?"
            group["matches"].append(
                dict(a=team_a, b=team_b, mid=mid, status=status,
                     poss=poss, occ=occ, site_live=site_live)
            )
            return

        # Un GROUPE (journée / tour de coupe) : expansion avec un label
        if tag == "nicegui-expansion" and props.get("label"):
            grp = {"label": props["label"], "matches": []}
            groups.append(grp)
            for c in el.get("children") or []:
                walk(c, grp)
            return

        for c in el.get("children") or []:
            walk(c, group)

    roots = [k for k, v in d.items() if v.get("tag") == "q-page"]
    if not roots:
        roots = list(d.keys())[:1]
    sink = {"label": "?", "matches": []}
    for r in roots:
        walk(r, sink)
    return [g for g in groups if g["matches"]]


def parse_standings(d):
    """Renvoie les lignes du classement (liste de dicts) ou None."""
    for v in d.values():
        if v.get("tag") == "nicegui-table":
            return (v.get("props") or {}).get("rows")
    return None


def parse_players(html):
    """Joueurs depuis la page /joueurs.

    Renvoie la liste des lignes du tableau (dicts avec nom, poste, nom_equipe,
    age, celebrite, salaire). Liste vide si la page n'a pas le tableau attendu.
    """
    d = parse_elements(html)
    if not d:
        return []
    for v in d.values():
        if v.get("tag") == "nicegui-table":
            props = v.get("props") or {}
            fields = {(c.get("field") or c.get("name")) for c in (props.get("columns") or [])}
            if "salaire" in fields or "celebrite" in fields:
                return props.get("rows") or []
    return []


def parse_player_history_csv(data):
    """Parse l'export `/joueurs` et renvoie joueurs, célébrités, clubs et saisons.

    Les colonnes saisonnières sont détectées dynamiquement pour accepter les
    futurs `nom_equipe_N` et `celebrite_N` sans modification du programme.
    """
    if isinstance(data, bytes):
        data = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(data))
    fields = reader.fieldnames or []
    celeb_seasons = {
        int(match.group(1)): field
        for field in fields
        if (match := re.fullmatch(r"celebrite_(\d+)", field))
    }
    team_seasons = {
        int(match.group(1)): field
        for field in fields
        if (match := re.fullmatch(r"nom_equipe_(\d+)", field))
    }
    if "nom" not in fields or len(celeb_seasons) < 2:
        raise ValueError("colonnes historiques joueurs absentes")

    players, histories, clubs = [], {}, {}
    for row in reader:
        name = (row.get("nom") or "").strip()
        if not name:
            continue
        history = {}
        for season, field in celeb_seasons.items():
            try:
                raw = (row.get(field) or "").strip().replace(",", ".")
                if raw:
                    history[season] = float(raw)
            except ValueError:
                pass
        club_history = {
            season: (row.get(field) or "").strip()
            for season, field in team_seasons.items()
            if (row.get(field) or "").strip()
        }
        if history:
            histories[name] = history
        if club_history:
            clubs[name] = club_history

        latest = max(history) if history else None
        latest_club_season = max(club_history) if club_history else None

        def number(field):
            try:
                raw = (row.get(field) or "").strip().replace(",", ".")
                return float(raw) if raw else None
            except ValueError:
                return None

        players.append({
            "nom": name,
            "poste": (row.get("poste") or "").strip() or None,
            "nom_equipe": club_history.get(latest_club_season) if latest_club_season is not None else None,
            "age": number("age_actuel"),
            "salaire": number("salaire_actuel"),
            "celebrite": history.get(latest) if latest is not None else None,
        })
    if not players or not histories:
        raise ValueError("export joueurs vide")
    return {
        "players": players,
        "histories": histories,
        "clubs": clubs,
        "seasons": sorted(celeb_seasons),
    }


def player_data_cache_path():
    return os.path.join(_config_dir(), PLAYER_DATA_NAME)


def save_player_data(data):
    """Valide puis écrit atomiquement le dernier export joueurs."""
    parsed = parse_player_history_csv(data)
    path = player_data_cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="data_joueurs-", suffix=".csv",
                                     dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        os.replace(temporary, path)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise
    return parsed


def load_bundled_player_data():
    """Charge le cache actualisé, puis le CSV embarqué si aucun cache n'existe."""
    paths = [player_data_cache_path(), resource_path(PLAYER_DATA_NAME)]
    seen = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        try:
            with open(path, "rb") as f:
                return parse_player_history_csv(f.read())
        except (OSError, UnicodeError, ValueError):
            pass
    return None


def parse_team_roster(html):
    """Effectif d'une équipe depuis /equipes/<nom> : liste de {nom, poste}.

    Sur la page, chaque joueur est un lien `/joueurs/<nom>` immédiatement suivi
    de son poste (GAR, DC, …) ; on apparie dans l'ordre du document.
    """
    d = parse_elements(html)
    if not d:
        return []
    roots = [k for k, v in d.items() if v.get("tag") == "q-page"] or list(d.keys())[:1]
    seq = []

    def walk(eid):
        v = d.get(str(eid))
        if not v:
            return
        if v.get("tag") == "nicegui-link":
            href = (v.get("props") or {}).get("href", "")
            if href.startswith("/joueurs/") and href != "/joueurs":
                seq.append(("player", urllib.parse.unquote(href[len("/joueurs/"):])))
        elif v.get("text"):
            seq.append(("text", v["text"]))
        for c in v.get("children") or []:
            walk(c)
        for s in (v.get("slots") or {}).values():
            for sid in s.get("ids", []):
                walk(sid)

    for r in roots:
        walk(r)

    roster, pending = [], None
    for kind, val in seq:
        if kind == "player":
            if pending:
                roster.append({"nom": pending, "poste": None})
            pending = val
        elif kind == "text" and pending and val in PLAYER_POSTES:
            roster.append({"nom": pending, "poste": val})
            pending = None
    if pending:
        roster.append({"nom": pending, "poste": None})
    return roster


def parse_player_info(html):
    """Infos d'un joueur depuis la fiche /joueurs/<nom>.

    Renvoie {celebrite, salaire, poste, age} (None pour ce qui manque). La
    célébrité est un nœud texte ; le salaire annuel (M€), le poste et l'âge sont
    dans des blocs markdown (innerHTML échappé) — on les dé-échappe puis on retire
    les balises avant de chercher les valeurs.
    """
    d = parse_elements(html)
    info = {"celebrite": None, "salaire": None, "poste": None, "age": None}
    if not d:
        return info
    parts = []
    for v in d.values():
        if v.get("text"):
            parts.append(v["text"])
        inner = (v.get("props") or {}).get("innerHTML")
        if inner:
            parts.append(re.sub(r"<[^>]+>", " ", unescape(inner)))
    blob = "\n".join(parts)
    for key, rx, cast in (("celebrite", CELEB_RE, float), ("salaire", SALARY_RE, float),
                          ("age", AGE_RE, int), ("poste", POSTE_RE, str)):
        m = rx.search(blob)
        if not m:
            continue
        raw = m.group(1)
        try:
            info[key] = cast(raw.replace(",", ".") if cast is float else raw)
        except (ValueError, TypeError):
            pass   # valeur inattendue : on laisse None plutôt que de planter
    return info


def parse_player_clubs(html):
    """Historique des clubs depuis la fiche /joueurs/<nom> : {n_saison: club}."""
    d = parse_elements(html)
    clubs = {}
    if not d:
        return clubs
    for v in d.values():
        parts = []
        if v.get("text"):
            parts.append(v["text"])
        inner = (v.get("props") or {}).get("innerHTML")
        if inner:
            parts.append(re.sub(r"<[^>]+>", " ", unescape(inner)))
        for t in parts:
            m = CLUB_RE.search(t)
            if m:
                clubs[int(m.group(1))] = m.group(2).strip()
    return clubs


def _png_rows(png):
    """Décode un PNG RGB/RGBA 8 bits en lignes de pixels, sans dépendance externe."""
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("image PNG invalide")
    pos, compressed = 8, bytearray()
    width = height = color_type = None
    while pos + 12 <= len(png):
        size = struct.unpack(">I", png[pos:pos + 4])[0]
        kind = png[pos + 4:pos + 8]
        data = png[pos + 8:pos + 8 + size]
        pos += size + 12
        if kind == b"IHDR":
            width, height, depth, color_type, _, _, interlace = struct.unpack(
                ">IIBBBBB", data
            )
            if depth != 8 or color_type not in (2, 6) or interlace:
                raise ValueError("format PNG non pris en charge")
        elif kind == b"IDAT":
            compressed.extend(data)
        elif kind == b"IEND":
            break
    if not width or not height:
        raise ValueError("dimensions PNG absentes")

    channels = 3 if color_type == 2 else 4
    stride = width * channels
    raw = zlib.decompress(bytes(compressed))
    rows, previous, offset = [], bytearray(stride), 0

    def paeth(a, b, c):
        p = a + b - c
        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
        return a if pa <= pb and pa <= pc else (b if pb <= pc else c)

    for _ in range(height):
        filter_type = raw[offset]
        offset += 1
        row = bytearray(raw[offset:offset + stride])
        offset += stride
        for i in range(stride):
            left = row[i - channels] if i >= channels else 0
            up = previous[i]
            upper_left = previous[i - channels] if i >= channels else 0
            if filter_type == 1:
                row[i] = (row[i] + left) & 255
            elif filter_type == 2:
                row[i] = (row[i] + up) & 255
            elif filter_type == 3:
                row[i] = (row[i] + (left + up) // 2) & 255
            elif filter_type == 4:
                row[i] = (row[i] + paeth(left, up, upper_left)) & 255
            elif filter_type != 0:
                raise ValueError("filtre PNG non pris en charge")
        rows.append(row)
        previous = row
    return width, height, channels, rows


def parse_celebrity_history(html, current=None):
    """Extrait l'historique de célébrité depuis le graphique PNG d'une fiche.

    Le site ne publie pas les valeurs du graphique sous forme structurée. On
    retrouve donc les points bleus et leurs coordonnées. La valeur la plus
    récente est ancrée sur `current` (valeur exacte de /joueurs) ; les valeurs
    passées sont des estimations assez précises pour comparer les évolutions.
    Renvoie {n_saison: valeur}.
    """
    match = CELEB_CHART_RE.search(html)
    seasons = sorted(parse_player_clubs(html))
    if not match or len(seasons) < 2:
        return {}
    try:
        width, height, channels, rows = _png_rows(base64.b64decode(match.group(1)))
    except Exception:
        return {}

    def dark(x, y):
        px = rows[y][x * channels:x * channels + 3]
        return len(px) == 3 and max(px) < 55

    # Les quatre bords noirs de l'axe sont les lignes sombres les plus longues.
    left = max(range(max(1, width // 25), max(2, width // 4)),
               key=lambda x: sum(dark(x, y) for y in range(height // 20, height - height // 10)))
    right = max(range(width * 3 // 4, width - max(1, width // 100)),
                key=lambda x: sum(dark(x, y) for y in range(height // 20, height - height // 10)))
    top = max(range(height // 25, height // 3),
              key=lambda y: sum(dark(x, y) for x in range(left, right + 1)))
    bottom = max(range(height // 2, height - height // 12),
                 key=lambda y: sum(dark(x, y) for x in range(left, right + 1)))
    if right <= left or bottom <= top:
        return {}

    # Matplotlib ajoute une marge horizontale de 5 % autour des points.
    span = len(seasons) - 1
    domain = span * 1.1
    points_y = []
    for i in range(len(seasons)):
        expected_x = left + ((i + span * 0.05) / domain) * (right - left)
        ys = []
        for y in range(max(0, top - 3), min(height, bottom + 4)):
            for x in range(max(0, round(expected_x) - 8), min(width, round(expected_x) + 9)):
                r, g, b = rows[y][x * channels:x * channels + 3]
                if r > 70 and g - r > 20 and b - r > 25 and b - g > 10:
                    ys.append(y)
        if not ys:
            return {}
        ys.sort()
        mid = len(ys) // 2
        points_y.append((ys[mid] if len(ys) % 2 else (ys[mid - 1] + ys[mid]) / 2))

    scale = 100 / (bottom - top)
    anchor = float(current) if current is not None else (bottom - points_y[-1]) * scale
    latest_y = points_y[-1]
    return {
        season: round(max(0, min(100, anchor + (latest_y - y) * scale)), 1)
        for season, y in zip(seasons, points_y)
    }


def fetch_competition(name):
    """Récupère une compétition. Résultats + live + calendrier 100% via l'API (plus de
    scraping HTML). Le calendrier (/calendrier_par_compet) ne donne ni date ni score :
    les matchs à venir s'affichent sans score. Renvoie (groups, standings) au même format
    que parse_matches/parse_standings pour rester compatible partout."""
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
    websocket de /joueurs. Renvoie {players, histories, clubs, seasons} au format de
    l'ancien parse_player_history_csv (clés de saison = int) :
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
    # 1) _subtree_has_class repère le marqueur du site, même imbriqué, et NE confond
    #    pas avec les barres de possession (bg-red-200).
    sample = {
        "1": {"tag": "nicegui-expansion", "slots": {"header": {"ids": ["2"]}},
              "children": ["5"]},
        "2": {"tag": "div", "children": ["3", "4"]},
        "3": {"tag": "div", "class": ["w-2", "h-2", "rounded-full", "bg-red-500"]},
        "4": {"tag": "div", "text": "0 - 0", "class": ["text-sm", "font-bold", "text-red-600"]},
        "5": {"tag": "div", "text": "50% - 50%", "class": ["w-48", "h-3", "bg-red-200"]},
    }
    assert _subtree_has_class(sample, "2", LIVE_CLASS_MARKERS) is True
    assert _subtree_has_class(sample, "5", LIVE_CLASS_MARKERS) is False  # possession ≠ live

    # 2) un match 0-0 marqué live par le site est 'live' SANS aucun changement de score,
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

    # 7) parse_team_roster (joueur suivi de son poste) et parse_player_info.
    def _wrap(obj):
        return "x parseElements(String.raw`" + json.dumps(obj) + "`) y"
    roster_dom = {
        "0": {"tag": "q-page", "children": ["1", "2", "3", "4", "5"]},
        "1": {"tag": "nicegui-link", "props": {"href": "/joueurs"}, "text": "Joueurs"},
        "2": {"tag": "nicegui-link", "props": {"href": "/joueurs/Max%20Wei"}},
        "3": {"tag": "div", "text": "GAR"},
        "4": {"tag": "nicegui-link", "props": {"href": "/joueurs/Jo%20Do"}},
        "5": {"tag": "div", "text": "DC"},
    }
    r = parse_team_roster(_wrap(roster_dom))
    assert [(x["nom"], x["poste"]) for x in r] == [("Max Wei", "GAR"), ("Jo Do", "DC")], r
    # célébrité dans un nœud texte ; salaire/âge/poste dans un bloc markdown échappé.
    player_dom = {
        "0": {"tag": "div", "text": "Célébrité : 47.4"},
        "1": {"tag": "nicegui-markdown",
              "props": {"innerHTML": "&lt;p&gt;&lt;strong&gt;Salaire annuel :&lt;/strong&gt; 6.22 M€&lt;/p&gt;"}},
        "2": {"tag": "nicegui-markdown",
              "props": {"innerHTML": "&lt;p&gt;Poste : GAR&lt;/p&gt;&lt;p&gt;Âge : 30&lt;/p&gt;"}},
    }
    info = parse_player_info(_wrap(player_dom))
    assert info == {"celebrite": 47.4, "salaire": 6.22, "poste": "GAR", "age": 30}, info
    assert parse_player_info(_wrap({"0": {"tag": "div", "text": "rien"}})) == \
        {"celebrite": None, "salaire": None, "poste": None, "age": None}
    # robustesse : décimale à virgule acceptée, valeur aberrante ignorée sans planter
    assert parse_player_info(_wrap({"0": {"tag": "div", "text": "Salaire annuel : 6,22"}}))["salaire"] == 6.22
    assert parse_player_info(_wrap({"0": {"tag": "div", "text": "Célébrité : ."}}))["celebrite"] is None
    print("  ✓ parse_team_roster / parse_player_info OK (+ robustesse valeurs)")

    # 8) team_domain_stats : taux de finition / d'arrêt, clean sheets, occasions.
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

    # 11) parse_player_clubs : historique des clubs (et pas le menu "Saison n°0").
    clubs_dom = {
        "0": {"tag": "nicegui-markdown", "props": {"innerHTML": "&lt;p&gt;Saison n°0&lt;/p&gt;"}},
        "1": {"tag": "nicegui-markdown", "props": {"innerHTML": "&lt;p&gt;Saison 0 : Real Betis&lt;/p&gt;"}},
        "2": {"tag": "div", "text": "Saison 1 : US Lecce"},
    }
    assert parse_player_clubs(_wrap(clubs_dom)) == {0: "Real Betis", 1: "US Lecce"}

    # 12) season_domstats_from_csv : agrège un CSV de saison par équipe.
    csv_text = ("competition,Phase,Equipe dom,Equipe ext,Score dom,Score ext,"
                "Occas dom,Occas ext,Posses dom,Posses ext\n"
                "L,J1,A,B,2,1,4,2,60,40\n"
                "L,J2,B,A,0,0,1,3,45,55\n")
    sd = season_domstats_from_csv(csv_text)
    assert sd["A"]["save"] == 66.7 and sd["A"]["conv"] == 28.6 and sd["A"]["clean"] == 1
    print("  ✓ parse_player_clubs / season_domstats_from_csv OK")

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

    # 14) export joueurs : saisons détectées dynamiquement + données exactes.
    player_csv = (
        "nom,poste,age_actuel,salaire_actuel,nom_equipe_0,nom_equipe_1,"
        "nom_equipe_3,celebrite_0,celebrite_1,celebrite_3\n"
        "A,MOFF,24,12.5,X,Y,Z,70.0,75.5,71.2\n"
        "B,GAR,30,,Q,Q,,40,42,\n"
    )
    parsed = parse_player_history_csv(player_csv)
    assert parsed["seasons"] == [0, 1, 3]
    assert parsed["histories"]["A"] == {0: 70.0, 1: 75.5, 3: 71.2}
    assert parsed["clubs"]["A"] == {0: "X", 1: "Y", 3: "Z"}
    assert parsed["players"][0]["nom_equipe"] == "Z"
    assert parsed["players"][1]["salaire"] is None
    print("  ✓ parse_player_history_csv OK (saisons dynamiques, valeurs manquantes)")

    # 14b) modèle mercato / simulation (coût contrat, prolongation, force par domaine).
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
