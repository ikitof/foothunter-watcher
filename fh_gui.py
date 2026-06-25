#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Interface graphique desktop (Tkinter) de Foot Live.

Séparée de foot_scores.py, qui reste le cœur logique importable et le point
d'entrée. foot_scores n'importe pas tkinter : ce module n'est chargé que pour
lancer l'app desktop (jamais sur Android, qui a sa propre UI Kivy)."""
import json
import sys
import threading
import time
import urllib.parse
import urllib.request

import foot_scores as _fs
from foot_scores import *  # noqa: F401,F403 - réexporte toute la logique du cœur
from foot_scores import _num, _pair, _same_commit


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

    explorer_btn = tk.Button(bar2, text="🔎 explorer", bg=CARD, fg=FG, bd=0, relief="flat",
                             activebackground=ACCENT, activeforeground="#fff",
                             font=("TkDefaultFont", 8), cursor="hand2",
                             command=lambda: show_explorer_window())
    explorer_btn.pack(side="right", padx=(0, 4))

    palmares_btn = tk.Button(bar2, text="🏆 palmarès", bg=CARD, fg=FG, bd=0, relief="flat",
                             activebackground=ACCENT, activeforeground="#fff",
                             font=("TkDefaultFont", 8), cursor="hand2",
                             command=lambda: show_palmares_window())
    palmares_btn.pack(side="right", padx=(0, 4))

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
                tds_n = cur_ds.get(club) if n == _fs.SEASON else (hist.get(n) or {}).get(club)
                v = tds_n.get(prim_key) if tds_n else None
                vtxt = "—" if v is None else (f"{v}%" if prim_key in PERCENT_STATS else f"{v}")
                row = tk.Frame(seas, bg=CARD)
                row.pack(fill="x", padx=8, pady=1)
                suffix = " (en cours)" if n == _fs.SEASON else ""
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
        squad = state.setdefault("mercato_squad", {})   # poste -> joueur (partagé avec l'explorateur)
        years = state.setdefault("mercato_years", {})   # poste -> nb d'années de contrat (1/2/3)
        cap_v = tk.StringVar(value="250")
        pmin_v = tk.StringVar(value="0")
        pmax_v = tk.StringVar(value="40")

        def _f(var, default):
            try:
                return float(var.get())
            except (TypeError, ValueError):
                return default

        def slots():
            # Une équipe Foothunter = exactement 1 joueur par poste (slot_id = poste).
            return [(poste, poste) for poste in TEAM_POSTES]

        bar = tk.Frame(win, bg=HDR)
        bar.pack(fill="x", side="top")
        tk.Label(bar, text="Budget M€", bg=HDR, fg=MUTED, font=("TkDefaultFont", 8)).pack(side="left", padx=(6, 2))
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

        def _draw_invest(parent, invest):
            h = 8 + 18 * len(DOMAINS)
            chart = tk.Canvas(parent, bg=CARD, height=h, highlightthickness=0, bd=0)
            chart.pack(fill="x", padx=8, pady=4)
            vmax = max([v or 0 for v in invest.values()] + [1.0])

            def redraw(*_):
                chart.delete("all")
                w = chart.winfo_width() or 700
                x0, barw = 120, max(40, w - 200)
                for i, dom in enumerate(DOMAINS):
                    y = 6 + i * 18
                    v = invest.get(dom) or 0
                    chart.create_text(6, y + 6, text=DOMAIN_LABELS[dom], fill=FG, anchor="w",
                                      font=("TkDefaultFont", 8))
                    chart.create_rectangle(x0, y, x0 + barw, y + 11, fill=HDR, outline="")
                    chart.create_rectangle(x0, y, x0 + barw * (v / vmax), y + 11,
                                           fill=ACCENT, outline="")
                    chart.create_text(x0 + barw + 4, y + 6, text=f"{v:g} M€", fill=MUTED, anchor="w",
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
            signings = []
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
                    signings.append((poste, cost))
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
            tk.Label(summ, text=f"  {agg['count']}/{len(TEAM_POSTES)} joueurs · célé moy. "
                                f"{agg['avg_celebrite'] or '—'} · âge moy. {agg['avg_age'] or '—'}",
                     bg=BG, fg=MUTED, anchor="w", font=("TkDefaultFont", 9)).pack(side="left")
            if over:
                tk.Label(content, text="⚠ budget dépassé", bg=BG, fg=LIVE, anchor="w",
                         font=("TkDefaultFont", 8)).pack(fill="x", padx=10)
            tk.Label(content, text="Budget investi par domaine (M€, au prorata du poste)",
                     bg=BG, fg=ACCENT, anchor="w",
                     font=("TkDefaultFont", 9, "bold")).pack(fill="x", padx=10, pady=(6, 0))
            _draw_invest(content, team_domain_investment(signings))

        for v in (cap_v, pmin_v, pmax_v):
            v.trace_add("write", lambda *_: render())
        # Hook pour rafraîchir le mercato quand l'explorateur y ajoute un joueur.
        state["mercato_render"] = lambda: win.winfo_exists() and render()
        win.protocol("WM_DELETE_WINDOW", lambda: (state.pop("mercato_render", None), win.destroy()))
        render()

        if pool["players"] is None:
            def load():
                ps = api_all_joueurs(_fs.SEASON)
                state["mercato_pool"] = ps
                pool["players"] = ps
                if win.winfo_exists():
                    root.after(0, render)
            threading.Thread(target=load, daemon=True).start()

    def show_explorer_window():
        """Explorateur de stats personnalisé : nuage de points joueurs (axes au choix
        parmi salaire/célébrité/âge, filtre par poste) ou classement d'équipes selon
        une stat choisie. Graphes dessinés sur Canvas (zéro dépendance)."""
        old = state.get("explorer_win")
        if old is not None and old.winfo_exists():
            old.destroy()
            return
        win = tk.Toplevel(root)
        state["explorer_win"] = win
        win.title("🔎 Explorateur de stats")
        win.configure(bg=BG)
        win.geometry("760x680")
        win.minsize(560, 460)
        try:
            win.attributes("-topmost", bool(topmost_var.get()))
        except tk.TclError:
            pass
        win.lift()

        TMETRICS = {"Buts / match": "gf_pm", "Encaissés / match": "ga_pm",
                    "Possession %": "poss", "Conversion %": "conv", "Arrêts %": "save",
                    "Clean sheets": "clean", "Occasions / match": "occ_for_pm"}
        real_comps = competitions[1:]

        mode_v = tk.StringVar(value="Joueurs")
        poste_v = tk.StringVar(value="Tous")
        pmin_v = tk.StringVar(value="0")
        pmax_v = tk.StringVar(value="40")
        comp_v = tk.StringVar(value=real_comps[0] if real_comps else "")
        tmetric_v = tk.StringVar(value="Buts / match")
        sortst = {"key": "stat", "rev": True}
        adv_v = tk.StringVar(value="Adv: tous")
        league_vars = {lg: tk.BooleanVar(value=(lg in MAJOR_LEAGUES)) for lg in SCOUT_LEAGUES}
        LEAGUE_ABBR = {"Premier League": "PL", "Liga": "Liga", "Bundesliga": "Bund",
                       "Serie A": "SerieA", "Ligue 1": "L1", "Liga Nos": "Por",
                       "Eredivisie": "Ned", "Süper Lig": "Tur", "Jupiler Pro League": "Bel",
                       "Championship": "Champ", "Liga 2": "Liga2", "Bundesliga 2": "Bund2",
                       "Serie B": "SerieB", "Ligue 2": "L2"}

        def _f(var, d):
            try:
                return float(var.get())
            except (TypeError, ValueError):
                return d

        bar = tk.Frame(win, bg=HDR)
        bar.pack(fill="x", side="top")
        ttk.Combobox(bar, textvariable=mode_v, values=["Joueurs", "Équipes"], state="readonly",
                     width=9).pack(side="left", padx=6, pady=4)
        controls = tk.Frame(win, bg=HDR)
        controls.pack(fill="x", side="top")
        hint = tk.Label(win, text="", bg=BG, fg=MUTED, anchor="w", justify="left",
                        wraplength=720, font=("TkDefaultFont", 8))
        hint.pack(fill="x", padx=8, pady=(2, 0))

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

        def _combo(parent, var, values, w=14):
            c = ttk.Combobox(parent, textvariable=var, values=values, state="readonly", width=w)
            c.pack(side="left", padx=(0, 8), pady=4)
            c.bind("<<ComboboxSelected>>", lambda *_: render())
            return c

        def _entry(parent, var, w=4):
            e = tk.Entry(parent, textvariable=var, width=w, bg=CARD, fg=FG, insertbackground=FG, relief="flat")
            e.pack(side="left", padx=(0, 6), pady=4)
            e.bind("<KeyRelease>", lambda *_: render())
            return e

        def render_players():
            row1 = tk.Frame(controls, bg=HDR)
            row1.pack(fill="x")
            tk.Label(row1, text="Poste", bg=HDR, fg=MUTED, font=("TkDefaultFont", 8)).pack(side="left", padx=(6, 2))
            _combo(row1, poste_v, list(POSTE_DOMAIN_WEIGHTS), 6)
            tk.Label(row1, text="Prix", bg=HDR, fg=MUTED, font=("TkDefaultFont", 8)).pack(side="left", padx=(0, 2))
            _entry(row1, pmin_v)
            tk.Label(row1, text="à", bg=HDR, fg=MUTED, font=("TkDefaultFont", 8)).pack(side="left")
            _entry(row1, pmax_v)
            tk.Label(row1, text="Adv.", bg=HDR, fg=MUTED, font=("TkDefaultFont", 8)).pack(side="left", padx=(6, 2))
            _combo(row1, adv_v, ["Adv: tous", "Adv: décisifs"], 12)
            row2 = tk.Frame(controls, bg=HDR)
            row2.pack(fill="x")
            tk.Label(row2, text="Ligues", bg=HDR, fg=MUTED, font=("TkDefaultFont", 8)).grid(row=0, column=0, sticky="w", padx=(6, 4))
            for i, lg in enumerate(SCOUT_LEAGUES):
                tk.Checkbutton(row2, text=LEAGUE_ABBR.get(lg, lg), variable=league_vars[lg],
                               command=render, bg=HDR, fg=FG, selectcolor=CARD, activebackground=HDR,
                               activeforeground=FG, bd=0, highlightthickness=0, cursor="hand2",
                               font=("TkDefaultFont", 8)).grid(row=1 + i // 7, column=i % 7, sticky="w", padx=2)

            poste = poste_v.get()
            if poste not in ROLE_RELEVANCE:
                poste = "GAR"
                poste_v.set(poste)
            leagues = [lg for lg in SCOUT_LEAGUES if league_vars[lg].get()] or list(MAJOR_LEAGUES)
            pool = state.get("mercato_pool") or []
            if not pool:
                tk.Label(content, text="Chargement des joueurs…", bg=BG, fg=MUTED,
                         font=("TkDefaultFont", 9)).pack(anchor="w", padx=10, pady=8)
                return
            cache = state.setdefault("scout_cache", {})
            missing = [lg for lg in leagues if (lg, poste) not in cache]
            if missing:
                tk.Label(content, text=f"Analyse de {len(missing)} ligue(s)…", bg=BG, fg=MUTED,
                         font=("TkDefaultFont", 9)).pack(anchor="w", padx=10, pady=8)

                def load(ls=tuple(missing), pp=poste):
                    for lg in ls:
                        try:
                            cache[(lg, pp)] = role_scout_rows(lg, pp, pool)
                        except Exception:
                            cache[(lg, pp)] = []
                    if win.winfo_exists():
                        root.after(0, render)
                threading.Thread(target=load, daemon=True).start()
                return
            seen, agg = set(), []
            for lg in leagues:
                for r in cache[(lg, poste)]:
                    k = (r["nom"], r["team"])
                    if k not in seen:
                        seen.add(k)
                        agg.append(r)
            stat_key, stat_label, counters = ROLE_RELEVANCE[poste]
            adv_key = "opp_dec" if adv_v.get() == "Adv: décisifs" else "opp"
            adv_lbl = "Adv✓" if adv_key == "opp_dec" else "Adv."
            lo, hi = _f(pmin_v, 0), _f(pmax_v, 1e9)
            rows = [dict(r) for r in agg if r["salaire"] is not None and lo <= r["salaire"] <= hi]
            cols = [("#", None), ("Joueur", "nom"), ("Équipe", "team"), ("Ligue", "competition"),
                    (stat_label, "stat"), (adv_lbl, adv_key), ("Célé", "celebrite"), ("Sal", "salaire")]
            valid = [c[1] for c in cols if c[1]]
            if sortst["key"] in ("opp", "opp_dec") and sortst["key"] != adv_key:
                sortst["key"] = adv_key
            if sortst["key"] not in valid:
                sortst["key"], sortst["rev"] = "stat", True
            k = sortst["key"]
            if k in ("nom", "team", "competition"):
                rows.sort(key=lambda r: (r.get(k) or "").lower(), reverse=not sortst["rev"])
            else:
                rows.sort(key=lambda r: (r.get(k) is None,
                                         -(r.get(k) or 0) if sortst["rev"] else (r.get(k) or 0)))
            top_dom = max(POSTE_DOMAIN_WEIGHTS[poste].items(), key=lambda kv: kv[1])[0]
            advmode = "matchs décisifs" if adv_key == "opp_dec" else "tous les matchs"
            hint.config(text=f"{len(rows)} {poste} sur {len(leagues)} ligue(s) · stat clé : {stat_label} "
                             f"({DOMAIN_LABELS[top_dom]}) · Adv. = célé {'/'.join(counters)} adverses ({advmode}) · "
                             "« + » ajoute au mercato")
            grid = tk.Frame(content, bg=BG)
            grid.pack(fill="both", expand=True, padx=6, pady=4)
            for ci, (lbl, key) in enumerate(cols):
                arrow = (" ▾" if sortst["rev"] else " ▴") if key == sortst["key"] else ""
                hh = tk.Label(grid, text=lbl + arrow, bg=HDR, fg=ACCENT if key else MUTED,
                              font=("TkDefaultFont", 8, "bold"),
                              anchor="w" if lbl in ("Joueur", "Équipe", "Ligue") else "center", padx=3)
                hh.grid(row=0, column=ci, sticky="we", padx=1, pady=1)
                if key:
                    hh.configure(cursor="hand2")
                    hh.bind("<Button-1>", lambda _e, kk=key: (sortst.update(
                        key=kk, rev=(not sortst["rev"]) if sortst["key"] == kk else (kk not in ("nom", "team", "competition"))),
                        render()))
            tk.Label(grid, text="", bg=HDR).grid(row=0, column=len(cols), padx=1, pady=1)
            for ri, r in enumerate(rows[:300], start=1):
                rowbg = CARD if ri % 2 else BG

                def cell(ci, text, fg=FG, left=False):
                    tk.Label(grid, text=text, bg=rowbg, fg=fg, font=("TkDefaultFont", 8),
                             anchor="w" if left else "center", padx=3).grid(row=ri, column=ci, sticky="we", padx=1)
                cell(0, str(ri), MUTED)
                cell(1, r["nom"] or "?", FG, True)
                cell(2, r["team"] or "?", MUTED, True)
                cell(3, LEAGUE_ABBR.get(r.get("competition"), r.get("competition") or "?"), MUTED, True)
                cell(4, f"{r['stat']:g}" if r["stat"] is not None else "—", ACCENT)
                cell(5, f"{r[adv_key]:g}" if r.get(adv_key) is not None else "—")
                cell(6, f"{r['celebrite']:g}" if r["celebrite"] is not None else "—")
                cell(7, f"{r['salaire']:g}" if r["salaire"] is not None else "—")

                def add_merc(rr=r, pp=poste):
                    state.setdefault("mercato_squad", {})[pp] = {
                        "nom": rr["nom"], "poste": pp, "nom_equipe": rr["team"],
                        "salaire": rr["salaire"], "celebrite": rr["celebrite"], "age": rr["age"]}
                    state.setdefault("mercato_years", {}).setdefault(pp, 1)
                    hook = state.get("mercato_render")
                    if hook:
                        hook()
                tk.Button(grid, text="+ merc", bg=rowbg, fg=GREEN, bd=0, relief="flat", cursor="hand2",
                          font=("TkDefaultFont", 8), command=add_merc).grid(row=ri, column=len(cols), sticky="we", padx=1)
            grid.grid_columnconfigure(1, weight=1)

        def render_teams():
            tk.Label(controls, text="Compétition", bg=HDR, fg=MUTED, font=("TkDefaultFont", 8)).pack(side="left", padx=(6, 2))
            _combo(controls, comp_v, real_comps, 16)
            tk.Label(controls, text="Stat", bg=HDR, fg=MUTED, font=("TkDefaultFont", 8)).pack(side="left", padx=(0, 2))
            _combo(controls, tmetric_v, list(TMETRICS), 16)
            comp = comp_v.get()
            ensure_domstats(comp, on_ready=lambda: win.winfo_exists() and render())
            ds = state["domstats"].get(comp) or {}
            hint.config(text=f"Classement des équipes de {comp} par {tmetric_v.get()}")
            if not ds:
                tk.Label(content, text="Chargement de la compétition…", bg=BG, fg=MUTED,
                         font=("TkDefaultFont", 9)).pack(anchor="w", padx=10, pady=8)
                return
            key = TMETRICS[tmetric_v.get()]
            rows = sorted(((t, s.get(key)) for t, s in ds.items() if s.get(key) is not None),
                          key=lambda r: -r[1])
            vmax = max((v for _, v in rows), default=1) or 1
            box = tk.Frame(content, bg=BG)
            box.pack(fill="x", padx=8, pady=4)
            for t, v in rows:
                row = tk.Frame(box, bg=BG)
                row.pack(fill="x", pady=1)
                tk.Label(row, text=t[:22], bg=BG, fg=FG, width=20, anchor="w",
                         font=("TkDefaultFont", 8)).pack(side="left")
                tk.Label(row, text=f"{v:g}", bg=BG, fg=MUTED, width=6, anchor="e",
                         font=("TkDefaultFont", 8)).pack(side="right")
                track = tk.Frame(row, bg=HDR, height=14)
                track.pack(side="left", fill="x", expand=True, padx=4)
                track.pack_propagate(False)
                tk.Frame(track, bg=ACCENT).place(relx=0, rely=0, relwidth=v / vmax, relheight=1)

        def render(*_):
            for w in controls.winfo_children():
                w.destroy()
            for w in content.winfo_children():
                w.destroy()
            if mode_v.get() == "Joueurs":
                render_players()
            else:
                render_teams()

        mode_v.trace_add("write", lambda *_: render())
        render()

        if not state.get("mercato_pool"):
            def load():
                ps = api_all_joueurs(_fs.SEASON)
                state["mercato_pool"] = ps
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

    def show_palmares_window():
        old = state.get("palmares_win")
        if old is not None and old.winfo_exists():
            old.lift()
            return
        win = tk.Toplevel(root)
        state["palmares_win"] = win
        win.title("🏆 Palmarès — records")
        win.configure(bg=BG)
        win.geometry("720x680")
        win.minsize(460, 360)
        try:
            win.attributes("-topmost", bool(topmost_var.get()))
        except tk.TclError:
            pass
        win.lift()
        win.protocol("WM_DELETE_WINDOW", lambda: (state.pop("palmares_win", None), win.destroy()))

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

        def render():
            if not box.winfo_exists():
                return
            for w in box.winfo_children():
                w.destroy()
            tk.Label(box, text="🏆 Palmarès", bg=BG, fg=ACCENT, anchor="w",
                     font=("TkDefaultFont", 13, "bold")).pack(fill="x", padx=12, pady=(10, 0))
            data = state.get("palmares")
            if not data:
                tk.Label(box, text="Calcul des records sur tous les matchs joués…",
                         bg=BG, fg=MUTED, anchor="w", font=("TkDefaultFont", 9)).pack(fill="x", padx=12, pady=12)
                return
            records, nmatch = data
            tk.Label(box, text=f"Sur {nmatch} matchs joués, toutes saisons confondues — 100% factuel",
                     bg=BG, fg=MUTED, anchor="w", font=("TkDefaultFont", 8)).pack(fill="x", padx=12, pady=(0, 6))
            medals = ["🥇", "🥈", "🥉"]
            for key, title, sub in _fs.PALMARES_CATEGORIES:
                items = records.get(key) or []
                if not items:
                    continue
                card = tk.Frame(box, bg=CARD)
                card.pack(fill="x", padx=10, pady=4)
                tk.Label(card, text=title, bg=CARD, fg=FG, anchor="w",
                         font=("TkDefaultFont", 10, "bold")).pack(fill="x", padx=10, pady=(6, 0))
                tk.Label(card, text=sub, bg=CARD, fg=MUTED, anchor="w",
                         font=("TkDefaultFont", 8)).pack(fill="x", padx=10, pady=(0, 2))
                for i, it in enumerate(items):
                    row = tk.Frame(card, bg=CARD)
                    row.pack(fill="x", padx=10, pady=1)
                    tk.Label(row, text=medals[i] if i < 3 else f"{i + 1}.", bg=CARD, fg=FG,
                             width=3, font=("TkDefaultFont", 10)).pack(side="left")
                    tk.Label(row, text=it["head"], bg=CARD, fg=ACCENT, width=12, anchor="w",
                             font=("TkDefaultFont", 9, "bold")).pack(side="left")
                    tk.Label(row, text=it["ctx"], bg=CARD, fg=MUTED, anchor="e",
                             font=("TkDefaultFont", 7)).pack(side="right")
                    tk.Label(row, text=it["desc"], bg=CARD, fg=FG, anchor="w",
                             font=("TkDefaultFont", 9)).pack(side="left", fill="x", expand=True)
                tk.Frame(card, bg=CARD, height=4).pack()

        render()
        if not state.get("palmares"):
            def work():
                try:
                    data = _fs.palmares_data(top=3)
                except Exception:
                    data = ({}, 0)
                state["palmares"] = data
                if win.winfo_exists():
                    root.after(0, render)
            threading.Thread(target=work, daemon=True).start()

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
            refresh_current_season()   # recale _fs.SEASON (réessaie si l'API était down, suit le rollover)
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
            names = fetch_competitions(_fs.SEASON)
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
