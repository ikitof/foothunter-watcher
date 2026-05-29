#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Foot Live — petite fenêtre "always-on-top" qui suit les scores de
http://foothunter.wiriath.com:6767/resultats/saison2/

- Rafraîchissement automatique (plus besoin de F5)
- Affiche tous les matchs (plus besoin de cliquer pour déplier)
- Détecte les matchs EN DIRECT en comparant les scores entre deux rafraîchissements
  (un score qui change => match live => surligné + clignote + petit "bip" optionnel)
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
import urllib.request
import urllib.parse
from datetime import date
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

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "foot_scores_config.json"
)

SCORE_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")
DATE_RE = re.compile(r"^\s*\d{1,2}/\d{1,2}/\d{2,4}\s*$")
POSS_RE = re.compile(r"^\s*\d+%\s*-\s*\d+%\s*$")

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


def fetch_competition(name):
    """Récupère et parse une compétition. Renvoie (groups, standings)."""
    html = http_get(SAISON_PATH + "/" + urllib.parse.quote(name))
    d = parse_elements(html)
    if d is None:
        raise ValueError("Réponse inattendue (pas de parseElements)")
    return parse_matches(d), parse_standings(d)


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
def selftest():
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
        for gi, g in enumerate(groups):
            tag = "  <== EN COURS" if gi == cur else ""
            print(f"   {g['label']} ({len(g['matches'])}){tag}")
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
    from tkinter import ttk

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
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f)
        except Exception:
            pass

    cfg = load_config()

    # ---- fenêtre ----------------------------------------------------------
    root = tk.Tk()
    root.title("⚽ Foot Live")
    root.configure(bg=BG)
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
                        tk.Label(tbl, text=val, bg=BG, fg=FG,
                                 font=("TkDefaultFont", 8),
                                 anchor="w").grid(row=ri, column=ci, sticky="w", padx=4)
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
                        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                            json.dump(cfg2, f)
                    except Exception:
                        pass
                root.after(0, apply)
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
    threading.Thread(target=load_comp_list, daemon=True).start()
    threading.Thread(target=poll_loop, daemon=True).start()
    root.mainloop()


# ----------------------------------------------------------------------------
if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        run_gui()
