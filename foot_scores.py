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
    """Remplace l'exe courant après fermeture, puis relance l'application."""
    if not is_windows_frozen():
        return
    current_exe = os.path.abspath(sys.executable)
    pid = os.getpid()
    script = os.path.join(tempfile.gettempdir(), f"FootLive-update-{pid}.cmd")
    batch = f"""@echo off
setlocal
set "SRC={new_exe_path}"
set "DST={current_exe}"
set "PID={pid}"

:wait
tasklist /FI "PID eq %PID%" | find "%PID%" >nul
if not errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto wait
)

move /Y "%SRC%" "%DST%" >nul
if errorlevel 1 exit /b 1
start "" "%DST%"
del "%~f0" >nul 2>nul
"""
    with open(script, "w", encoding="utf-8") as f:
        f.write(batch)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(["cmd.exe", "/c", script], close_fds=True,
                     creationflags=creationflags)

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


# ----------------------------------------------------------------------------
# Modèle Mercato / simulation (règles tirées du manuel Présentation-générale.pdf)
# ----------------------------------------------------------------------------
# Contribution de chaque poste aux domaines de jeu (% du niveau du poste, manuel p.20).
# Le niveau réel des joueurs est CACHÉ par le jeu : on estime la force d'une équipe à
# partir de la CÉLÉBRITÉ (proxy visible) pondérée par cette matrice.
POSTE_DOMAIN_WEIGHTS = {
    "GAR":  {"arrets": 80, "conservation": 20},
    "DC":   {"defense": 60, "recuperation": 20, "conservation": 20},
    "LAT":  {"defense": 50, "recuperation": 30, "creation": 20},
    "MDEF": {"recuperation": 50, "conservation": 40, "creation": 10},
    "MOFF": {"creation": 60, "concretisation": 30, "finition": 10},
    "AIL":  {"concretisation": 50, "creation": 20, "finition": 30},
    "AC":   {"finition": 60, "conservation": 20, "concretisation": 20},
}
DOMAINS = ["arrets", "defense", "recuperation", "conservation",
           "creation", "concretisation", "finition"]
DOMAIN_LABELS = {
    "arrets": "Arrêts", "defense": "Défense", "recuperation": "Récupération",
    "conservation": "Conservation", "creation": "Création",
    "concretisation": "Concrétisation", "finition": "Finition",
}
# Formations : nombre de joueurs par poste (somme = 11).
FORMATIONS = {
    "4-3-3":   {"GAR": 1, "DC": 2, "LAT": 2, "MDEF": 1, "MOFF": 2, "AIL": 2, "AC": 1},
    "4-4-2":   {"GAR": 1, "DC": 2, "LAT": 2, "MDEF": 2, "MOFF": 2, "AIL": 0, "AC": 2},
    "3-5-2":   {"GAR": 1, "DC": 3, "LAT": 0, "MDEF": 2, "MOFF": 2, "AIL": 1, "AC": 2},
    "4-2-3-1": {"GAR": 1, "DC": 2, "LAT": 2, "MDEF": 2, "MOFF": 2, "AIL": 1, "AC": 1},
}


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def contract_cost(salaire, years):
    """Coût d'un recrutement : salaire annuel payé d'avance pour `years` (1/2/3) ans."""
    s = _num(salaire)
    return None if s is None else round(s * max(1, int(years)), 2)


def extension_cost(old_salary, current_salary):
    """Coût d'une prolongation (manuel) : +10% sur le salaire courant si la célébrité a
    monté (salaire courant > ancien), sinon l'ancien salaire."""
    o, c = _num(old_salary), _num(current_salary)
    if c is None:
        return o
    if o is not None and c > o:
        return round(c * 1.10, 2)
    return o if o is not None else c


def team_domain_strength(players):
    """Force estimée par domaine (0-100) d'un effectif : célébrité (proxy du niveau caché)
    moyennée par poste puis pondérée par POSTE_DOMAIN_WEIGHTS. Un poste absent compte 0
    (équipe déséquilibrée). Renvoie {domaine: valeur|None}."""
    by_poste = {}
    for p in players:
        poste = (p.get("poste") or "").upper()
        cel = _num(p.get("celebrite"))
        if poste in POSTE_DOMAIN_WEIGHTS and cel is not None:
            by_poste.setdefault(poste, []).append(cel)
    avg_poste = {k: sum(v) / len(v) for k, v in by_poste.items()}
    out = {}
    for dom in DOMAINS:
        num = den = 0.0
        for poste, weights in POSTE_DOMAIN_WEIGHTS.items():
            w = weights.get(dom)
            if not w:
                continue
            den += w
            num += w * avg_poste.get(poste, 0.0)
        out[dom] = round(num / den, 1) if den else None
    return out


def squad_aggregate(players):
    """Résumé d'un effectif : effectif, moyennes célébrité/âge, masse salariale, postes."""
    cel = [c for c in (_num(p.get("celebrite")) for p in players) if c is not None]
    age = [a for a in (_num(p.get("age")) for p in players) if a is not None]
    sal = [s for s in (_num(p.get("salaire")) for p in players) if s is not None]
    postes = {}
    for p in players:
        k = (p.get("poste") or "?").upper()
        postes[k] = postes.get(k, 0) + 1
    return {
        "count": len(players),
        "avg_celebrite": round(sum(cel) / len(cel), 1) if cel else None,
        "avg_age": round(sum(age) / len(age), 1) if age else None,
        "total_salaire": round(sum(sal), 2) if sal else 0.0,
        "postes": postes,
    }


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
    """Récupère une compétition. Résultats + live via l'API ; calendrier (matchs à
    venir + dates) via le HTML, que l'API ne fournit pas. Renvoie (groups, standings)
    au même format que parse_matches/parse_standings pour rester compatible partout."""
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
    api_had_results = False
    for o in api_season_matches(SEASON):
        if o.get("competition") != name:
            continue
        api_had_results = True
        m = _api_match_to_dict(o)
        lo = live.pop((o.get("Equipe dom"), o.get("Equipe ext")), None)
        if lo is not None:
            _apply_live(m, lo)
        bucket(o.get("Phase") or "").append(m)

    # 2) Matchs en direct pas encore dans les résultats (flux live, sans Phase).
    for lo in live.values():
        bucket("En direct").append(_live_match_to_dict(lo))

    # 3) Calendrier (matchs à venir + dates) depuis le HTML — absent de l'API. Repli
    #    résilient : si l'API n'a renvoyé AUCUN résultat, on prend aussi les résultats
    #    HTML pour ne pas afficher une page vide. Dédoublonnage sur (a, b) déjà présents.
    try:
        d = parse_elements(http_get(SAISON_PATH + "/" + urllib.parse.quote(name)))
        if d:
            seen = {(m.get("a"), m.get("b")) for ms in groups_by_phase.values() for m in ms}
            for g in parse_matches(d):
                for m in g["matches"]:
                    if (m.get("a"), m.get("b")) in seen:
                        continue
                    if m.get("status") == "scheduled" or (
                            not api_had_results and m.get("status") == "result"):
                        bucket(g["label"]).append(m)
                        seen.add((m.get("a"), m.get("b")))
    except Exception:
        pass

    groups = [{"label": ph, "matches": groups_by_phase[ph]}
              for ph in order if groups_by_phase[ph]]
    return groups, _standings_from_leaderboard(groups)


def fetch_players():
    """Récupère et parse la liste globale des joueurs (page /joueurs)."""
    return parse_players(http_get("/joueurs"))


