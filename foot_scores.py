#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Foot Live — petite fenêtre "always-on-top" qui suit les scores de
http://foothunter.wiriath.com:6767/resultats/saison2/

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
import sys
import re
import json
import time
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
SAISON_PATH = "/resultats/saison2"
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
    for asset in data.get("assets") or []:
        if asset.get("name") == UPDATE_ASSET_NAME:
            asset_url = asset.get("browser_download_url") or ""
            break
    if not commit or not asset_url:
        raise ValueError("build Windows GitHub introuvable")
    return commit, asset_url


def _same_commit(a, b):
    return bool(a and b and (a == b or a.startswith(b) or b.startswith(a)))


def download_update_exe(commit, url):
    """Télécharge le build Windows publié par la release roulante `main-latest`."""
    suffix = commit[:12] if commit else str(int(time.time()))
    target = os.path.join(tempfile.gettempdir(), f"FootLive-{suffix}.exe")
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Cache-Control": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=UPDATE_TIMEOUT) as r, open(target, "wb") as f:
        while True:
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
    if os.path.getsize(target) < 100 * 1024:
        raise ValueError("exécutable téléchargé trop petit")
    with open(target, "rb") as f:
        if f.read(2) != b"MZ":
            raise ValueError("fichier téléchargé invalide")
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


def parse_competitions(menu_html):
    """Liste (nom, chemin) des compétitions depuis la page menu saison2."""
    d = parse_elements(menu_html)
    out = []
    seen = set()
    if not d:
        return out
    for v in d.values():
        if v.get("tag") == "nicegui-link":
            href = (v.get("props") or {}).get("href", "")
            name = v.get("text")
            if href.startswith(SAISON_PATH + "/") and name and name not in seen:
                seen.add(name)
                out.append(name)
    return out


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


def fetch_competition(name):
    """Récupère et parse une compétition. Renvoie (groups, standings)."""
    html = http_get(SAISON_PATH + "/" + urllib.parse.quote(name))
    d = parse_elements(html)
    if d is None:
        raise ValueError("Réponse inattendue (pas de parseElements)")
    return parse_matches(d), parse_standings(d)


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
            if m.get("status") == "result":
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
            if m.get("status") != "result":
                continue
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


def selftest():
    print("→ Tests hors-ligne…")
    selftest_offline()
    print("→ Récupération du menu…")
    comps = parse_competitions(http_get(SAISON_PATH))
    print(f"  {len(comps)} compétitions : {', '.join(comps[:6])} …")
    assert comps, "aucune compétition trouvée"

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
        cfg = {
            "competition": comp_var.get(),
            "interval": interval_var.get(),
            "live_only": bool(live_only_var.get()),
            "topmost": bool(topmost_var.get()),
            "beep": bool(beep_var.get()),
            "geometry": root.geometry(),
        }
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
        return groups

    # ---- effectifs : cache + préchargement en tâche de fond ---------------
    def _team_squad_agg(team):
        """Stats d'effectif d'une équipe : table globale, sinon effectif en cache."""
        g = (state.get("squads") or {}).get(team)
        if g:
            return g
        rows = state["rosters"].get(team)
        return squad_stats(rows).get(team) if rows else None

    def ensure_team_cached(team):
        """Récupère et met en cache l'effectif d'une équipe absente de la table globale."""
        if not team or team in state["rosters"] or team in (state.get("squads") or {}):
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
        """Précharge (en arrière-plan) les effectifs manquants pour `teams`."""
        squads = state.get("squads") or {}
        todo = [t for t in dict.fromkeys(teams)
                if t and t not in squads and t not in state["rosters"]]
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
                tk.Label(pr, text=f"{(p.get('poste') or '').ljust(4)} {p.get('nom') or '?'}",
                         bg=CARD, fg=FG, anchor="w", font=("TkDefaultFont", 8)).pack(side="left")
                det = []
                if p.get("celebrite") is not None:
                    det.append(f"célé {p['celebrite']}")
                if p.get("salaire") is not None:
                    det.append(f"sal {p['salaire']}M€")
                if p.get("age") is not None:
                    det.append(f"{p['age']}a")
                tk.Label(pr, text="   ".join(det), bg=CARD, fg=MUTED, anchor="e",
                         font=("TkDefaultFont", 8)).pack(side="right")

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
            rows = []
            for c, gs in (payload or {}).items():
                rows += tag(leaderboard(gs), lambda r, c=c: c)
            rows.sort(key=lambda t: (t["points"], t["gd"], t["gf"]), reverse=True)
            return "★ Toutes", rows
        groups, _ = payload
        return last_comp, tag(leaderboard(groups), lambda r: last_comp)

    def open_stats():
        title, rows = _stats_rows()
        if not rows:
            return
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
            sc_text = m["mid"]
        else:
            sc_fg = MUTED
            sc_font = ("TkDefaultFont", 9)
            sc_text = m["mid"]
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

    def start_update_check():
        if not auto_update_enabled():
            return

        def work():
            current = current_build_commit()
            if not current:
                return
            try:
                latest, asset_url = latest_published_build()
                if _same_commit(current, latest):
                    return
                ui(lambda: status_var.set(
                    f"mise à jour {latest[:7]} en téléchargement…"
                ))
                exe_path = download_update_exe(latest, asset_url)
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
                    n_live = sum(1 for g in boards.values()
                                 for grp in g for m in grp["matches"] if m.get("live"))
                    summary = f"{len(boards)}/{len(names)} compés · {n_live} live"
                else:
                    groups, standings = fetch_competition(comp)
                    n_changed = len(tracker.update(comp, groups))
                    payload = (groups, standings)
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
            names = parse_competitions(http_get(SAISON_PATH))
            if names:
                vals = [ALL_KEY] + names
                def apply():
                    nonlocal competitions
                    competitions = vals
                    comp_box.configure(values=vals)
                    cfg2 = load_config()
                    cfg2["competitions"] = names
                    try:
                        write_config_file(cfg2)
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

    # ---- démarrage --------------------------------------------------------
    def on_close():
        state["stop"] = True
        state["wake"].set()
        save_config()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    section_header("chargement…", MUTED)
    start_update_check()
    threading.Thread(target=load_comp_list, daemon=True).start()
    threading.Thread(target=load_squads, daemon=True).start()
    threading.Thread(target=poll_loop, daemon=True).start()
    root.mainloop()


# ----------------------------------------------------------------------------
if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        run_gui()