def fetch_team_squad(team):
    """Effectif complet d'une équipe, enrichi depuis les fiches joueurs.

    Combine /equipes/<team> (liste des joueurs) et chaque fiche /joueurs/<nom>
    (célébrité, salaire annuel, âge, poste), en parallèle. Disponible pour toutes
    les équipes, y compris celles absentes de la table globale (ex. Ligue 2).
    Renvoie une liste de dicts {nom_equipe, nom, poste, celebrite, salaire, age}.
    """
    roster = parse_team_roster(http_get("/equipes/" + urllib.parse.quote(team)))
    if not roster:
        return []

    def enrich(p):
        try:   # une fiche en échec (404 après promotion, réseau, format) ne fait
            info = parse_player_info(http_get("/joueurs/" + urllib.parse.quote(p["nom"])))
        except Exception:   # ...pas perdre tout l'effectif : ce joueur garde des stats vides
            info = {"celebrite": None, "salaire": None, "poste": None, "age": None}
        return dict(nom_equipe=team, nom=p["nom"], poste=info["poste"] or p.get("poste"),
                    celebrite=info["celebrite"], salaire=info["salaire"], age=info["age"])

    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(enrich, roster))


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
    assert sum(FORMATIONS["4-3-3"].values()) == 11
    print("  ✓ modèle mercato OK (coût contrat, supplément, force par domaine, agrégats)")

    # 15) note de version : affichée une seule fois par build.
    assert should_show_whats_new({}, "abc", enabled=True)
    assert not should_show_whats_new({"whats_new_seen_build": "abc"}, "abc", enabled=True)
    assert should_show_whats_new({"whats_new_seen_build": "old"}, "abc", enabled=True)
    assert not should_show_whats_new({}, "abc", enabled=False)
    notes = load_whats_new()
    assert "Nouveautés" in notes
    assert not any(term in notes for term in ("CSV", "/joueurs", "GitHub", "implémentation"))
    print("  ✓ note de version affichée une seule fois par build")


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

    print("→ Téléchargement de l'export joueurs via /joueurs…")
    exported = parse_player_history_csv(download_players_csv())
    assert len(exported["players"]) > 500 and len(exported["seasons"]) >= 2
    print(f"  ✓ {len(exported['players'])} joueurs, saisons {exported['seasons']}")

    print("→ Joueurs via l'API /infos_all_joueurs…")
    joueurs = api_all_joueurs(SEASON)
    assert len(joueurs) > 500, "API: infos_all_joueurs vide"
    assert {"nom", "poste", "celebrite", "salaire", "age"} <= set(joueurs[0])
    print(f"  ✓ {len(joueurs)} joueurs via l'API ; force estimée d'un échantillon : "
          f"{team_domain_strength(joueurs[:11])}")

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
        # simulate a score change to prove live-detection works
        g = groups[cur]
        if g["matches"]:
            m = g["matches"][0]
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
def run_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox

    # ---- couleurs / thème -------------------------------------------------
    BG = "#0f1115"
    CARD = "#1a1d24"
    CARD_LIVE = "#2a1410"
    FG = "#e6e6e6"
    MUTED = "#8a90a0"
    ACCENT = "#5898d4"
    LIVE = "#ff5252"
    FLASH = "#ffd54a"
    GREEN = "#6ddf6d"
    HDR = "#12141a"

    # ---- config persistante ----------------------------------------------
    def load_config():
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_config():
        cfg.update({
            "competition": comp_var.get(),
            "interval": interval_var.get(),
            "live_only": bool(live_only_var.get()),
            "topmost": bool(topmost_var.get()),
            "beep": bool(beep_var.get()),
            "geometry": root.geometry(),
        })
        try:
            write_config_file(cfg)
        except Exception:
            pass

    cfg = load_config()

    # ---- fenêtre ----------------------------------------------------------
    root = tk.Tk()
    root.title("⚽ Foot Live")
    root.configure(bg=BG)
    try:
        icon_img = tk.PhotoImage(file=resource_path("foot-live.png"))
        root.iconphoto(True, icon_img)
        root._foot_live_icon = icon_img
    except Exception:
        pass
    root.geometry(cfg.get("geometry", "470x620"))
    root.minsize(360, 300)

    comp_var = tk.StringVar(value=cfg.get("competition", ALL_KEY))
    interval_var = tk.IntVar(value=int(cfg.get("interval", 30)))
    live_only_var = tk.IntVar(value=int(cfg.get("live_only", 0)))
    topmost_var = tk.IntVar(value=int(cfg.get("topmost", 1)))
    beep_var = tk.IntVar(value=int(cfg.get("beep", 1)))
    status_var = tk.StringVar(value="démarrage…")

    root.attributes("-topmost", bool(topmost_var.get()))

    # ---- styles ttk -------------------------------------------------------
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure("TCombobox", fieldbackground=CARD, background=CARD,
                    foreground=FG, arrowcolor=FG, selectbackground=CARD,
                    selectforeground=FG)
    style.map("TCombobox", fieldbackground=[("readonly", CARD)],
              foreground=[("readonly", FG)])

    # ---- barre de contrôle ------------------------------------------------
    bar = tk.Frame(root, bg=HDR)
    bar.pack(fill="x", side="top")

    competitions = [ALL_KEY] + (cfg.get("competitions") or DEFAULT_COMPETITIONS)
    comp_box = ttk.Combobox(bar, textvariable=comp_var, values=competitions,
                            state="readonly", width=20)
    comp_box.grid(row=0, column=0, padx=4, pady=4, sticky="we")

    refresh_btn = tk.Button(bar, text="↻", bg=CARD, fg=FG, bd=0, relief="flat",
                            activebackground=ACCENT, activeforeground="#fff",
                            font=("TkDefaultFont", 11, "bold"), cursor="hand2")
    refresh_btn.grid(row=0, column=1, padx=(0, 4), pady=4)

    bar.grid_columnconfigure(0, weight=1)

    bar2 = tk.Frame(root, bg=HDR)
    bar2.pack(fill="x", side="top")

    tk.Label(bar2, text="toutes les", bg=HDR, fg=MUTED,
             font=("TkDefaultFont", 8)).pack(side="left", padx=(6, 2))
    interval_box = ttk.Combobox(bar2, textvariable=interval_var,
                                values=[10, 15, 20, 30, 45, 60, 120],
                                state="readonly", width=4)
    interval_box.pack(side="left")
    tk.Label(bar2, text="s", bg=HDR, fg=MUTED,
             font=("TkDefaultFont", 8)).pack(side="left", padx=(1, 8))

    def styled_check(parent, text, var):
        return tk.Checkbutton(parent, text=text, variable=var, bg=HDR, fg=MUTED,
                              selectcolor=CARD, activebackground=HDR,
                              activeforeground=FG, bd=0, highlightthickness=0,
                              font=("TkDefaultFont", 8), cursor="hand2")

    styled_check(bar2, "live", live_only_var).pack(side="left")
    styled_check(bar2, "épingler", topmost_var).pack(side="left")
    styled_check(bar2, "bip", beep_var).pack(side="left")

    stats_btn = tk.Button(bar2, text="📊 stats", bg=CARD, fg=FG, bd=0, relief="flat",
                          activebackground=ACCENT, activeforeground="#fff",
                          font=("TkDefaultFont", 8), cursor="hand2",
                          command=lambda: open_stats())
    stats_btn.pack(side="right", padx=(0, 6))

    players_btn = tk.Button(bar2, text="👤 joueurs", bg=CARD, fg=FG, bd=0, relief="flat",
                            activebackground=ACCENT, activeforeground="#fff",
                            font=("TkDefaultFont", 8), cursor="hand2",
                            command=lambda: open_league_players())
    players_btn.pack(side="right", padx=(0, 4))

    evolution_btn = tk.Button(bar2, text="📈", bg=CARD, fg=FG, bd=0, relief="flat",
                              activebackground=ACCENT, activeforeground="#fff",
                              font=("TkDefaultFont", 8), cursor="hand2",
                              command=lambda: open_evolution_window())
    evolution_btn.pack(side="right", padx=(0, 4))

    mercato_btn = tk.Button(bar2, text="🛒 mercato", bg=CARD, fg=FG, bd=0, relief="flat",
                            activebackground=ACCENT, activeforeground="#fff",
                            font=("TkDefaultFont", 8), cursor="hand2",
                            command=lambda: show_mercato_window())
    mercato_btn.pack(side="right", padx=(0, 4))

    # ---- zone défilante ---------------------------------------------------
    body_wrap = tk.Frame(root, bg=BG)
    body_wrap.pack(fill="both", expand=True)
    canvas = tk.Canvas(body_wrap, bg=BG, highlightthickness=0, bd=0)
    vsb = tk.Scrollbar(body_wrap, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vsb.set)
    vsb.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    inner = tk.Frame(canvas, bg=BG)
    inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")

    def _on_inner_config(_):
        canvas.configure(scrollregion=canvas.bbox("all"))
    inner.bind("<Configure>", _on_inner_config)

    def _on_canvas_config(e):
        canvas.itemconfig(inner_id, width=e.width)
    canvas.bind("<Configure>", _on_canvas_config)

    def _on_wheel(e):
        delta = -1 if (e.num == 5 or e.delta < 0) else 1
        canvas.yview_scroll(-delta, "units")
    canvas.bind_all("<MouseWheel>", _on_wheel)
    canvas.bind_all("<Button-4>", _on_wheel)
    canvas.bind_all("<Button-5>", _on_wheel)

    # ---- barre de statut --------------------------------------------------
    statusbar = tk.Label(root, textvariable=status_var, bg=HDR, fg=MUTED,
                         anchor="w", font=("TkDefaultFont", 8))
    statusbar.pack(fill="x", side="bottom")

    # ---- état partagé -----------------------------------------------------
    tracker = LiveTracker()
    state = {
        "generation": 0,       # incrémenté à chaque changement de compétition
        "wake": threading.Event(),
        "stop": False,
        "last": None,          # dernier (comp, payload) rendu (pour re-render rapide)
        "team_win": None,      # fenêtre "historique d'équipe" ouverte (réutilisée)
        "stats_win": None,     # fenêtre "stats & classement" ouverte (réutilisée)
        "squads": None,        # {équipe: stats d'effectif} (salaire/célébrité), chargé une fois
        "players": None,       # liste globale brute des joueurs (table /joueurs)
        "rosters": {},         # cache {équipe: [joueurs]} (effectifs récupérés à la demande)
        "roster_lock": threading.Lock(),   # protège roster_inflight
        "roster_inflight": set(),           # équipes en cours de récupération (anti-doublon)
        "player_win": None,    # fenêtre "fiche joueur" ouverte (réutilisée)
        "league_win": None,    # fenêtre "joueurs de la ligue par poste" (réutilisée)
        "evolution_win": None,  # fenêtre "évolutions de célébrité" (réutilisée)
        "domstats": {},        # cache {compétition: {équipe: stats par domaine}}, invalidé au poll
        "domstats_inflight": set(),   # compétitions en cours de récupération (anti-doublon)
        "history": None,       # {n_saison: {équipe: stats par domaine}} (CSV saisons passées)
        "player_clubs": {},    # cache {nom joueur: {n_saison: club}}
        "celebrity_histories": {},    # {nom joueur: {saison: célébrité exacte du CSV}}
        "evolution_players": [],      # joueurs de l'export, y compris les anciens
        "evolution_seasons": [],
        "evolution_source": "",
        "evolution_error": "",
        "evolution_loading": False,
        "evolution_done": 0,
        "evolution_total": 0,
    }

    # ---- rendu ------------------------------------------------------------
    def clear_inner():
        for w in inner.winfo_children():
            w.destroy()

    def section_header(text, color=ACCENT):
        f = tk.Frame(inner, bg=BG)
        f.pack(fill="x", padx=6, pady=(10, 2))
        tk.Label(f, text=text, bg=BG, fg=color, anchor="w",
                 font=("TkDefaultFont", 10, "bold")).pack(side="left")

    # ---- historique d'une équipe (clic sur un nom) ------------------------
    def _groups_for(comp):
        """Retrouve les journées d'une compétition depuis le dernier rendu."""
        if state["last"] is None:
            return None
        last_comp, payload = state["last"]
        if last_comp == ALL_KEY:
            return (payload or {}).get(comp)
        groups, _ = payload
        return groups if comp == last_comp else None   # seulement la compé affichée

    def _domain_stats_for(comp):
        """Stats d'équipe par domaine pour une compétition (lecture cache).

        Calcule depuis le dernier rendu si dispo, sinon renvoie {} (sans le mettre
        en cache) — un fetch éventuel passe par ensure_domstats. Le cache est purgé
        par le poll quand de nouvelles données arrivent.
        """
        cached = state["domstats"].get(comp)
        if cached is not None:
            return cached
        groups = _groups_for(comp)
        if groups:
            ds = team_domain_stats(groups)
            state["domstats"][comp] = ds
            return ds
        return {}

    def ensure_domstats(comp, on_ready=None):
        """Garantit la présence en cache des stats par domaine de `comp`.

        Calcule depuis le dernier rendu si possible, sinon récupère la compétition
        en arrière-plan (puis précharge ses effectifs) et appelle `on_ready`.
        """
        if not comp or state["domstats"].get(comp):
            return
        groups = _groups_for(comp)
        if groups:
            state["domstats"][comp] = team_domain_stats(groups)
            return
        with state["roster_lock"]:
            if comp in state["domstats_inflight"]:
                return
            state["domstats_inflight"].add(comp)

        def work():
            try:
                g, _ = fetch_competition(comp)
                ds = team_domain_stats(g)
            except Exception:
                ds = {}
            if ds:
                state["domstats"][comp] = ds
                prefetch_squads(list(ds.keys()))
            with state["roster_lock"]:
                state["domstats_inflight"].discard(comp)
            if on_ready:
                try:
                    root.after(0, on_ready)
                except (RuntimeError, tk.TclError):
                    pass
        threading.Thread(target=work, daemon=True).start()

    # ---- effectifs : cache + préchargement en tâche de fond ---------------
    def _team_squad_agg(team):
        """Stats d'effectif d'une équipe : table globale, sinon effectif en cache."""
        g = (state.get("squads") or {}).get(team)
        if g:
            return g
        rows = state["rosters"].get(team)
        return squad_stats(rows).get(team) if rows else None

    def ensure_team_cached(team):
        """Met en cache l'effectif d'une équipe (table globale si présente, sinon fetch)."""
        if not team or team in state["rosters"]:
            return
        # équipe de la table globale : son effectif est déjà connu, pas de fetch
        glob = [p for p in (state.get("players") or []) if p.get("nom_equipe") == team]
        if glob:
            state["rosters"][team] = glob
            return
        with state["roster_lock"]:
            if team in state["rosters"] or team in state["roster_inflight"]:
                return
            state["roster_inflight"].add(team)
        try:
            rows = fetch_team_squad(team)
        except Exception:
            rows = []
        state["rosters"][team] = rows
        with state["roster_lock"]:
            state["roster_inflight"].discard(team)

    def prefetch_squads(teams):
        """Précharge (en arrière-plan) les effectifs manquants pour `teams` (toutes équipes)."""
        todo = [t for t in dict.fromkeys(teams) if t and t not in state["rosters"]]
        if not todo:
            return

        def work():
            for t in todo:
                ensure_team_cached(t)
        threading.Thread(target=work, daemon=True).start()

    def open_team(comp, team):
        groups = _groups_for(comp)
        if not team or team == "?" or not groups:
            return
        show_team_window(comp, team, team_history(groups, team))

    def _bind_team_click(label, comp, team):
        """Rend un nom d'équipe cliquable (curseur main + survol + clic)."""
        if not team or team == "?":
            return
        base = label.cget("fg")
        label.configure(cursor="hand2")
        label.bind("<Enter>", lambda _e: label.configure(fg=ACCENT))
        label.bind("<Leave>", lambda _e, c=base: label.configure(fg=c))
        label.bind("<Button-1>", lambda _e: open_team(comp, team))

    def show_team_window(comp, team, hist):
        old = state.get("team_win")
        if old is not None and old.winfo_exists():
            old.destroy()
        win = tk.Toplevel(root)
        state["team_win"] = win
        win.title(f"⚽ {team}")
        win.configure(bg=BG)
        win.geometry("470x600")
        win.minsize(340, 320)
        try:
            win.attributes("-topmost", bool(topmost_var.get()))
        except tk.TclError:
            pass
        win.lift()

        cv = tk.Canvas(win, bg=BG, highlightthickness=0, bd=0)
        sb = tk.Scrollbar(win, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        box = tk.Frame(cv, bg=BG)
        bid = cv.create_window((0, 0), window=box, anchor="nw")
        box.bind("<Configure>", lambda _e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>", lambda e: cv.itemconfig(bid, width=e.width))

        def _wheel(e):
            cv.yview_scroll(-1 if (e.num == 5 or e.delta < 0) else 1, "units")
            return "break"   # n'entraîne pas le défilement de la fenêtre principale
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            win.bind(seq, _wheel)

        st = hist["stats"]
        tk.Label(box, text=team, bg=BG, fg=ACCENT, anchor="w",
                 font=("TkDefaultFont", 13, "bold")).pack(fill="x", padx=10, pady=(10, 0))
        tk.Label(box, text=f"{comp} · historique", bg=BG, fg=MUTED, anchor="w",
                 font=("TkDefaultFont", 8)).pack(fill="x", padx=10)

        def fmt(v, suffix=""):
            return f"{v}{suffix}" if v is not None else "—"

        # carte effectif : salaire (si connu) + célébrité, moyenne & médiane, et la
        # liste des joueurs. Grands clubs => table globale ; sinon on récupère
        # l'effectif sur /equipes/<team> + fiches /joueurs/<nom> en tâche de fond.
        sc = tk.Frame(box, bg=CARD)
        sc.pack(fill="x", padx=8, pady=(6, 2))
        tk.Label(sc, text="💰 Effectif", bg=CARD, fg=ACCENT, anchor="w",
                 font=("TkDefaultFont", 9, "bold")).pack(fill="x", padx=8, pady=(4, 0))
        eff_body = tk.Frame(sc, bg=CARD)
        eff_body.pack(fill="x")

        def fill_effectif(rows):
            if not eff_body.winfo_exists():
                return
            for w in eff_body.winfo_children():
                w.destroy()
            if not rows:
                tk.Label(eff_body, text="Pas de données joueurs pour cette équipe.",
                         bg=CARD, fg=MUTED, font=("TkDefaultFont", 9)).pack(
                    anchor="w", padx=8, pady=4)
                return
            agg = squad_stats(rows).get(team, {})

            def line(label, value):
                r = tk.Frame(eff_body, bg=CARD)
                r.pack(fill="x")
                tk.Label(r, text=label, bg=CARD, fg=MUTED, anchor="w",
                         font=("TkDefaultFont", 9)).pack(side="left", padx=8, pady=1)
                tk.Label(r, text=value, bg=CARD, fg=FG, anchor="e",
                         font=("TkDefaultFont", 9, "bold")).pack(side="right", padx=8)

            line("Joueurs", str(agg.get("count", len(rows))))
            line("Salaire M€ (moy. · méd.)",
                 f"{fmt(agg.get('avg_salary'))} · {fmt(agg.get('med_salary'))}")
            line("Célébrité (moy. · méd.)",
                 f"{fmt(agg.get('avg_celeb'))} · {fmt(agg.get('med_celeb'))}")
            line("Âge (moy. · méd.)",
                 f"{fmt(agg.get('avg_age'))} · {fmt(agg.get('med_age'))}")
            for p in sorted(rows, key=lambda r: (r.get("celebrite") is None,
                                                 -(r.get("celebrite") or 0))):
                pr = tk.Frame(eff_body, bg=CARD)
                pr.pack(fill="x", padx=8)
                nm = tk.Label(pr, text=f"{(p.get('poste') or '').ljust(4)} {p.get('nom') or '?'}",
                              bg=CARD, fg=FG, anchor="w", font=("TkDefaultFont", 8))
                nm.pack(side="left")
                det = []
                if p.get("celebrite") is not None:
                    det.append(f"célé {p['celebrite']}")
                if p.get("salaire") is not None:
                    det.append(f"sal {p['salaire']}M€")
                if p.get("age") is not None:
                    det.append(f"{p['age']}a")
                dl = tk.Label(pr, text="   ".join(det), bg=CARD, fg=MUTED, anchor="e",
                              font=("TkDefaultFont", 8))
                dl.pack(side="right")
                # clic sur un joueur -> sa fiche (stats par poste)
                for w in (pr, nm, dl):
                    w.configure(cursor="hand2")
                    w.bind("<Enter>", lambda _e, n=nm: n.configure(fg=ACCENT))
                    w.bind("<Leave>", lambda _e, n=nm: n.configure(fg=FG))
                    w.bind("<Button-1>", lambda _e, pp=p: open_player(comp, team, pp))

        cached = state["rosters"].get(team)
        glob = [p for p in (state.get("players") or []) if p.get("nom_equipe") == team]
        if cached is not None:
            fill_effectif(cached)
        elif glob:
            state["rosters"][team] = glob
            fill_effectif(glob)
        else:
            tk.Label(eff_body, text="chargement de l'effectif…", bg=CARD, fg=MUTED,
                     font=("TkDefaultFont", 9)).pack(anchor="w", padx=8, pady=4)

            def load_roster():
                try:
                    rows = fetch_team_squad(team)
                except Exception:
                    rows = []
                state["rosters"][team] = rows
                try:
                    root.after(0, lambda: win.winfo_exists() and fill_effectif(rows))
                except (RuntimeError, tk.TclError):
                    pass

            threading.Thread(target=load_roster, daemon=True).start()

        if st["played"] == 0:
            tk.Label(box, text="Aucun match joué pour le moment.", bg=BG, fg=MUTED,
                     font=("TkDefaultFont", 9)).pack(padx=10, pady=14)
        else:
            card = tk.Frame(box, bg=CARD)
            card.pack(fill="x", padx=8, pady=8)

            def stat_line(label, value):
                r = tk.Frame(card, bg=CARD)
                r.pack(fill="x")
                tk.Label(r, text=label, bg=CARD, fg=MUTED, anchor="w",
                         font=("TkDefaultFont", 9)).pack(side="left", padx=8, pady=2)
                tk.Label(r, text=value, bg=CARD, fg=FG, anchor="e",
                         font=("TkDefaultFont", 9, "bold")).pack(side="right", padx=8)

            stat_line("Matchs joués", str(st["played"]))
            stat_line("Bilan V-N-D", f"{st['wins']}-{st['draws']}-{st['losses']}  ·  {st['points']} pts")
            stat_line("Buts marqués", f"{st['gf']}  ({fmt(st['avg_gf'])}/match)")
            stat_line("Buts encaissés", f"{st['ga']}  ({fmt(st['avg_ga'])}/match)")
            stat_line("Possession moy.", fmt(st["avg_poss"], "%"))
            stat_line("Occasions moy.", fmt(st["avg_occ"]))

            tk.Label(box, text="Matchs", bg=BG, fg=ACCENT, anchor="w",
                     font=("TkDefaultFont", 10, "bold")).pack(fill="x", padx=10, pady=(8, 2))
            res_col = {"V": GREEN, "N": MUTED, "D": LIVE}
            for p in hist["played"]:
                row = tk.Frame(box, bg=CARD)
                row.pack(fill="x", padx=8, pady=2)
                top = tk.Frame(row, bg=CARD)
                top.pack(fill="x", padx=8, pady=(4, 0))
                tk.Label(top, text=p["res"], bg=CARD, fg=res_col[p["res"]], width=2,
                         font=("TkDefaultFont", 10, "bold")).pack(side="left")
                tk.Label(top, text=f"{p['label']} · {'dom.' if p['home'] else 'ext.'}",
                         bg=CARD, fg=MUTED, font=("TkDefaultFont", 8)).pack(side="left", padx=(2, 6))
                tk.Label(top, text=p["opp"], bg=CARD, fg=FG, anchor="w",
                         font=("TkDefaultFont", 9)).pack(side="left")
                tk.Label(top, text=p["score"], bg=CARD, fg=res_col[p["res"]],
                         font=("TkDefaultFont", 10, "bold")).pack(side="right")
                detail = []
                if p["poss"] is not None:
                    detail.append(f"poss {p['poss']}%")
                if p["occ"] is not None:
                    detail.append(f"occ {p['occ']}")
                if detail:
                    tk.Label(row, text="   ".join(detail), bg=CARD, fg=MUTED, anchor="w",
                             font=("TkDefaultFont", 8)).pack(fill="x", padx=8, pady=(0, 4))

        if hist["upcoming"]:
            tk.Label(box, text="À venir", bg=BG, fg=ACCENT, anchor="w",
                     font=("TkDefaultFont", 10, "bold")).pack(fill="x", padx=10, pady=(8, 2))
            for u in hist["upcoming"]:
                tk.Label(box, text=f"{u['label']} · {'dom.' if u['home'] else 'ext.'} · "
                                   f"{u['opp']}  ({u['date']})",
                         bg=BG, fg=MUTED, anchor="w",
                         font=("TkDefaultFont", 8)).pack(fill="x", padx=12, pady=1)

    # ---- fiche joueur : stats pertinentes selon le poste (clic) -----------
    def open_player(comp, team, prow):
        if prow and prow.get("nom"):
            show_player_window(comp, team, prow)

    def show_player_window(comp, team, prow):
        old = state.get("player_win")
        if old is not None and old.winfo_exists():
            old.destroy()
        win = tk.Toplevel(root)
        state["player_win"] = win
        nom = prow.get("nom") or "?"
        poste = prow.get("poste")
        win.title(f"👤 {nom}")
        win.configure(bg=BG)
        win.geometry("460x600")
        win.minsize(340, 320)
        try:
            win.attributes("-topmost", bool(topmost_var.get()))
        except tk.TclError:
            pass
        win.lift()

        cv = tk.Canvas(win, bg=BG, highlightthickness=0, bd=0)
        sb = tk.Scrollbar(win, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        box = tk.Frame(cv, bg=BG)
        bid = cv.create_window((0, 0), window=box, anchor="nw")
        box.bind("<Configure>", lambda _e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>", lambda e: cv.itemconfig(bid, width=e.width))

        def _wheel(e):
            cv.yview_scroll(-1 if (e.num == 5 or e.delta < 0) else 1, "units")
            return "break"
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            win.bind(seq, _wheel)

        age = prow.get("age")
        tk.Label(box, text=nom, bg=BG, fg=ACCENT, anchor="w",
                 font=("TkDefaultFont", 13, "bold")).pack(fill="x", padx=10, pady=(10, 0))
        tk.Label(box, text=f"{poste or '?'} · {team}" + (f" · {age} ans" if age is not None else ""),
                 bg=BG, fg=MUTED, anchor="w", font=("TkDefaultFont", 8)).pack(fill="x", padx=10)

        def card():
            c = tk.Frame(box, bg=CARD)
            c.pack(fill="x", padx=8, pady=(6, 2))
            return c

        def line(parent, label, value, value_fg=FG, note=None, on_click=None):
            r = tk.Frame(parent, bg=CARD)
            r.pack(fill="x")
            lbl = tk.Label(r, text=label + (" ›" if on_click else ""), bg=CARD, fg=MUTED,
                           anchor="w", font=("TkDefaultFont", 9))
            lbl.pack(side="left", padx=8, pady=1)
            val = tk.Label(r, text=value, bg=CARD, fg=value_fg, anchor="e",
                           font=("TkDefaultFont", 9, "bold"))
            val.pack(side="right", padx=8)
            note_lbl = None
            if note:
                note_lbl = tk.Label(parent, text=note, bg=CARD, fg=MUTED, anchor="e",
                                    font=("TkDefaultFont", 7))
                note_lbl.pack(fill="x", padx=8)
            if on_click:
                for w in (r, lbl, val) + ((note_lbl,) if note_lbl else ()):
                    w.configure(cursor="hand2")
                    w.bind("<Button-1>", lambda _e: on_click())
                    w.bind("<Enter>", lambda _e: lbl.configure(fg=ACCENT))
                    w.bind("<Leave>", lambda _e: lbl.configure(fg=MUTED))

        # Profil + comparaison au poste (salaire / célébrité via la table globale)
        pool = state.get("players") or []
        prof = card()
        tk.Label(prof, text="Profil", bg=CARD, fg=ACCENT, anchor="w",
                 font=("TkDefaultFont", 9, "bold")).pack(fill="x", padx=8, pady=(4, 0))
        for label, field, val, unit in (("Salaire", "salaire", prow.get("salaire"), " M€"),
                                        ("Célébrité", "celebrite", prow.get("celebrite"), "")):
            pc = position_percentile(pool, poste, field, val)
            note = (f"méd. {poste} {pc['median']}{unit} · > {pc['pct']}% des {poste}"
                    if pc else None)
            line(prof, label, f"{val}{unit}" if val is not None else "—", note=note)

        # Stats par poste = rendement de l'équipe dans les domaines du poste
        ds = _domain_stats_for(comp)
        tds = ds.get(team)
        tk.Label(box, text=f"Stats par poste — {poste or '?'}", bg=BG, fg=ACCENT, anchor="w",
                 font=("TkDefaultFont", 10, "bold")).pack(fill="x", padx=10, pady=(8, 0))
        tk.Label(box, text="rendement de l'équipe dans les domaines du poste · "
                           "rang dans la compétition",
                 bg=BG, fg=MUTED, anchor="w", font=("TkDefaultFont", 7)).pack(fill="x", padx=10)

        metrics = POSTE_STATS.get(poste, [("Buts / match", "gf_pm", True),
                                          ("Buts encaissés / match", "ga_pm", False),
                                          ("Possession moy.", "poss", True)])
        if not tds:
            tk.Label(box, text="Pas encore de match joué cette saison.", bg=BG, fg=MUTED,
                     font=("TkDefaultFont", 9)).pack(anchor="w", padx=12, pady=6)
        else:
            sc = card()

            def fmt_metric(key, v):
                if v is None:
                    return "—"
                if key == "clean":
                    return f"{v}/{tds['played']}"
                return f"{v}%" if key in PERCENT_STATS else f"{v}"

            def rank(key, high):
                vals = [s[key] for s in ds.values() if s.get(key) is not None]
                me = tds.get(key)
                if me is None or not vals:
                    return None
                better = sum(1 for v in vals if (v > me if high else v < me))
                return better + 1, len(vals)

            for label, key, high in metrics:
                rk = rank(key, high)
                if rk:
                    pos, n = rk
                    col = GREEN if pos <= max(1, n / 3) else (LIVE if pos > 2 * n / 3 else FG)
                    note = f"{pos}ᵉ/{n} de la compétition — voir tous les {poste}"
                else:
                    col, note = FG, None
                line(sc, label, fmt_metric(key, tds.get(key)), value_fg=col, note=note,
                     on_click=lambda k=key, h=high: show_league_window(comp, poste, k, h))

        # Performances par saison : clubs successifs + stat clé du poste, par saison
        prim = (POSTE_STATS.get(poste) or [("Buts / match", "gf_pm", True)])[0]
        prim_label, prim_key, _ = prim
        tk.Label(box, text="Performances par saison", bg=BG, fg=ACCENT, anchor="w",
                 font=("TkDefaultFont", 10, "bold")).pack(fill="x", padx=10, pady=(8, 0))
        tk.Label(box, text=f"club de la saison · {prim_label} (rendement de l'équipe)",
                 bg=BG, fg=MUTED, anchor="w", font=("TkDefaultFont", 7)).pack(fill="x", padx=10)
        seas = tk.Frame(box, bg=BG)
        seas.pack(fill="x")

        def fill_seasons(clubs):
            if not seas.winfo_exists():
                return
            for w in seas.winfo_children():
                w.destroy()
            if not clubs:
                tk.Label(seas, text="Parcours indisponible.", bg=BG, fg=MUTED,
                         font=("TkDefaultFont", 8)).pack(anchor="w", padx=12, pady=2)
                return
            hist = state.get("history") or {}
            cur_ds = _domain_stats_for(comp)
            for n in sorted(clubs):
                club = clubs[n]
                tds_n = cur_ds.get(club) if n == SEASON else (hist.get(n) or {}).get(club)
                v = tds_n.get(prim_key) if tds_n else None
                vtxt = "—" if v is None else (f"{v}%" if prim_key in PERCENT_STATS else f"{v}")
                row = tk.Frame(seas, bg=CARD)
                row.pack(fill="x", padx=8, pady=1)
                suffix = " (en cours)" if n == SEASON else ""
                tk.Label(row, text=f"Saison {n} · {club}{suffix}", bg=CARD, fg=FG, anchor="w",
                         font=("TkDefaultFont", 8)).pack(side="left", padx=8, pady=2)
                tk.Label(row, text=f"{prim_label} {vtxt}" if tds_n else "données indispo.",
                         bg=CARD, fg=MUTED, anchor="e",
                         font=("TkDefaultFont", 8)).pack(side="right", padx=8)

        cached_clubs = state["player_clubs"].get(nom)
        if cached_clubs is not None:
            fill_seasons(cached_clubs)
        else:
            tk.Label(seas, text="chargement du parcours…", bg=BG, fg=MUTED,
                     font=("TkDefaultFont", 8)).pack(anchor="w", padx=12, pady=2)

            def load_clubs():
                try:
                    clubs = parse_player_clubs(http_get("/joueurs/" + urllib.parse.quote(nom)))
                except Exception:
                    clubs = {}
                state["player_clubs"][nom] = clubs
                try:
                    root.after(0, lambda: win.winfo_exists() and fill_seasons(clubs))
                except (RuntimeError, tk.TclError):
                    pass

            threading.Thread(target=load_clubs, daemon=True).start()

    # ---- évolutions de célébrité par saison et par poste ------------------
    def apply_player_data(parsed, source):
        state["celebrity_histories"] = parsed["histories"]
        state["evolution_players"] = parsed["players"]
        state["evolution_seasons"] = parsed["seasons"]
        state["player_clubs"].update(parsed["clubs"])
        state["evolution_done"] = len(parsed["histories"])
        state["evolution_total"] = len(parsed["players"])
        state["evolution_source"] = source

    def load_player_data():
        """Charge immédiatement le CSV local, puis demande la version du site."""
        parsed = load_bundled_player_data()
        if parsed:
            apply_player_data(parsed, "CSV local")
        start_evolution_load(force=True)

    def start_evolution_load(force=False):
        """Actualise en arrière-plan l'export CSV produit par la page `/joueurs`."""
        if state["evolution_loading"]:
            return
        if state["celebrity_histories"] and not force:
            return
        state["evolution_loading"] = True
        state["evolution_error"] = ""

        def work():
            try:
                parsed = save_player_data(download_players_csv())
                apply_player_data(parsed, "CSV /joueurs actualisé")
            except Exception as exc:
                state["evolution_error"] = str(exc)[:120]
            finally:
                state["evolution_loading"] = False

        threading.Thread(target=work, daemon=True).start()

    def open_evolution_window():
        old = state.get("evolution_win")
        if old is not None and old.winfo_exists():
            old.lift()
            return
        win = tk.Toplevel(root)
        state["evolution_win"] = win
        win.title("📈 Évolutions de célébrité")
        win.configure(bg=BG)
        win.geometry("1000x680")
        win.minsize(760, 400)
        try:
            win.attributes("-topmost", bool(topmost_var.get()))
        except tk.TclError:
            pass
        win.lift()

        periods = {}
        period_v = tk.StringVar()
        poste_v = tk.StringVar(value="Tous")
        status = tk.StringVar(value="chargement des joueurs…")
        source = tk.StringVar(value="Données exactes de l'export CSV /joueurs.")

        bar = tk.Frame(win, bg=HDR)
        bar.pack(fill="x", side="top")
        tk.Label(bar, text="Période", bg=HDR, fg=MUTED,
                 font=("TkDefaultFont", 8)).pack(side="left", padx=(6, 2))
        period_box = ttk.Combobox(bar, textvariable=period_v, values=list(periods),
                                  state="readonly", width=14)
        period_box.pack(side="left", padx=(0, 8), pady=4)
        tk.Label(bar, text="Poste", bg=HDR, fg=MUTED,
                 font=("TkDefaultFont", 8)).pack(side="left", padx=(0, 2))
        poste_box = ttk.Combobox(bar, textvariable=poste_v,
                                 values=["Tous"] + list(PLAYER_POSTES),
                                 state="readonly", width=7)
        poste_box.pack(side="left", pady=4)
        refresh = tk.Button(bar, text="↻", bg=CARD, fg=FG, bd=0, relief="flat",
                            activebackground=ACCENT, activeforeground="#fff",
                            font=("TkDefaultFont", 10, "bold"), cursor="hand2",
                            command=lambda: start_evolution_load(force=True))
        refresh.pack(side="right", padx=6, pady=4)

        tk.Label(win, textvariable=status, bg=BG, fg=MUTED, anchor="w",
                 font=("TkDefaultFont", 8)).pack(fill="x", padx=8, pady=(4, 0))
        tk.Label(win, textvariable=source, bg=BG, fg=MUTED, anchor="w",
                 font=("TkDefaultFont", 7)).pack(fill="x", padx=8)

        cv = tk.Canvas(win, bg=BG, highlightthickness=0, bd=0)
        sb = tk.Scrollbar(win, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        content = tk.Frame(cv, bg=BG)
        bid = cv.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>", lambda e: cv.itemconfig(bid, width=e.width))

        def _wheel(e):
            cv.yview_scroll(-1 if (e.num == 5 or e.delta < 0) else 1, "units")
            return "break"
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            win.bind(seq, _wheel)

        def player_comp(team):
            current = comp_var.get()
            if current != ALL_KEY:
                return current
            if state["last"] and state["last"][0] == ALL_KEY:
                for comp, groups in (state["last"][1] or {}).items():
                    if any(team in (m.get("a"), m.get("b"))
                           for group in groups for m in group["matches"]):
                        return comp
            return competitions[1] if len(competitions) > 1 else current

        def open_row(row):
            open_player(player_comp(row["team"]), row["team"], row["player"])

        def delta_text(value):
            return f"{value:+.1f}"

        def cell(parent, row, col, text, bg, fg=FG, left=False, bold=False, width=None):
            label = tk.Label(parent, text=text, bg=bg, fg=fg,
                             font=("TkDefaultFont", 8, "bold" if bold else "normal"),
                             anchor="w" if left else "center", padx=4, width=width)
            label.grid(row=row, column=col, sticky="we", padx=1)
            return label

        def player_table(parent, title, rows, descending):
            tk.Label(parent, text=title, bg=BG, fg=ACCENT, anchor="w",
                     font=("TkDefaultFont", 10, "bold")).pack(
                         fill="x", padx=10, pady=(10, 2))
            grid = tk.Frame(parent, bg=BG)
            grid.pack(fill="x", padx=6)
            headers = ["#", "Joueur", "Poste", "Âge", "Équipe", "Avant → après", "Δ"]
            widths = [3, 20, 6, 5, 18, 15, 7]
            for ci, header in enumerate(headers):
                cell(grid, 0, ci, header, HDR, MUTED, left=ci in (1, 4), bold=True,
                     width=widths[ci])
            ordered = sorted(rows, key=lambda r: r["delta"], reverse=descending)[:12]
            for ri, row in enumerate(ordered, start=1):
                rowbg = CARD if ri % 2 else BG
                color = GREEN if row["delta"] > 0 else (LIVE if row["delta"] < 0 else MUTED)
                age = row["player"].get("age")
                age_text = str(int(age)) if isinstance(age, (int, float)) else "—"
                labels = [
                    cell(grid, ri, 0, str(ri), rowbg, MUTED, width=widths[0]),
                    cell(grid, ri, 1, row["nom"] or "?", rowbg, FG, left=True, width=widths[1]),
                    cell(grid, ri, 2, row["poste"] or "?", rowbg, MUTED, width=widths[2]),
                    cell(grid, ri, 3, age_text, rowbg, MUTED, width=widths[3]),
                    cell(grid, ri, 4, row["team"] or "?", rowbg, MUTED, left=True, width=widths[4]),
                    cell(grid, ri, 5, f"{row['before']:.1f} → {row['after']:.1f}", rowbg,
                         width=widths[5]),
                    cell(grid, ri, 6, delta_text(row["delta"]), rowbg, color, bold=True,
                         width=widths[6]),
                ]
                for label in labels:
                    label.configure(cursor="hand2")
                    label.bind("<Button-1>", lambda _e, r=row: open_row(r))
            grid.grid_columnconfigure(1, weight=1)
            grid.grid_columnconfigure(4, weight=1)

        def render():
            page = tk.Frame(content, bg=BG)

            def show_page():
                for widget in content.winfo_children():
                    if widget is not page:
                        widget.destroy()
                page.pack(fill="both", expand=True)

            seasons = state.get("evolution_seasons") or []
            fresh_periods = {
                f"Saison {before} → {after}": (before, after)
                for before, after in zip(seasons, seasons[1:])
            }
            if fresh_periods != periods:
                periods.clear()
                periods.update(fresh_periods)
                period_box.configure(values=list(periods))
                if period_v.get() not in periods:
                    meaningful = [
                        label for label, (before, after) in periods.items()
                        if any(
                            history.get(before) is not None
                            and history.get(after) is not None
                            and history[before] != history[after]
                            for history in state["celebrity_histories"].values()
                        )
                    ]
                    period_v.set(meaningful[-1] if meaningful else next(reversed(periods), ""))

            players = state.get("evolution_players") or []
            if not players:
                status.set("chargement de l'export joueurs…")
                tk.Label(page, text="Chargement…", bg=BG, fg=MUTED,
                         font=("TkDefaultFont", 9)).pack(anchor="w", padx=10, pady=8)
                show_page()
                return

            start_evolution_load()
            done, total = state["evolution_done"], state["evolution_total"]
            refresh_text = " · actualisation…" if state["evolution_loading"] else ""
            error_text = f" · actualisation impossible : {state['evolution_error']}" \
                if state["evolution_error"] else ""
            status.set(f"{done}/{total} historiques CSV chargés{refresh_text}{error_text}")
            source.set(f"Données exactes · {state['evolution_source'] or 'export CSV /joueurs'}")
            if not periods or period_v.get() not in periods:
                tk.Label(page, text="Pas encore assez de saisons dans l'export.",
                         bg=BG, fg=MUTED, font=("TkDefaultFont", 9)).pack(
                             anchor="w", padx=10, pady=10)
                show_page()
                return
            season_from, season_to = periods[period_v.get()]
            all_rows = celebrity_evolution_rows(
                players, state["celebrity_histories"], season_from, season_to
            )
            selected = None if poste_v.get() == "Tous" else poste_v.get()
            rows = [r for r in all_rows if not selected or r["poste"] == selected]

            tk.Label(page, text="Évolution moyenne par poste", bg=BG, fg=ACCENT, anchor="w",
                     font=("TkDefaultFont", 10, "bold")).pack(
                         fill="x", padx=10, pady=(8, 2))
            role_grid = tk.Frame(page, bg=BG)
            role_grid.pack(fill="x", padx=6)
            role_headers = ["Poste", "Joueurs", "Moyenne", "Plus forte hausse", "Plus forte baisse"]
            role_widths = [7, 8, 9, 28, 28]
            for ci, header in enumerate(role_headers):
                cell(role_grid, 0, ci, header, HDR, MUTED, left=ci in (0, 3, 4), bold=True,
                     width=role_widths[ci])
            for ri, role in enumerate(role_evolution_summary(all_rows), start=1):
                rowbg = CARD if ri % 2 else BG
                avg_color = GREEN if role["avg"] > 0 else (LIVE if role["avg"] < 0 else MUTED)
                labels = [
                    cell(role_grid, ri, 0, role["poste"], rowbg, FG, left=True, bold=True,
                         width=role_widths[0]),
                    cell(role_grid, ri, 1, str(role["count"]), rowbg, width=role_widths[1]),
                    cell(role_grid, ri, 2, delta_text(role["avg"]), rowbg, avg_color, bold=True,
                         width=role_widths[2]),
                    cell(role_grid, ri, 3, f"{role['rise']['nom']} "
                         f"({delta_text(role['rise']['delta'])})", rowbg, GREEN, left=True,
                         width=role_widths[3]),
                    cell(role_grid, ri, 4, f"{role['drop']['nom']} "
                         f"({delta_text(role['drop']['delta'])})", rowbg, LIVE, left=True,
                         width=role_widths[4]),
                ]
                for label in labels:
                    label.configure(cursor="hand2")
                    label.bind("<Button-1>", lambda _e, p=role["poste"]:
                               (poste_v.set(p), render()))
            role_grid.grid_columnconfigure(3, weight=1)
            role_grid.grid_columnconfigure(4, weight=1)

            label = selected or "tous postes"
            if rows:
                player_table(page, f"Plus fortes hausses · {label}", rows, True)
                player_table(page, f"Plus fortes baisses · {label}", rows, False)
            else:
                tk.Label(page, text="Pas encore assez d'historiques pour cette sélection.",
                         bg=BG, fg=MUTED, font=("TkDefaultFont", 9)).pack(
                             anchor="w", padx=10, pady=10)
            show_page()

        progress = {"sig": None}

        def tick():
            if not win.winfo_exists():
                return
            sig = (state["evolution_done"] // 50, state["evolution_total"],
                   state["evolution_loading"], len(state.get("evolution_players") or []),
                   tuple(state.get("evolution_seasons") or []),
                   state["evolution_source"], state["evolution_error"],
                   period_v.get(), poste_v.get())
            if sig != progress["sig"]:
                progress["sig"] = sig
                render()
            win.after(800, tick)

        period_box.bind("<<ComboboxSelected>>", lambda *_: render())
        poste_box.bind("<<ComboboxSelected>>", lambda *_: render())
        render()
        win.after(800, tick)

    # ---- joueurs de la ligue par poste (bouton 👤 / clic sur une stat) ----
    def open_league_players():
        comp = comp_var.get()
        if comp == ALL_KEY:
            names = competitions[1:]
            comp = names[0] if names else None
        if comp:
            show_league_window(comp)

    def show_mercato_window():
        """Simulateur de mercato : choisir une formation + un budget, remplir les
        postes avec des joueurs filtrés par prix, voir le coût (salaire × années payé
        d'avance) vs budget et la force estimée de l'équipe par domaine."""
        old = state.get("mercato_win")
        if old is not None and old.winfo_exists():
            old.destroy()
            return
        win = tk.Toplevel(root)
        state["mercato_win"] = win
        win.title("🛒 Mercato — simulateur d'équipe")
        win.configure(bg=BG)
        win.geometry("760x720")
        win.minsize(560, 480)
        try:
            win.attributes("-topmost", bool(topmost_var.get()))
        except tk.TclError:
            pass
        win.lift()

        pool = {"players": state.get("mercato_pool")}
        squad = {}          # slot_id -> joueur
        years = {}          # slot_id -> nb d'années de contrat (1/2/3)
        formation_v = tk.StringVar(value="4-3-3")
        cap_v = tk.StringVar(value="250")
        pmin_v = tk.StringVar(value="0")
        pmax_v = tk.StringVar(value="40")

        def _f(var, default):
            try:
                return float(var.get())
            except (TypeError, ValueError):
                return default

        def slots():
            out = []
            for poste, n in FORMATIONS.get(formation_v.get(), {}).items():
                for i in range(n):
                    out.append((poste, f"{poste}{i + 1}"))
            return out

        bar = tk.Frame(win, bg=HDR)
        bar.pack(fill="x", side="top")
        tk.Label(bar, text="Formation", bg=HDR, fg=MUTED, font=("TkDefaultFont", 8)).pack(side="left", padx=(6, 2))
        ttk.Combobox(bar, textvariable=formation_v, values=list(FORMATIONS), state="readonly",
                     width=8).pack(side="left", padx=(0, 8), pady=4)
        tk.Label(bar, text="Budget M€", bg=HDR, fg=MUTED, font=("TkDefaultFont", 8)).pack(side="left", padx=(0, 2))
        tk.Entry(bar, textvariable=cap_v, width=6, bg=CARD, fg=FG, insertbackground=FG,
                 relief="flat").pack(side="left", padx=(0, 8), pady=4)
        tk.Label(bar, text="Prix", bg=HDR, fg=MUTED, font=("TkDefaultFont", 8)).pack(side="left", padx=(0, 2))
        tk.Entry(bar, textvariable=pmin_v, width=4, bg=CARD, fg=FG, insertbackground=FG,
                 relief="flat").pack(side="left", pady=4)
        tk.Label(bar, text="à", bg=HDR, fg=MUTED, font=("TkDefaultFont", 8)).pack(side="left", padx=2)
        tk.Entry(bar, textvariable=pmax_v, width=4, bg=CARD, fg=FG, insertbackground=FG,
                 relief="flat").pack(side="left", padx=(0, 8), pady=4)
        tk.Button(bar, text="vider", bg=CARD, fg=FG, bd=0, relief="flat", cursor="hand2",
                  activebackground=ACCENT, activeforeground="#fff", font=("TkDefaultFont", 8),
                  command=lambda: (squad.clear(), years.clear(), render())).pack(side="right", padx=6, pady=4)

        tk.Label(win, text="Choisis une formation et un budget, clique « + » pour recruter à chaque poste "
                           "(salaire × années, payé d'avance). Force estimée d'après la célébrité.",
                 bg=BG, fg=MUTED, anchor="w", justify="left", wraplength=720,
                 font=("TkDefaultFont", 8)).pack(fill="x", padx=8, pady=(4, 0))

        cv = tk.Canvas(win, bg=BG, highlightthickness=0, bd=0)
        sb = tk.Scrollbar(win, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        content = tk.Frame(cv, bg=BG)
        bid = cv.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>", lambda e: cv.itemconfig(bid, width=e.width))

        def _wheel(e):
            cv.yview_scroll(-1 if (e.num == 5 or e.delta < 0) else 1, "units")
            return "break"
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            win.bind(seq, _wheel)

        def eligible(poste):
            lo, hi = _f(pmin_v, 0), _f(pmax_v, 1e9)
            taken = {p.get("id") for p in squad.values() if p}
            out = [p for p in (pool["players"] or [])
                   if (p.get("poste") or "").upper() == poste
                   and p.get("id") not in taken
                   and (lambda s: s is not None and lo <= s <= hi)(_num(p.get("salaire")))]
            out.sort(key=lambda p: -(_num(p.get("celebrite")) or 0))
            return out

        def open_picker(poste, slot_id):
            cands = eligible(poste)
            pick = tk.Toplevel(win)
            pick.title(f"Recruter — {poste}")
            pick.configure(bg=BG)
            pick.geometry("420x460")
            try:
                pick.attributes("-topmost", bool(topmost_var.get()))
            except tk.TclError:
                pass
            tk.Label(pick, text=f"{len(cands)} joueurs · {poste} · prix {_f(pmin_v,0):g}-{_f(pmax_v,0):g} M€",
                     bg=BG, fg=MUTED, anchor="w", font=("TkDefaultFont", 8)).pack(fill="x", padx=8, pady=4)
            lb = tk.Listbox(pick, bg=CARD, fg=FG, selectbackground=ACCENT, relief="flat",
                            font=("TkDefaultFont", 9), activestyle="none")
            lb.pack(fill="both", expand=True, padx=8, pady=(0, 8))
            for p in cands:
                lb.insert("end", f"{p.get('nom')}  ·  {p.get('nom_equipe')}  ·  "
                                 f"célé {p.get('celebrite')}  ·  {p.get('salaire')} M€/an")

            def assign(*_):
                sel = lb.curselection()
                if sel:
                    squad[slot_id] = cands[sel[0]]
                    years.setdefault(slot_id, 1)
                    pick.destroy()
                    render()
            lb.bind("<Double-Button-1>", assign)
            tk.Button(pick, text="Recruter", bg=ACCENT, fg="#fff", bd=0, relief="flat",
                      cursor="hand2", command=assign).pack(fill="x", padx=8, pady=(0, 8))

        def _draw_strength(parent, strength):
            h = 8 + 18 * len(DOMAINS)
            chart = tk.Canvas(parent, bg=CARD, height=h, highlightthickness=0, bd=0)
            chart.pack(fill="x", padx=8, pady=4)

            def redraw(*_):
                chart.delete("all")
                w = chart.winfo_width() or 700
                x0, barw = 120, max(40, w - 170)
                for i, dom in enumerate(DOMAINS):
                    y = 6 + i * 18
                    v = strength.get(dom) or 0
                    chart.create_text(6, y + 6, text=DOMAIN_LABELS[dom], fill=FG, anchor="w",
                                      font=("TkDefaultFont", 8))
                    chart.create_rectangle(x0, y, x0 + barw, y + 11, fill=HDR, outline="")
                    chart.create_rectangle(x0, y, x0 + barw * min(100, v) / 100.0, y + 11,
                                           fill=ACCENT, outline="")
                    chart.create_text(x0 + barw + 4, y + 6, text=f"{v:g}", fill=MUTED, anchor="w",
                                      font=("TkDefaultFont", 8))
            chart.bind("<Configure>", redraw)
            redraw()

        def render(*_):
            for w in content.winfo_children():
                w.destroy()
            if pool["players"] is None:
                tk.Label(content, text="Chargement des joueurs…", bg=BG, fg=MUTED,
                         font=("TkDefaultFont", 9)).pack(anchor="w", padx=10, pady=8)
                return
            total = 0.0
            filled = []
            grid = tk.Frame(content, bg=BG)
            grid.pack(fill="x", padx=6, pady=(4, 4))
            for ri, (poste, slot_id) in enumerate(slots()):
                p = squad.get(slot_id)
                rowbg = CARD if ri % 2 else BG
                tk.Label(grid, text=poste, bg=rowbg, fg=ACCENT, width=5, anchor="w",
                         font=("TkDefaultFont", 8, "bold")).grid(row=ri, column=0, sticky="we", padx=1, pady=1)
                if p:
                    yr = years.get(slot_id, 1)
                    cost = contract_cost(p.get("salaire"), yr) or 0
                    total += cost
                    filled.append(p)
                    tk.Label(grid, text=f"{p.get('nom')} · {p.get('nom_equipe')}", bg=rowbg, fg=FG,
                             anchor="w", font=("TkDefaultFont", 8)).grid(row=ri, column=1, sticky="we", padx=2)
                    yv = tk.StringVar(value=str(yr))
                    yb = ttk.Combobox(grid, textvariable=yv, values=["1", "2", "3"], state="readonly", width=2)
                    yb.grid(row=ri, column=2, padx=2)
                    yb.bind("<<ComboboxSelected>>",
                            lambda _e, s=slot_id, v=yv: (years.__setitem__(s, int(v.get())), render()))
                    tk.Label(grid, text=f"{cost:g} M€", bg=rowbg, fg=MUTED, width=9,
                             font=("TkDefaultFont", 8)).grid(row=ri, column=3, padx=2)
                    tk.Button(grid, text="✕", bg=rowbg, fg=LIVE, bd=0, relief="flat", cursor="hand2",
                              font=("TkDefaultFont", 8),
                              command=lambda s=slot_id: (squad.pop(s, None), years.pop(s, None), render())
                              ).grid(row=ri, column=4, padx=2)
                else:
                    tk.Label(grid, text="— vide —", bg=rowbg, fg=MUTED, anchor="w",
                             font=("TkDefaultFont", 8)).grid(row=ri, column=1, sticky="we", padx=2)
                    tk.Button(grid, text="+ recruter", bg=rowbg, fg=GREEN, bd=0, relief="flat",
                              cursor="hand2", font=("TkDefaultFont", 8),
                              command=lambda ps=poste, s=slot_id: open_picker(ps, s)
                              ).grid(row=ri, column=3, columnspan=2, sticky="we", padx=2)
            grid.grid_columnconfigure(1, weight=1)

            cap = _f(cap_v, 0)
            agg = squad_aggregate(filled)
            over = total > cap > 0
            summ = tk.Frame(content, bg=BG)
            summ.pack(fill="x", padx=10, pady=(8, 2))
            tk.Label(summ, text=f"Budget : {total:g} / {cap:g} M€", bg=BG,
                     fg=(LIVE if over else GREEN), anchor="w",
                     font=("TkDefaultFont", 11, "bold")).pack(side="left")
            tk.Label(summ, text=f"  {agg['count']}/11 joueurs · célé moy. {agg['avg_celebrite'] or '—'} · "
                                f"âge moy. {agg['avg_age'] or '—'}", bg=BG, fg=MUTED,
                     anchor="w", font=("TkDefaultFont", 9)).pack(side="left")
            if over:
                tk.Label(content, text="⚠ budget dépassé", bg=BG, fg=LIVE, anchor="w",
                         font=("TkDefaultFont", 8)).pack(fill="x", padx=10)
            tk.Label(content, text="Force estimée par domaine", bg=BG, fg=ACCENT, anchor="w",
                     font=("TkDefaultFont", 9, "bold")).pack(fill="x", padx=10, pady=(6, 0))
            _draw_strength(content, team_domain_strength(filled))

        for v in (formation_v, cap_v, pmin_v, pmax_v):
            v.trace_add("write", lambda *_: render())
        render()

        if pool["players"] is None:
            def load():
                ps = api_all_joueurs(SEASON)
                state["mercato_pool"] = ps
                pool["players"] = ps
                if win.winfo_exists():
                    root.after(0, render)
            threading.Thread(target=load, daemon=True).start()

    def show_league_window(comp, poste=None, sort_key=None, sort_high=True):
        old = state.get("league_win")
        if old is not None and old.winfo_exists():
            old.destroy()
        win = tk.Toplevel(root)
        state["league_win"] = win
        win.title("👤 Joueurs par poste")
        win.configure(bg=BG)
        win.geometry("640x620")
        win.minsize(460, 320)
        try:
            win.attributes("-topmost", bool(topmost_var.get()))
        except tk.TclError:
            pass
        win.lift()

        real_comps = competitions[1:]
        comp_v = tk.StringVar(value=comp if comp in real_comps else (real_comps[0] if real_comps else comp))
        poste_v = tk.StringVar(value=poste or "GAR")

        bar = tk.Frame(win, bg=HDR)
        bar.pack(fill="x", side="top")
        tk.Label(bar, text="Compétition", bg=HDR, fg=MUTED,
                 font=("TkDefaultFont", 8)).pack(side="left", padx=(6, 2))
        comp_box = ttk.Combobox(bar, textvariable=comp_v, values=real_comps, state="readonly", width=16)
        comp_box.pack(side="left", padx=(0, 8), pady=4)
        tk.Label(bar, text="Poste", bg=HDR, fg=MUTED,
                 font=("TkDefaultFont", 8)).pack(side="left", padx=(0, 2))
        poste_box = ttk.Combobox(bar, textvariable=poste_v, values=list(PLAYER_POSTES),
                                 state="readonly", width=6)
        poste_box.pack(side="left", pady=4)

        tk.Label(win, text="Stats d'équipe selon le poste · clique un en-tête pour trier · "
                           "clique un joueur pour sa fiche",
                 bg=BG, fg=MUTED, anchor="w", font=("TkDefaultFont", 8)).pack(fill="x", padx=8, pady=(4, 0))

        cv = tk.Canvas(win, bg=BG, highlightthickness=0, bd=0)
        sb = tk.Scrollbar(win, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        content = tk.Frame(cv, bg=BG)
        bid = cv.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>", lambda e: cv.itemconfig(bid, width=e.width))

        def _wheel(e):
            cv.yview_scroll(-1 if (e.num == 5 or e.delta < 0) else 1, "units")
            return "break"
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            win.bind(seq, _wheel)

        def metrics_for(p):
            return POSTE_STATS.get(p, [("Buts / match", "gf_pm", True),
                                      ("Buts encaissés / match", "ga_pm", False),
                                      ("Possession moy.", "poss", True)])

        def fmt(key, v):
            if v is None:
                return "—"
            return f"{v}%" if key in PERCENT_STATS else f"{v}"

        sortst = {"key": sort_key, "rev": sort_high}

        prog = {"gen": 0, "sig": None}

        def render(*_):
            for w in content.winfo_children():
                w.destroy()
            cur_comp, cur_poste = comp_v.get(), poste_v.get()
            ensure_domstats(cur_comp, on_ready=lambda: win.winfo_exists() and render())
            ds = state["domstats"].get(cur_comp) or {}
            if ds:
                prefetch_squads(list(ds.keys()))
            mets = metrics_for(cur_poste)
            highs = {k: h for _, k, h in mets}
            if sortst["key"] not in highs:
                sortst["key"], sortst["rev"] = mets[0][1], mets[0][2]
            key = sortst["key"]
            rows = league_players(ds, state["rosters"], cur_poste)
            rows.sort(key=lambda r: (r["stats"].get(key) is None,
                                     -(r["stats"].get(key) or 0) if sortst["rev"]
                                     else (r["stats"].get(key) or 0)))

            grid = tk.Frame(content, bg=BG)
            grid.pack(fill="both", expand=True, padx=6, pady=(4, 8))
            cols = [("#", None), ("Joueur", None), ("Équipe", None)] + [(lbl, k) for lbl, k, _ in mets]
            for ci, (lbl, k) in enumerate(cols):
                arrow = (" ▾" if sortst["rev"] else " ▴") if (k and k == key) else ""
                h = tk.Label(grid, text=lbl + arrow, bg=HDR, fg=ACCENT if k else MUTED,
                             font=("TkDefaultFont", 8, "bold"),
                             anchor="w" if lbl in ("Joueur", "Équipe") else "center", padx=4)
                h.grid(row=0, column=ci, sticky="we", padx=1, pady=1)
                if k:
                    h.configure(cursor="hand2")
                    h.bind("<Button-1>", lambda _e, kk=k: (sortst.update(
                        key=kk, rev=(not sortst["rev"]) if sortst["key"] == kk else highs[kk]), render()))

            if not rows:
                if cur_comp not in state["domstats"]:
                    msg = "Chargement de la compétition…"
                elif any(t not in state["rosters"] for t in ds):
                    msg = "Chargement des effectifs…"
                else:
                    msg = "Aucun joueur à ce poste."
                tk.Label(content, text=msg, bg=BG, fg=MUTED,
                         font=("TkDefaultFont", 9)).pack(anchor="w", padx=10, pady=6)
            for ri, r in enumerate(rows, start=1):
                rowbg = CARD if ri % 2 else BG

                def cell(ci, text, fg=FG, left=False):
                    lab = tk.Label(grid, text=text, bg=rowbg, fg=fg, font=("TkDefaultFont", 8),
                                   anchor="w" if left else "center", padx=4)
                    lab.grid(row=ri, column=ci, sticky="we", padx=1)
                    return lab
                cells = [cell(0, str(ri), MUTED), cell(1, r["nom"] or "?", FG, left=True),
                         cell(2, r["team"], MUTED, left=True)]
                for j, (_, k, _) in enumerate(mets):
                    cells.append(cell(3 + j, fmt(k, r["stats"].get(k))))
                for w in cells:
                    w.configure(cursor="hand2")
                    w.bind("<Button-1>",
                           lambda _e, c=cur_comp, t=r["team"], p=r["player"]: open_player(c, t, p))
            grid.grid_columnconfigure(1, weight=1)

        def arm_refresh():
            prog["gen"] += 1
            g = prog["gen"]

            def tick(n):
                if not win.winfo_exists() or g != prog["gen"]:
                    return
                cur = comp_v.get()
                ds = state["domstats"].get(cur) or {}
                loaded = sum(1 for t in ds if t in state["rosters"])
                sig = (cur, poste_v.get(), len(ds), loaded)
                if sig != prog["sig"]:
                    prog["sig"] = sig
                    render()
                incomplete = (not ds) or (loaded < len(ds))
                if incomplete and n < 120:
                    win.after(1000, lambda: tick(n + 1))
            win.after(900, lambda: tick(0))

        def reload(*_):          # changement de compétition : recharge + relance le suivi
            prog["sig"] = None
            render()
            arm_refresh()

        comp_box.bind("<<ComboboxSelected>>", reload)
        poste_box.bind("<<ComboboxSelected>>", lambda *_: (sortst.update(key=None), render()))
        render()
        arm_refresh()

    # ---- page "stats & classement" (bouton 📊) ----------------------------
    def _stats_rows():
        """Classement à afficher d'après le dernier rendu (comp courante ou Toutes).

        Le salaire / la célébrité sont lus en direct depuis le cache des effectifs
        au moment du rendu (et se remplissent au fil du préchargement).
        """
        if state["last"] is None:
            return None, None

        def tag(rows, comp_of):
            for r in rows:
                r["comp"] = comp_of(r)
            return rows

        last_comp, payload = state["last"]
        if last_comp == ALL_KEY:
            # Une seule ligne par équipe : TOUS ses matchs, toutes compétitions
            # confondues (championnat + coupes + Europe) — sinon chaque équipe
            # apparaissait une fois par compétition, avec 1-3 matchs côté coupes.
            boards = payload or {}
            all_groups = []
            seen = {}   # équipe -> {compétition: nb d'apparitions} (retrouve son championnat)
            for c, gs in boards.items():
                all_groups += gs
                for g in gs:
                    for m in g["matches"]:
                        for t in (m.get("a"), m.get("b")):
                            if t:
                                seen.setdefault(t, {})
                                seen[t][c] = seen[t].get(c, 0) + 1
            rows = leaderboard(all_groups)
            for r in rows:
                comps = seen.get(r["team"]) or {}
                # le championnat = la compétition où l'équipe a le plus de matchs
                r["comp"] = max(comps, key=comps.get) if comps else None
            return "★ Toutes (champ. + coupes confondus)", rows
        groups, _ = payload
        return last_comp, tag(leaderboard(groups), lambda r: last_comp)

    def open_stats():
        title, rows = _stats_rows()
        if rows is None:
            return                       # rien de chargé encore (avant le 1er rendu)
        prefetch_squads([r["team"] for r in rows])   # salaire/célébrité en arrière-plan
        show_stats_window(title, rows)

    def show_stats_window(title, rows):
        old = state.get("stats_win")
        if old is not None and old.winfo_exists():
            old.destroy()
        win = tk.Toplevel(root)
        state["stats_win"] = win
        win.title(f"📊 Stats — {title}")
        win.configure(bg=BG)
        win.geometry("760x620")
        win.minsize(480, 320)
        try:
            win.attributes("-topmost", bool(topmost_var.get()))
        except tk.TclError:
            pass
        win.lift()

        cv = tk.Canvas(win, bg=BG, highlightthickness=0, bd=0)
        sb = tk.Scrollbar(win, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        box = tk.Frame(cv, bg=BG)
        bid = cv.create_window((0, 0), window=box, anchor="nw")
        box.bind("<Configure>", lambda _e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>", lambda e: cv.itemconfig(bid, width=e.width))

        def _wheel(e):
            cv.yview_scroll(-1 if (e.num == 5 or e.delta < 0) else 1, "units")
            return "break"
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            win.bind(seq, _wheel)

        tk.Label(box, text=f"📊 Classement & stats — {title}", bg=BG, fg=ACCENT,
                 anchor="w", font=("TkDefaultFont", 12, "bold")).pack(fill="x", padx=10, pady=(10, 2))

        tk.Label(box, text="Trie en cliquant un en-tête · clique une équipe pour son "
                           "historique · salaire/célébrité chargés en arrière-plan",
                 bg=BG, fg=MUTED, anchor="w", font=("TkDefaultFont", 8)).pack(fill="x", padx=10, pady=(0, 4))

        content = tk.Frame(box, bg=BG)
        content.pack(fill="both", expand=True)
        cols = [("#", None), ("Équipe", "team"), ("MJ", "played"), ("V-N-D", None),
                ("Pts", "points"), ("BP", "gf"), ("BC", "ga"), ("Diff", "gd"),
                ("Poss", "avg_poss"), ("Occ", "avg_occ"),
                ("Sal.", "avg_salary"), ("Célé.", "avg_celeb")]
        sortst = {"key": "points", "rev": True}

        def on_sort(key):
            if sortst["key"] == key:
                sortst["rev"] = not sortst["rev"]
            else:
                sortst["key"] = key
                sortst["rev"] = (key != "team")   # numérique décroissant, équipe A→Z
            render()

        def render():
            if not content.winfo_exists():
                return
            for w in content.winfo_children():
                w.destroy()
            if not rows:
                tk.Label(content,
                         text="Aucun match joué pour le moment — le classement et "
                              "les stats apparaîtront dès les premiers résultats de "
                              "la saison.",
                         bg=BG, fg=MUTED, anchor="w", justify="left", wraplength=700,
                         font=("TkDefaultFont", 9)).pack(fill="x", padx=12, pady=16)
                return
            # salaire / célébrité en direct depuis le cache (préchargement progressif)
            for r in rows:
                agg = _team_squad_agg(r["team"])
                r["avg_salary"] = agg.get("avg_salary") if agg else None
                r["avg_celeb"] = agg.get("avg_celeb") if agg else None

            played = [r for r in rows if r["played"]]

            def best(metric, biggest=True):
                pool = [r for r in played if r.get(metric) is not None]
                if not pool:
                    return None
                return (max if biggest else min)(pool, key=lambda r: r[metric])

            facts = []
            for label, win_row, render_value in (
                ("🥇 Meilleure attaque", best("gf", True), lambda r: f"{r['team']} ({r['gf']} buts)"),
                ("🛡️ Meilleure défense", best("ga", False), lambda r: f"{r['team']} ({r['ga']} encaissés)"),
                ("⚽ Possession", best("avg_poss", True), lambda r: f"{r['team']} ({r['avg_poss']}%)"),
                ("🎯 Occasions", best("avg_occ", True), lambda r: f"{r['team']} ({r['avg_occ']}/match)"),
                ("💰 Plus gros salaires (moy.)", best("avg_salary", True), lambda r: f"{r['team']} ({r['avg_salary']:.1f} M€)"),
                ("🌟 Plus célèbre (moy.)", best("avg_celeb", True), lambda r: f"{r['team']} ({r['avg_celeb']:.1f})"),
            ):
                if win_row:
                    facts.append((label, render_value(win_row)))
            if facts:
                fc = tk.Frame(content, bg=CARD)
                fc.pack(fill="x", padx=8, pady=(2, 8))
                for label, value in facts:
                    fr = tk.Frame(fc, bg=CARD)
                    fr.pack(fill="x")
                    tk.Label(fr, text=label, bg=CARD, fg=MUTED, anchor="w",
                             font=("TkDefaultFont", 9)).pack(side="left", padx=8, pady=2)
                    tk.Label(fr, text=value, bg=CARD, fg=FG, anchor="e",
                             font=("TkDefaultFont", 9, "bold")).pack(side="right", padx=8)

            grid = tk.Frame(content, bg=BG)
            grid.pack(fill="both", expand=True, padx=6, pady=(0, 8))
            key, rev = sortst["key"], sortst["rev"]

            def sort_key(r):
                if key == "team":
                    return (r.get("team") or "").lower()
                v = r.get(key)
                return v if v is not None else float("-inf")
            ordered = sorted(rows, key=sort_key, reverse=rev)

            for ci, (label, k) in enumerate(cols):
                arrow = (" ▾" if rev else " ▴") if (k and k == key) else ""
                h = tk.Label(grid, text=label + arrow, bg=HDR,
                             fg=ACCENT if k else MUTED, font=("TkDefaultFont", 8, "bold"),
                             anchor="w" if label == "Équipe" else "center", padx=4)
                h.grid(row=0, column=ci, sticky="we", padx=1, pady=1)
                if k:
                    h.configure(cursor="hand2")
                    h.bind("<Button-1>", lambda _e, kk=k: on_sort(kk))

            for ri, r in enumerate(ordered, start=1):
                rowbg = CARD if ri % 2 else BG

                def cell(ci, text, fg=FG, left=False):
                    lbl = tk.Label(grid, text=text, bg=rowbg, fg=fg,
                                   font=("TkDefaultFont", 8),
                                   anchor="w" if left else "center", padx=4)
                    lbl.grid(row=ri, column=ci, sticky="we", padx=1)
                    return lbl

                cell(0, str(ri), MUTED)
                tname = cell(1, r["team"], FG, left=True)
                _bind_team_click(tname, r.get("comp"), r["team"])
                cell(2, str(r["played"]))
                cell(3, f"{r['wins']}-{r['draws']}-{r['losses']}")
                cell(4, str(r["points"]), GREEN)
                cell(5, str(r["gf"]))
                cell(6, str(r["ga"]))
                gd = r["gd"]
                cell(7, f"+{gd}" if gd > 0 else str(gd),
                     GREEN if gd > 0 else (LIVE if gd < 0 else MUTED))
                cell(8, f"{r['avg_poss']}%" if r["avg_poss"] is not None else "—")
                cell(9, f"{r['avg_occ']}" if r["avg_occ"] is not None else "—")
                cell(10, f"{r['avg_salary']:.1f}" if r.get("avg_salary") is not None else "—")
                cell(11, f"{r['avg_celeb']:.1f}" if r.get("avg_celeb") is not None else "—")
            grid.grid_columnconfigure(1, weight=1)

        render()

        # remplissage progressif au fur et à mesure que les effectifs arrivent
        prog = {"resolved": -1, "ticks": 0}

        def refresh():
            if not win.winfo_exists():
                return
            res = sum(1 for r in rows if _team_squad_agg(r["team"]) is not None)
            if res != prog["resolved"]:
                prog["resolved"] = res
                render()
            prog["ticks"] += 1
            if res < len(rows) and prog["ticks"] < 90:
                win.after(1000, refresh)
        win.after(1000, refresh)

    def render_match(parent, comp, m, show_comp=False):
        live = m.get("live")
        bg = CARD_LIVE if live else CARD
        card = tk.Frame(parent, bg=bg)
        card.pack(fill="x", padx=6, pady=2)

        def short(name, n=20):
            name = name or "?"
            return name if len(name) <= n else name[: n - 1] + "…"

        line = tk.Frame(card, bg=bg)
        line.pack(fill="x", padx=8, pady=(5, 1))
        # colonne équipe A (alignée à droite)
        ta = tk.Label(line, text=short(m["a"]), bg=bg, fg=FG, anchor="e",
                      font=("TkDefaultFont", 10))
        ta.grid(row=0, column=0, sticky="e")
        _bind_team_click(ta, comp, m["a"])
        # score / date au centre
        if m["status"] == "result":
            sc_fg = LIVE if live else FG
            sc_font = ("TkDefaultFont", 11, "bold")
            sc_text = m.get("mid") or "—"
        else:
            sc_fg = MUTED
            sc_font = ("TkDefaultFont", 9)
            sc_text = m.get("mid") or "—"
        sc = tk.Label(line, text=f"  {sc_text}  ", bg=bg, fg=sc_fg, font=sc_font)
        sc.grid(row=0, column=1)
        tb = tk.Label(line, text=short(m["b"]), bg=bg, fg=FG, anchor="w",
                      font=("TkDefaultFont", 10))
        tb.grid(row=0, column=2, sticky="w")
        _bind_team_click(tb, comp, m["b"])
        line.grid_columnconfigure(0, weight=1, uniform="x")
        line.grid_columnconfigure(2, weight=1, uniform="x")

        # badges + stats (2e ligne)
        bits = []
        if live:
            bits.append(("🔴 LIVE", LIVE))
        if m.get("imminent_dom") or m.get("imminent_ext"):
            bits.append(("⚡ but imminent", LIVE))
        if show_comp:
            bits.append((comp, ACCENT))
        if m.get("poss"):
            bits.append((f"poss {m['poss']}", MUTED))
        if m.get("occ"):
            bits.append((f"tirs {m['occ']}", MUTED))
        if bits:
            sub = tk.Frame(card, bg=bg)
            sub.pack(fill="x", padx=8, pady=(0, 4))
            for txt, col in bits:
                tk.Label(sub, text=txt, bg=bg, fg=col,
                         font=("TkDefaultFont", 8)).pack(side="left", padx=(0, 8))

        if m.get("changed"):
            _flash(card, sc, [FLASH, bg, FLASH, bg, FLASH, bg])
        return card

    def _flash(card, score_lbl, colors):
        if not colors:
            return
        c = colors[0]
        try:
            card.configure(bg=c)
            for ch in card.winfo_children():
                _recolor(ch, c)
        except tk.TclError:
            return
        root.after(220, lambda: _flash(card, score_lbl, colors[1:]))

    def _recolor(widget, bg):
        try:
            widget.configure(bg=bg)
        except tk.TclError:
            pass
        for ch in widget.winfo_children():
            _recolor(ch, bg)

    def render(comp, payload):
        """payload : pour une compétition -> (groups, standings) ;
                     pour ALL -> {comp: groups}"""
        state["last"] = (comp, payload)
        yfrac = canvas.yview()[0]
        clear_inner()
        live_only = bool(live_only_var.get())

        if comp == ALL_KEY:
            boards = payload  # dict comp -> groups
            rendered = False
            # 1) EN DIRECT : scores qui ont changé entre deux rafraîchissements (fiable)
            live_rows = [(c, m) for c, gs in boards.items()
                         for g in gs for m in g["matches"] if m.get("live")]
            if live_rows:
                rendered = True
                section_header(f"🔴 EN DIRECT ({len(live_rows)})", LIVE)
                for c, m in live_rows:
                    render_match(inner, c, m, show_comp=True)
            # 2) En cours / récents : résultats de la journée ou du tour en cours
            cur_rows = [(c, m) for c, gs in boards.items()
                        for g in gs for m in g["matches"]
                        if m.get("current") and not m.get("live")]
            if cur_rows:
                rendered = True
                section_header(f"🟢 En cours / récents ({len(cur_rows)})", GREEN)
                for c, m in cur_rows:
                    render_match(inner, c, m, show_comp=True)
            # 3) Aujourd'hui : matchs programmés ce jour (masqué en mode live)
            if not live_only:
                today_rows = [(c, m) for c, gs in boards.items()
                              for g in gs for m in g["matches"]
                              if m.get("is_today") and not m.get("live")]
                if today_rows:
                    rendered = True
                    section_header(f"📅 Aujourd'hui ({len(today_rows)})", ACCENT)
                    for c, m in today_rows:
                        render_match(inner, c, m, show_comp=True)
            if not rendered:
                section_header("Rien en direct pour le moment", MUTED)
        else:
            groups, standings = payload
            cur = current_group_index(groups)
            # Section EN DIRECT en haut (changements confirmés)
            live_rows = [m for g in groups for m in g["matches"] if m.get("live")]
            if live_rows:
                section_header(f"🔴 EN DIRECT ({len(live_rows)})", LIVE)
                for m in live_rows:
                    render_match(inner, comp, m)
            # Journées / tours (en mode live, on ne montre que celui en cours)
            for gi, g in enumerate(groups):
                if live_only and gi != cur:
                    continue
                tag = "  ▸ en cours" if gi == cur else ""
                section_header(g["label"] + tag,
                               GREEN if gi == cur else MUTED)
                for m in g["matches"]:
                    render_match(inner, comp, m)
            # Classement (si dispo et pas en mode live-only)
            if standings and not live_only:
                section_header("📊 Classement", ACCENT)
                tbl = tk.Frame(inner, bg=BG)
                tbl.pack(fill="x", padx=6, pady=2)
                heads = ["#", "Équipe", "Pts", "Diff", "Buts"]
                for ci, h in enumerate(heads):
                    tk.Label(tbl, text=h, bg=BG, fg=MUTED,
                             font=("TkDefaultFont", 8, "bold"),
                             anchor="w").grid(row=0, column=ci, sticky="w", padx=4)
                for ri, rowd in enumerate(standings, start=1):
                    vals = [rowd.get("Rang"), rowd.get("Équipe"),
                            rowd.get("Points"), rowd.get("Diff"), rowd.get("Buts")]
                    for ci, val in enumerate(vals):
                        lbl = tk.Label(tbl, text=val, bg=BG, fg=FG,
                                       font=("TkDefaultFont", 8), anchor="w")
                        lbl.grid(row=ri, column=ci, sticky="w", padx=4)
                        if ci == 1:   # colonne "Équipe" -> cliquable
                            _bind_team_click(lbl, comp, val)
                tbl.grid_columnconfigure(1, weight=1)

        inner.update_idletasks()
        canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.yview_moveto(yfrac)

    # ---- thread de polling ------------------------------------------------
    def ui(fn):
        """Exécute fn sur le thread principal, sans planter si la fenêtre est fermée."""
        try:
            root.after(0, fn)
        except (RuntimeError, tk.TclError):
            pass

    def show_whats_new_once():
        build_id = whats_new_build_id()
        if not should_show_whats_new(cfg, build_id):
            return
        notes = load_whats_new()
        cfg["whats_new_seen_build"] = build_id
        save_config()
        if not notes:
            return

        win = tk.Toplevel(root)
        win.title("Nouveautés Foot Live")
        win.configure(bg=BG)
        win.geometry("580x500")
        win.minsize(440, 320)
        win.transient(root)
        try:
            win.attributes("-topmost", bool(topmost_var.get()))
        except tk.TclError:
            pass

        header = tk.Frame(win, bg=HDR)
        header.pack(fill="x")
        tk.Label(header, text="Nouveautés", bg=HDR, fg=FG, anchor="w",
                 font=("TkDefaultFont", 15, "bold")).pack(
                     side="left", padx=14, pady=(12, 2))
        build_label = build_id[:7] if build_id else "build local"
        tk.Label(header, text=build_label, bg=HDR, fg=MUTED,
                 font=("TkDefaultFont", 8)).pack(side="right", padx=14, pady=(14, 2))
        tk.Label(header, text="Cette note apparaît une seule fois après la mise à jour.",
                 bg=HDR, fg=MUTED, anchor="w", font=("TkDefaultFont", 8)).pack(
                     fill="x", padx=14, pady=(0, 12))

        wrap = tk.Frame(win, bg=BG)
        wrap.pack(fill="both", expand=True, padx=12, pady=12)
        scroll = tk.Scrollbar(wrap)
        scroll.pack(side="right", fill="y")
        text = tk.Text(wrap, bg=CARD, fg=FG, insertbackground=FG, relief="flat",
                       bd=0, padx=14, pady=12, wrap="word", yscrollcommand=scroll.set,
                       font=("TkDefaultFont", 10), cursor="arrow")
        text.pack(side="left", fill="both", expand=True)
        scroll.configure(command=text.yview)
        text.tag_configure("h1", foreground=ACCENT, font=("TkDefaultFont", 13, "bold"),
                           spacing1=4, spacing3=8)
        text.tag_configure("h2", foreground=ACCENT, font=("TkDefaultFont", 11, "bold"),
                           spacing1=12, spacing3=5)
        text.tag_configure("bullet", lmargin1=12, lmargin2=28, spacing3=4)
        for line in notes.splitlines():
            if line.startswith("# "):
                continue  # le titre est déjà affiché dans l'en-tête de la fenêtre
            elif line.startswith("## "):
                text.insert("end", line[3:] + "\n", "h2")
            elif line.startswith("- "):
                text.insert("end", "• " + line[2:] + "\n", "bullet")
            else:
                text.insert("end", line + "\n")
        text.configure(state="disabled")

        tk.Button(win, text="Fermer", command=win.destroy, bg=ACCENT, fg="#fff",
                  activebackground=GREEN, activeforeground="#fff", relief="flat",
                  bd=0, padx=18, pady=6, cursor="hand2",
                  font=("TkDefaultFont", 9, "bold")).pack(
                      side="bottom", anchor="e", padx=12, pady=(0, 12))
        win.lift()

    def start_update_check():
        if not auto_update_enabled():
            return

        def work():
            current = current_build_commit()
            if not current:
                return
            try:
                latest, asset_url, asset_size = latest_published_build()
                if _same_commit(current, latest):
                    return
                ui(lambda: status_var.set(
                    f"mise à jour {latest[:7]} en téléchargement…"
                ))
                exe_path = download_update_exe(latest, asset_url, asset_size)
            except Exception:
                return

            def prompt():
                if not root.winfo_exists():
                    return
                ok = messagebox.askyesno(
                    "Mise à jour Foot Live",
                    "Une nouvelle version est disponible depuis la branche main.\n"
                    "Redémarrer maintenant pour l'installer ?",
                    parent=root,
                )
                if ok:
                    state["stop"] = True
                    state["wake"].set()
                    save_config()
                    launch_self_update(exe_path)
                    root.destroy()
                else:
                    status_var.set(f"mise à jour prête ({latest[:7]})")

            ui(prompt)

        threading.Thread(target=work, daemon=True).start()

    def poll_loop():
        while not state["stop"]:
            refresh_current_season()   # recale SEASON (réessaie si l'API était down, suit le rollover)
            gen = state["generation"]
            comp = comp_var.get()
            try:
                n_changed = 0
                if comp == ALL_KEY:
                    names = competitions[1:]
                    boards = {}
                    with ThreadPoolExecutor(max_workers=8) as ex:
                        futs = {ex.submit(fetch_competition, n): n for n in names}
                        for fut in as_completed(futs):
                            n = futs[fut]
                            try:
                                groups, _ = fut.result()
                                n_changed += len(tracker.update(n, groups))
                                boards[n] = groups
                            except Exception:
                                pass
                    payload = boards
                    state["domstats"].clear()   # données rafraîchies -> recalcul à la demande
                    n_live = sum(1 for g in boards.values()
                                 for grp in g for m in grp["matches"] if m.get("live"))
                    summary = f"{len(boards)}/{len(names)} compés · {n_live} live"
                else:
                    groups, standings = fetch_competition(comp)
                    n_changed = len(tracker.update(comp, groups))
                    payload = (groups, standings)
                    state["domstats"].pop(comp, None)   # données rafraîchies -> recalcul à la demande
                    n_live = sum(1 for g in groups for m in g["matches"] if m.get("live"))
                    summary = f"{n_live} live"
                    # précharge les effectifs de cette compé pour la page stats
                    prefetch_squads([t for g in groups for m in g["matches"]
                                     for t in (m.get("a"), m.get("b"))])

                if gen == state["generation"]:
                    ts = time.strftime("%H:%M:%S")
                    ui(lambda p=payload, c=comp, s=summary, t=ts:
                       (render(c, p),
                        status_var.set(f"✓ {t} · {s} · maj/{interval_var.get()}s")))
                    if n_changed and beep_var.get():
                        ui(root.bell)
            except Exception as e:
                if gen == state["generation"]:
                    ui(lambda e=e: status_var.set(f"⚠ hors-ligne : {e} (réessai…)"))

            # attente interruptible
            state["wake"].wait(timeout=max(5, int(interval_var.get())))
            state["wake"].clear()

    # ---- callbacks UI -----------------------------------------------------
    def trigger_refresh(*_):
        status_var.set("rafraîchissement…")
        state["wake"].set()

    def on_comp_change(*_):
        state["generation"] += 1
        clear_inner()
        section_header("chargement…", MUTED)
        trigger_refresh()
        save_config()

    def on_topmost(*_):
        root.attributes("-topmost", bool(topmost_var.get()))
        save_config()

    def on_setting(*_):
        save_config()
        trigger_refresh()

    refresh_btn.configure(command=trigger_refresh)
    comp_box.bind("<<ComboboxSelected>>", on_comp_change)
    interval_box.bind("<<ComboboxSelected>>", lambda *_: save_config())
    live_only_var.trace_add("write", lambda *_: (save_config(), _rerender_cache()))
    topmost_var.trace_add("write", on_topmost)
    beep_var.trace_add("write", lambda *_: save_config())

    # re-render rapide depuis le dernier payload (pour le toggle live-only)
    def _rerender_cache():
        if state["last"] is not None:
            comp, payload = state["last"]
            if comp == comp_var.get():
                render(comp, payload)

    # ---- chargement de la vraie liste de compétitions (en tâche de fond) --
    def load_comp_list():
        try:
            refresh_current_season()
            names = fetch_competitions(SEASON)
            if names:
                vals = [ALL_KEY] + names
                def apply():
                    nonlocal competitions
                    competitions = vals
                    comp_box.configure(values=vals)
                    cfg["competitions"] = names
                    try:
                        write_config_file(cfg)
                    except Exception:
                        pass
                root.after(0, apply)
        except Exception:
            pass

    # ---- chargement des effectifs (salaire/célébrité) en tâche de fond ----
    def load_squads():
        try:
            players = fetch_players()
            if players:
                state["players"] = players
                state["squads"] = squad_stats(players)
        except Exception:
            pass

    def load_history():
        """Charge les saisons terminées depuis l'API /api/all_matchs (remplace les CSV embarqués)."""
        hist = {}
        for key, matches in api_all_matchs().items():
            try:
                n = int(str(key).replace("saison", ""))
            except ValueError:
                continue
            try:
                hist[n] = season_domstats_from_api(matches)
            except Exception:
                pass
        if hist:
            state["history"] = hist

    # ---- démarrage --------------------------------------------------------
    def on_close():
        state["stop"] = True
        state["wake"].set()
        save_config()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    section_header("chargement…", MUTED)
    start_update_check()
    root.after(900, show_whats_new_once)
    threading.Thread(target=load_comp_list, daemon=True).start()
    threading.Thread(target=load_squads, daemon=True).start()
    threading.Thread(target=load_history, daemon=True).start()
    threading.Thread(target=load_player_data, daemon=True).start()
    threading.Thread(target=poll_loop, daemon=True).start()
    root.mainloop()


# ----------------------------------------------------------------------------
if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        run_gui()
