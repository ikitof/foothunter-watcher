#!/usr/bin/env python3
"""Interface Android Kivy pour Foot Live."""

import json
import os
import threading
import time
import urllib.parse
import webbrowser

from kivy.app import App
from kivy.animation import Animation
from kivy.clock import Clock
from kivy.core.audio import SoundLoader
from kivy.core.window import Window
from kivy.graphics import Color, Rectangle, RoundedRectangle
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.togglebutton import ToggleButton
from kivy.uix.widget import Widget

import foot_scores as core

try:
    import certifi
except ImportError:
    certifi = None

if certifi:
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())

APP_VERSION = "0.1.0"
APK_ASSET_NAME = "FootLive.apk"
APK_COMMIT_ASSET_NAME = "android-commit.txt"
LEAGUE_ABBR = {"Premier League": "PL", "Liga": "Liga", "Bundesliga": "Bund", "Serie A": "SerieA",
               "Ligue 1": "L1", "Liga Nos": "Por", "Eredivisie": "Ned", "Süper Lig": "Tur",
               "Jupiler Pro League": "Bel", "Championship": "Champ", "Liga 2": "Liga2",
               "Bundesliga 2": "Bund2", "Serie B": "SerieB", "Ligue 2": "L2"}

BG = (0.055, 0.067, 0.09, 1)
CARD = (0.10, 0.115, 0.145, 1)
CARD_ALT = (0.075, 0.085, 0.11, 1)
HEADER = (0.07, 0.08, 0.105, 1)
FG = (0.90, 0.92, 0.96, 1)
MUTED = (0.55, 0.58, 0.66, 1)
ACCENT = (0.35, 0.62, 0.88, 1)
GREEN = (0.38, 0.86, 0.42, 1)
RED = (1.0, 0.32, 0.32, 1)


class Surface(BoxLayout):
    def __init__(self, color=CARD, radius=dp(6), **kwargs):
        super().__init__(**kwargs)
        with self.canvas.before:
            self._color = Color(*color)
            self._rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[radius])
        self.bind(pos=self._sync_rect, size=self._sync_rect)

    def _sync_rect(self, *_):
        self._rect.pos = self.pos
        self._rect.size = self.size


def label(text="", color=FG, size=14, bold=False, height=None, halign="left"):
    markup = f"[b]{text}[/b]" if bold else text
    widget = Label(
        text=markup,
        markup=True,
        color=color,
        font_size=dp(size),
        halign=halign,
        valign="middle",
        size_hint_y=None,
        height=dp(height or 34),
        shorten=True,
        shorten_from="right",
    )
    widget.bind(size=lambda instance, _value: setattr(
        instance, "text_size", (instance.width - dp(8), None)
    ))
    return widget


def action(text, callback, color=ACCENT, width=None):
    button = Button(
        text=text,
        color=FG,
        background_normal="",
        background_down="",
        background_color=color,
        font_size=dp(13),
        bold=True,
        size_hint_y=None,
        height=dp(44),
    )
    if width:
        button.size_hint_x = None
        button.width = dp(width)
    button.bind(on_release=callback)
    return button


def spacer(height=8):
    return Widget(size_hint_y=None, height=dp(height))


def bar_row(text, value, vmax=100, color=ACCENT):
    """Ligne 'libellé | barre proportionnelle | valeur' (barre via size_hint_x, sans
    canvas — fiable et lisible sur mobile)."""
    frac = max(0.0, min(1.0, (value or 0) / vmax)) if vmax else 0.0
    row = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(26), spacing=dp(4))
    lab = label(text, color=FG, size=11, height=26)
    lab.size_hint_x = 0.42
    row.add_widget(lab)
    track = BoxLayout(orientation="horizontal")
    fill = Surface(color=color, radius=dp(3))
    fill.size_hint_x = max(0.001, frac)
    track.add_widget(fill)
    if frac < 1:
        track.add_widget(Widget(size_hint_x=1 - frac))
    row.add_widget(track)
    val = label(f"{value:g}" if value is not None else "—", color=MUTED, size=11,
                halign="right", height=26)
    val.size_hint_x = None
    val.width = dp(42)
    row.add_widget(val)
    return row


class FootLiveMobileApp(App):
    title = "Foot Live"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.current_tab = "scores"
        self.current_comp = "Premier League"
        self.competitions = list(core.DEFAULT_COMPETITIONS)
        self.groups = None
        self.standings = None
        self.player_data = None
        self.loading = False
        self.refresh_event = None
        self.config_data = {}
        self.update_url = ""
        self.last_update_ts = None
        self.last_error = None
        self.match_scores = {}   # clé match -> total de buts, pour détecter un but
        self.goal_sound = None
        # Mercato / explorateur de stats
        self.mercato_pool = None      # liste joueurs (api_all_joueurs), chargée à la demande
        self.mercato_squad = {}       # poste -> joueur (1 joueur par poste)
        self.mercato_years = {}       # poste -> années de contrat
        self.mercato_cap = "250"
        self.mercato_pmax = "40"
        self.explore_mode = "Joueurs"
        self.explore_poste = "Tous"
        self.explore_pmax = "40"
        self.explore_comp = ""
        self.explore_leagues = list(core.MAJOR_LEAGUES)   # ligues scoutées (multi-sélection)
        self.explore_adv = "Adv: tous"                    # adversité : tous les matchs / décisifs
        self.explore_metric = "Buts / match"
        self.palmares = None                              # records (calculés à la 1re ouverture)

    @property
    def config_path(self):
        return os.path.join(self.user_data_dir, "mobile_config.json")

    @property
    def player_cache_path(self):
        return os.path.join(self.user_data_dir, core.PLAYER_DATA_NAME)

    def load_config(self):
        try:
            with open(self.config_path, encoding="utf-8") as stream:
                self.config_data = json.load(stream)
        except Exception:
            self.config_data = {}
        self.current_comp = self.config_data.get("competition", self.current_comp)

    def save_config(self):
        self.config_data["competition"] = self.current_comp
        os.makedirs(self.user_data_dir, exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as stream:
            json.dump(self.config_data, stream)

    def build(self):
        Window.clearcolor = BG
        self.load_config()
        # Hauteur des barres système Android (0 ailleurs) : on étend l'en-tête et le
        # pied dessous pour que le contenu ne passe pas sous elles. Avec android.api
        # = 35, Android impose l'edge-to-edge, sinon les barres recouvrent l'app.
        self._inset_top = self._android_inset("status_bar_height")
        self._inset_bottom = self._android_inset("navigation_bar_height")

        self.root_layout = BoxLayout(orientation="vertical", spacing=0)
        self.root_layout.add_widget(self._build_header())
        self.root_layout.add_widget(self._build_tabs())

        self.scroll = ScrollView(do_scroll_x=False, bar_width=dp(3))
        # Fond opaque derrière la zone scrollable : sans lui, les labels (fond
        # transparent) laissent des traînées (« rayures ») au défilement sur Android.
        with self.scroll.canvas.before:
            Color(*BG)
            self._scroll_bg = Rectangle(pos=self.scroll.pos, size=self.scroll.size)
        self.scroll.bind(pos=self._sync_scroll_bg, size=self._sync_scroll_bg)

        self.content = BoxLayout(
            orientation="vertical",
            spacing=dp(8),
            padding=(dp(10), dp(10), dp(10), dp(24)),
            size_hint_y=None,
        )
        self.content.bind(minimum_height=self.content.setter("height"))
        self.scroll.add_widget(self.content)
        self.root_layout.add_widget(self.scroll)

        self.status = label("Chargement...", color=MUTED, size=11, height=28)
        footer = Surface(
            orientation="horizontal",
            color=HEADER,
            size_hint_y=None,
            height=dp(30) + self._inset_bottom,
            padding=(dp(10), 0, dp(10), self._inset_bottom),
        )
        footer.add_widget(self.status)
        self.root_layout.add_widget(footer)

        Clock.schedule_once(lambda _dt: self.startup(), 0.2)
        self._apply_refresh_interval()
        # Rafraîchit le « Actualisé il y a … » du pied sans refaire d'appel réseau.
        Clock.schedule_interval(self._render_status, 5)
        return self.root_layout

    def _refresh_secs(self):
        """Intervalle d'actualisation auto choisi dans les réglages (10-300 s)."""
        try:
            return max(10, min(300, int(self.config_data.get("refresh_secs", 30))))
        except (TypeError, ValueError):
            return 30

    def _apply_refresh_interval(self):
        if self.refresh_event:
            self.refresh_event.cancel()
        self.refresh_event = Clock.schedule_interval(
            lambda _dt: self.refresh(), self._refresh_secs())

    def _android_inset(self, name):
        """Hauteur (px) d'une barre système Android via ses ressources ; 0 hors Android."""
        try:
            from jnius import autoclass
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            res = activity.getResources()
            rid = res.getIdentifier(name, "dimen", "android")
            return res.getDimensionPixelSize(rid) if rid > 0 else 0
        except Exception:
            return 0

    def _sync_scroll_bg(self, *_):
        self._scroll_bg.pos = self.scroll.pos
        self._scroll_bg.size = self.scroll.size

    def _age_str(self, secs):
        secs = int(secs)
        if secs < 5:
            return "à l'instant"
        if secs < 60:
            return f"il y a {secs} s"
        mins = secs // 60
        if mins < 60:
            return f"il y a {mins} min"
        return f"il y a {mins // 60} h {mins % 60:02d}"

    def _render_status(self, *_):
        """Affiche l'état du pied : en cours, hors ligne, ou fraîcheur des données."""
        if self.loading:
            self.set_status("Actualisation...")
        elif self.last_error and not self.last_update_ts:
            self.set_status(f"Hors ligne : {self.last_error}")
        elif self.last_error:
            self.set_status(
                f"Hors ligne · dernière maj {self._age_str(time.time() - self.last_update_ts)}")
        elif self.last_update_ts:
            self.set_status(f"Actualisé {self._age_str(time.time() - self.last_update_ts)}")
        else:
            self.set_status("Chargement...")

    def _detect_goals(self, groups):
        """True si un score a augmenté depuis le dernier rafraîchissement (= but).

        On mémorise le total de buts par match (compétition + journée + équipes).
        Le premier passage — ou un changement de compétition — sert de référence
        et ne déclenche rien ; ensuite toute hausse du total signale un but.
        """
        fired = False
        seen = {}
        for group in groups or []:
            for match in group.get("matches", []):
                a, b = match.get("a"), match.get("b")
                score = core._pair(match.get("mid"))
                if not (a and b and score):
                    continue
                key = (self.current_comp, group.get("label"), a, b)
                total = score[0] + score[1]
                seen[key] = total
                previous = self.match_scores.get(key)
                if previous is not None and total > previous:
                    fired = True
        self.match_scores = seen
        return fired

    def _goal_alert(self):
        """BUT ! Klaxon + flash plein écran + vibration."""
        self._play_buzzer()
        self._vibrate()
        self._flash()

    def _play_buzzer(self):
        sound = self.goal_sound
        if not sound:
            return
        try:
            sound.stop()        # relance depuis le début si déjà en cours
            sound.volume = 1.0
            sound.play()
        except Exception:
            pass

    def _vibrate(self):
        try:
            from jnius import autoclass
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            Context = autoclass("android.content.Context")
            vib = activity.getSystemService(Context.VIBRATOR_SERVICE)
            if not vib:
                return
            try:    # API 26+ : motif klaxon (buzz / pause / buzz)
                VibrationEffect = autoclass("android.os.VibrationEffect")
                vib.vibrate(VibrationEffect.createWaveform([0, 500, 150, 500], -1))
            except Exception:
                vib.vibrate(900)        # API < 26
        except Exception:
            pass

    def _flash(self):
        overlay = Widget(opacity=0)
        with overlay.canvas:
            Color(*GREEN)
            Rectangle(pos=(0, 0), size=Window.size)
        Window.add_widget(overlay)

        def pulse():    # instances neuves : réutiliser la même corromprait l'état
            return Animation(opacity=0.85, d=0.10) + Animation(opacity=0.0, d=0.16)
        anim = pulse() + pulse() + pulse()
        anim.bind(on_complete=lambda *_: Window.remove_widget(overlay))
        anim.start(overlay)

    def _build_header(self):
        header = Surface(
            orientation="horizontal",
            color=HEADER,
            size_hint_y=None,
            height=dp(64) + self._inset_top,
            padding=(dp(12), dp(8) + self._inset_top, dp(12), dp(8)),
            spacing=dp(8),
        )
        title_box = BoxLayout(orientation="vertical")
        title_box.add_widget(label("FOOT LIVE", color=FG, size=19, bold=True, height=28))
        title_box.add_widget(label("Scores et tendances", color=MUTED, size=11, height=22))
        header.add_widget(title_box)
        self.update_button = action("MISE A JOUR", lambda *_: self.open_update(), GREEN, 116)
        self.update_button.opacity = 0
        self.update_button.disabled = True
        self.update_button.width = 0   # masqué = n'occupe pas de place tant qu'absent
        header.add_widget(self.update_button)
        header.add_widget(action("Réglages", lambda *_: self.open_settings(), CARD_ALT, 96))
        header.add_widget(action("ACTUALISER", lambda *_: self.refresh(force=True), ACCENT, 104))
        return header

    def _build_tabs(self):
        tabs = Surface(
            orientation="horizontal",
            color=HEADER,
            size_hint_y=None,
            height=dp(50),
            padding=(dp(8), dp(3)),
            spacing=dp(5),
        )
        self.tab_buttons = {}
        for key, text in (
            ("scores", "SCORES"),
            ("standing", "CLASS."),
            ("evolution", "ÉVO."),
            ("mercato", "MERCATO"),
            ("explore", "STATS"),
            ("palmares", "🏆"),
        ):
            button = action(text, lambda _button, name=key: self.select_tab(name), CARD_ALT)
            button.font_size = dp(10)        # 6 onglets : police réduite pour tenir
            self.tab_buttons[key] = button
            tabs.add_widget(button)
        self._style_tabs()
        return tabs

    def _style_tabs(self):
        for key, button in self.tab_buttons.items():
            button.background_color = ACCENT if key == self.current_tab else CARD_ALT

    def startup(self):
        try:
            self.goal_sound = SoundLoader.load(core.resource_path("goal_buzzer.wav"))
        except Exception:
            self.goal_sound = None
        self.load_local_player_data()
        self.render_current()
        self.show_whats_new_once()
        self.refresh(force=True)
        self.refresh_player_data()
        threading.Thread(target=self.load_competitions, daemon=True).start()
        threading.Thread(target=self.check_update, daemon=True).start()

    def load_local_player_data(self):
        for path in (self.player_cache_path, core.resource_path(core.PLAYER_DATA_NAME)):
            try:
                with open(path, "rb") as stream:
                    self.player_data = core.parse_player_history_csv(stream.read())
                return
            except Exception:
                pass

    def refresh_player_data(self):
        def work():
            try:
                data = core.download_players_csv()
                parsed = core.parse_player_history_csv(data)
                os.makedirs(self.user_data_dir, exist_ok=True)
                temporary = self.player_cache_path + ".tmp"
                with open(temporary, "wb") as stream:
                    stream.write(data)
                os.replace(temporary, self.player_cache_path)
                self.player_data = parsed
                Clock.schedule_once(lambda _dt: self.render_current(), 0)
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def load_competitions(self):
        try:
            core.refresh_current_season()
            names = core.fetch_competitions(core.SEASON)
            if names:
                self.competitions = names
                if self.current_comp not in names:
                    self.current_comp = names[0]
                Clock.schedule_once(lambda _dt: self.render_current(), 0)
        except Exception:
            pass

    def select_tab(self, name):
        self.current_tab = name
        self._style_tabs()
        self.render_current()

    def clear_content(self):
        self.content.clear_widgets()
        self.scroll.scroll_y = 1

    def set_status(self, text):
        self.status.text = text

    def refresh(self, force=False):
        if self.loading:
            return
        if self.current_tab in ("evolution", "mercato", "explore") and not force:
            return
        self.loading = True
        self._render_status()

        def work():
            try:
                core.refresh_current_season()
                groups, standings = core.fetch_competition(self.current_comp)
                self.groups, self.standings = groups, standings
                error = None
            except Exception as exc:
                error = str(exc)

            def finish(_dt):
                self.loading = False
                if error:
                    self.last_error = error[:80]
                else:
                    self.last_error = None
                    self.last_update_ts = time.time()
                    goal = self._detect_goals(self.groups)
                    self.render_current()
                    if goal and self.config_data.get("goal_alert", True):
                        self._goal_alert()
                self._render_status()
            Clock.schedule_once(finish, 0)
        threading.Thread(target=work, daemon=True).start()

    def render_current(self):
        if self.current_tab == "scores":
            self.render_scores()
        elif self.current_tab == "standing":
            self.render_standing()
        elif self.current_tab == "mercato":
            self.render_mercato()
        elif self.current_tab == "explore":
            self.render_explore()
        elif self.current_tab == "palmares":
            self.render_palmares()
        else:
            self.render_evolution()

    def render_palmares(self):
        self.clear_content()
        self.add_title("Palmarès", "Records sur tous les matchs joués — 100% factuel")
        if self.palmares is None:
            self.content.add_widget(label("Calcul des records…", color=MUTED, height=50))
            if not getattr(self, "_palmares_loading", False):
                self._palmares_loading = True

                def work():
                    try:
                        d = core.palmares_data(top=3)
                    except Exception:
                        d = ({}, 0)
                    self.palmares = d
                    self._palmares_loading = False
                    Clock.schedule_once(lambda _dt: self.render_current(), 0)
                threading.Thread(target=work, daemon=True).start()
            return
        records, nmatch = self.palmares
        self.content.add_widget(label(f"Sur {nmatch} matchs joués, toutes saisons", color=MUTED, size=11, height=22))
        medals = ["🥇", "🥈", "🥉"]
        for key, title, sub in core.PALMARES_CATEGORIES:
            items = records.get(key) or []
            if not items:
                continue
            self.content.add_widget(label(title, color=ACCENT, size=15, bold=True, height=32))
            self.content.add_widget(label(sub, color=MUTED, size=10, height=18))
            for i, it in enumerate(items):
                card = Surface(orientation="vertical", color=CARD, size_hint_y=None, height=dp(50),
                               padding=(dp(10), dp(4)), spacing=0)
                top = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(24))
                top.add_widget(label(f"{medals[i]} {it['head']}", bold=True, color=ACCENT, size=12, height=24))
                top.add_widget(label(it["ctx"], color=MUTED, halign="right", size=9, height=24))
                card.add_widget(top)
                card.add_widget(label(it["desc"], color=FG, size=11, height=22))
                self.content.add_widget(card)

    def add_title(self, text, subtitle=None):
        self.content.add_widget(label(text, color=ACCENT, size=18, bold=True, height=38))
        if subtitle:
            self.content.add_widget(label(subtitle, color=MUTED, size=11, height=24))

    def add_competition_picker(self):
        picker = Spinner(
            text=self.current_comp,
            values=self.competitions,
            color=FG,
            background_normal="",
            background_color=CARD,
            size_hint_y=None,
            height=dp(46),
            font_size=dp(14),
        )

        def change(_spinner, value):
            if value != self.current_comp:
                self.current_comp = value
                self.match_scores = {}   # nouvelle compétition -> repart d'une référence
                self.save_config()
                self.refresh(force=True)
        picker.bind(text=change)
        self.content.add_widget(picker)

    def render_scores(self):
        self.clear_content()
        self.add_title("Scores", "Le direct est signalé en rouge.")
        self.add_competition_picker()
        if not self.groups:
            self.content.add_widget(label("Chargement des matchs...", color=MUTED, height=50))
            return
        for group in self.groups:
            self.content.add_widget(label(group["label"], color=ACCENT, bold=True, height=34))
            for match in group["matches"]:
                self.content.add_widget(self.match_card(match))

    def match_card(self, match):
        live = bool(match.get("site_live") or match.get("live"))
        card = Surface(
            orientation="vertical",
            color=(0.19, 0.09, 0.08, 1) if live else CARD,
            size_hint_y=None,
            height=dp(76),
            padding=(dp(10), dp(7)),
            spacing=dp(2),
        )
        line = BoxLayout(orientation="horizontal")
        line.add_widget(label(match.get("a") or "?", bold=True, height=34))
        line.add_widget(label(match.get("mid") or "-", color=RED if live else FG,
                              bold=True, halign="center", height=34))
        line.add_widget(label(match.get("b") or "?", bold=True, halign="right", height=34))
        card.add_widget(line)
        details = []
        if match.get("poss"):
            details.append(f"Possession {match['poss']}")
        if match.get("occ"):
            details.append(f"Occasions {match['occ']}")
        if live:
            details.insert(0, "EN DIRECT")
        card.add_widget(label("  |  ".join(details) or "Match programmé",
                              color=RED if live else MUTED, size=10, height=22,
                              halign="center"))
        return card

    def render_standing(self):
        self.clear_content()
        self.add_title("Classement", "Points, différence et buts marqués.")
        self.add_competition_picker()
        if not self.groups:
            self.content.add_widget(label("Chargement du classement...", color=MUTED, height=50))
            return
        rows = core.leaderboard(self.groups)
        if not rows:
            self.content.add_widget(label(
                "Aucun match joué pour le moment — le classement apparaîtra dès "
                "les premiers résultats.", color=MUTED, height=70))
            return
        for index, row in enumerate(rows, start=1):
            card = Surface(
                orientation="horizontal",
                color=CARD if index % 2 else CARD_ALT,
                size_hint_y=None,
                height=dp(48),
                padding=(dp(8), dp(4)),
            )
            pos = label(str(index), color=MUTED, bold=True, halign="center", height=40)
            pos.size_hint_x = None
            pos.width = dp(34)
            card.add_widget(pos)
            card.add_widget(label(row["team"], bold=True, height=40))
            stats = label(f"{row['points']} pts   {row['gd']:+d}   {row['gf']} buts",
                          color=GREEN if index <= 3 else FG, halign="right", height=40)
            stats.size_hint_x = 0.65
            card.add_widget(stats)
            self.content.add_widget(card)

    def render_evolution(self):
        self.clear_content()
        self.add_title("Evolutions de célébrité", "Comparez les saisons et les postes.")
        if not self.player_data:
            self.content.add_widget(label("Chargement des données joueurs...", color=MUTED,
                                          height=50))
            return
        seasons = self.player_data["seasons"]
        periods = [(a, b) for a, b in zip(seasons, seasons[1:])]
        if not periods:
            self.content.add_widget(label("Pas assez de saisons disponibles.", color=MUTED))
            return
        current_period = self.config_data.get("period")
        labels = [f"Saison {a} vers {b}" for a, b in periods]
        if current_period not in labels:
            moving = [
                text for text, (before, after) in zip(labels, periods)
                if any(
                    hist.get(before) is not None and hist.get(after) is not None
                    and hist[before] != hist[after]
                    for hist in self.player_data["histories"].values()
                )
            ]
            current_period = moving[-1] if moving else labels[-1]
        current_poste = self.config_data.get("poste", "Tous")

        controls = BoxLayout(orientation="horizontal", spacing=dp(6), size_hint_y=None,
                             height=dp(46))
        period_picker = Spinner(text=current_period, values=labels, background_normal="",
                                background_color=CARD, color=FG)
        poste_picker = Spinner(text=current_poste, values=["Tous"] + list(core.PLAYER_POSTES),
                               background_normal="", background_color=CARD, color=FG,
                               size_hint_x=0.42)
        controls.add_widget(period_picker)
        controls.add_widget(poste_picker)
        self.content.add_widget(controls)

        def change(*_):
            self.config_data["period"] = period_picker.text
            self.config_data["poste"] = poste_picker.text
            self.save_config()
            self.render_evolution()
        period_picker.bind(text=change)
        poste_picker.bind(text=change)

        before, after = periods[labels.index(period_picker.text)]
        all_rows = core.celebrity_evolution_rows(
            self.player_data["players"], self.player_data["histories"], before, after
        )
        rows = all_rows if poste_picker.text == "Tous" else [
            row for row in all_rows if row["poste"] == poste_picker.text
        ]
        self.content.add_widget(label("Moyenne par poste", color=ACCENT, bold=True, height=38))
        for role in core.role_evolution_summary(all_rows):
            delta = role["avg"]
            card = Surface(orientation="horizontal", color=CARD, size_hint_y=None,
                           height=dp(54), padding=(dp(9), dp(4)))
            role_name = label(role["poste"], bold=True, height=44)
            role_name.shorten = False
            role_name.size_hint_x = None
            role_name.width = dp(58)
            card.add_widget(role_name)
            card.add_widget(label(f"{role['count']} joueurs", color=MUTED, height=44))
            card.add_widget(label(f"{delta:+.1f}", color=GREEN if delta > 0 else RED,
                                  bold=True, halign="right", height=44))
            self.content.add_widget(card)

        self.add_evolution_list("Plus fortes hausses", rows, True)
        self.add_evolution_list("Plus fortes baisses", rows, False)

    def add_evolution_list(self, title, rows, descending):
        self.content.add_widget(label(title, color=ACCENT, bold=True, height=42))
        for rank, row in enumerate(
            sorted(rows, key=lambda item: item["delta"], reverse=descending)[:12], start=1
        ):
            delta = row["delta"]
            age = row["player"].get("age")
            age_text = f"{int(age)} ans" if isinstance(age, (int, float)) else "âge indisponible"
            card = Surface(orientation="vertical", color=CARD, size_hint_y=None,
                           height=dp(68), padding=(dp(10), dp(5)), spacing=0)
            top = BoxLayout(orientation="horizontal")
            name = label(f"{rank}. {row['nom']}", bold=True, height=32)
            delta_label = label(f"{delta:+.1f}", color=GREEN if delta > 0 else RED,
                                bold=True, halign="right", height=32)
            delta_label.size_hint_x = 0.3
            top.add_widget(name)
            top.add_widget(delta_label)
            card.add_widget(top)
            card.add_widget(label(
                f"{row['poste']}  |  {age_text}  |  {row['team'] or '?'}  |  "
                f"{row['before']:.1f} vers {row['after']:.1f}",
                color=MUTED, size=10, height=24,
            ))
            self.content.add_widget(card)

    # ---- Mercato ----------------------------------------------------------
    def ensure_mercato_pool(self):
        if self.mercato_pool is not None:
            return
        def work():
            self.mercato_pool = core.api_all_joueurs(core.SEASON)
            Clock.schedule_once(lambda _dt: self.render_current(), 0)
        threading.Thread(target=work, daemon=True).start()

    def _mspin(self, value, values, attr, width=None):
        sp = Spinner(text=str(value), values=[str(v) for v in values], color=FG,
                     background_normal="", background_color=CARD, size_hint_y=None,
                     height=dp(40), font_size=dp(13))
        if width:
            sp.size_hint_x = None
            sp.width = dp(width)

        def change(_s, v):
            setattr(self, attr, v)
            self.render_current()
        sp.bind(text=change)
        return sp

    def _mercato_slots(self):
        # Une équipe Foothunter = exactement 1 joueur par poste (slot_id = poste).
        return [(poste, poste) for poste in core.TEAM_POSTES]

    def _mercato_remove(self, slot_id):
        self.mercato_squad.pop(slot_id, None)
        self.mercato_years.pop(slot_id, None)
        self.render_current()

    def _mercato_eligible(self, poste):
        try:
            hi = float(self.mercato_pmax)
        except (TypeError, ValueError):
            hi = 1e9
        taken = {p.get("id") for p in self.mercato_squad.values() if p}
        out = [p for p in (self.mercato_pool or [])
               if (p.get("poste") or "").upper() == poste and p.get("id") not in taken
               and isinstance(p.get("salaire"), (int, float)) and p.get("salaire") <= hi]
        out.sort(key=lambda p: -(p.get("celebrite") or 0))
        return out

    def _mercato_pick(self, poste, slot_id):
        cands = self._mercato_eligible(poste)
        body = BoxLayout(orientation="vertical", spacing=dp(4), padding=dp(6))
        popup = Popup(title=f"Recruter — {poste} (≤ {self.mercato_pmax} M€)", content=body,
                      size_hint=(0.95, 0.85), background_color=HEADER, separator_color=ACCENT)
        sv = ScrollView()
        lst = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(2))
        lst.bind(minimum_height=lst.setter("height"))
        if not cands:
            lst.add_widget(label("Aucun joueur dans ce budget.", color=MUTED, height=40))
        for p in cands[:80]:
            def assign(_b, pl=p):
                self.mercato_squad[slot_id] = pl
                self.mercato_years.setdefault(slot_id, 1)
                popup.dismiss()
                self.render_current()
            lst.add_widget(action(
                f"{p.get('nom')} ({p.get('salaire')}M) · {p.get('nom_equipe')} · célé {p.get('celebrite')}",
                assign, CARD))
        sv.add_widget(lst)
        body.add_widget(sv)
        body.add_widget(action("Fermer", lambda *_: popup.dismiss(), CARD_ALT))
        popup.open()

    def render_mercato(self):
        self.clear_content()
        self.add_title("Mercato", "Coût = salaire × années, payé d'avance.")
        ctrl = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(44), spacing=dp(6))
        ctrl.add_widget(self._mspin(self.mercato_cap, [100, 150, 200, 250, 300, 400, 600], "mercato_cap"))
        ctrl.add_widget(self._mspin(self.mercato_pmax, [10, 20, 30, 40, 60, 100], "mercato_pmax"))
        self.content.add_widget(ctrl)
        self.content.add_widget(label("budget M€ · prix max M€ · 1 joueur par poste", color=MUTED, size=10, height=20))
        if self.mercato_pool is None:
            self.ensure_mercato_pool()
            self.content.add_widget(label("Chargement des joueurs…", color=MUTED, height=50))
            return
        total = 0.0
        filled = []
        signings = []
        for poste, slot_id in self._mercato_slots():
            p = self.mercato_squad.get(slot_id)
            row = Surface(orientation="horizontal", color=CARD, size_hint_y=None, height=dp(46),
                          padding=(dp(8), dp(4)), spacing=dp(4))
            pl = label(poste, color=ACCENT, bold=True, size=12, height=38)
            pl.size_hint_x = None
            pl.width = dp(48)
            row.add_widget(pl)
            if p:
                yr = self.mercato_years.get(slot_id, 1)
                cost = core.contract_cost(p.get("salaire"), yr) or 0
                total += cost
                filled.append(p)
                signings.append((poste, cost))
                row.add_widget(label(p.get("nom") or "?", size=12, height=38))
                yr_sp = Spinner(text=str(yr), values=["1", "2", "3"], color=FG, background_normal="",
                                background_color=CARD_ALT, size_hint=(None, None), width=dp(42),
                                height=dp(38), font_size=dp(12))

                def _yr(_s, v, s=slot_id):
                    self.mercato_years[s] = int(v)
                    self.render_current()
                yr_sp.bind(text=_yr)
                row.add_widget(yr_sp)
                cl = label(f"{cost:g}M", color=MUTED, size=11, halign="right", height=38)
                cl.size_hint_x = None
                cl.width = dp(46)
                row.add_widget(cl)
                row.add_widget(action("X", lambda *_, s=slot_id: self._mercato_remove(s), RED, 36))
            else:
                row.add_widget(label("— vide —", color=MUTED, size=12, height=38))
                row.add_widget(action("+ recruter",
                                      lambda *_, ps=poste, s=slot_id: self._mercato_pick(ps, s), GREEN, 108))
            self.content.add_widget(row)
        try:
            cap = float(self.mercato_cap)
        except (TypeError, ValueError):
            cap = 0.0
        agg = core.squad_aggregate(filled)
        over = cap > 0 and total > cap
        self.content.add_widget(label(f"Budget : {total:g} / {cap:g} M€",
                                      color=RED if over else GREEN, bold=True, size=15, height=36))
        self.content.add_widget(label(
            f"{agg['count']}/{len(core.TEAM_POSTES)} · célé moy. {agg['avg_celebrite'] or '—'} · "
            f"âge moy. {agg['avg_age'] or '—'}", color=MUTED, size=11, height=24))
        self.content.add_widget(label("Budget investi par domaine (M€, au prorata du poste)",
                                      color=ACCENT, bold=True, height=32))
        invest = core.team_domain_investment(signings)
        imax = max(list(invest.values()) + [1.0])
        for d in core.DOMAINS:
            self.content.add_widget(bar_row(core.DOMAIN_LABELS[d], invest.get(d), imax))

    # ---- Explorateur de stats --------------------------------------------
    def _set_explore_mode(self, mode):
        self.explore_mode = mode
        self.render_current()

    def render_explore(self):
        self.clear_content()
        self.add_title("Stats", "Explore joueurs et équipes.")
        toggle = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(42), spacing=dp(6))
        for m in ("Joueurs", "Équipes"):
            toggle.add_widget(action(m, lambda _b, mm=m: self._set_explore_mode(mm),
                                     ACCENT if self.explore_mode == m else CARD_ALT))
        self.content.add_widget(toggle)
        if self.explore_mode == "Joueurs":
            self._render_explore_players()
        else:
            self._render_explore_teams()

    def _render_explore_players(self):
        # Trouveur par rôle, multi-ligues : stat pertinente de l'équipe (ex. GAR ->
        # Arrêts %) + « adversité » (célé des postes adverses affrontés). « + » ajoute
        # le joueur au mercato.
        if not self.competitions:
            self.content.add_widget(label("Compétitions en chargement…", color=MUTED, height=40))
            return
        poste = self.explore_poste if self.explore_poste in core.ROLE_RELEVANCE else "GAR"
        self.explore_poste = poste
        ctrl = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(42), spacing=dp(6))
        ctrl.add_widget(self._mspin(poste, list(core.ROLE_RELEVANCE), "explore_poste"))
        ctrl.add_widget(self._mspin(self.explore_pmax, [10, 20, 30, 40, 60, 100], "explore_pmax"))
        ctrl.add_widget(self._mspin(self.explore_adv, ["Adv: tous", "Adv: décisifs"], "explore_adv"))
        self.content.add_widget(ctrl)
        self.content.add_widget(label("Ligues à scouter (coche) · prix max M€", color=MUTED, size=10, height=20))
        grid = GridLayout(cols=4, size_hint_y=None, spacing=dp(4))
        grid.bind(minimum_height=grid.setter("height"))
        for lg in core.SCOUT_LEAGUES:
            tb = ToggleButton(text=LEAGUE_ABBR.get(lg, lg),
                              state="down" if lg in self.explore_leagues else "normal",
                              size_hint_y=None, height=dp(34), font_size=dp(11),
                              background_normal="", background_down="",
                              background_color=ACCENT if lg in self.explore_leagues else CARD, color=FG)

            def _tog(btn, lg=lg):
                if btn.state == "down" and lg not in self.explore_leagues:
                    self.explore_leagues.append(lg)
                elif btn.state == "normal" and lg in self.explore_leagues:
                    self.explore_leagues.remove(lg)
                self.render_current()
            tb.bind(state=_tog)
            grid.add_widget(tb)
        self.content.add_widget(grid)

        leagues = [lg for lg in core.SCOUT_LEAGUES if lg in self.explore_leagues] or list(core.MAJOR_LEAGUES)
        adv_key = "opp_dec" if self.explore_adv == "Adv: décisifs" else "opp"
        stat_key, stat_label, counters = core.ROLE_RELEVANCE[poste]
        advmode = "matchs décisifs" if adv_key == "opp_dec" else "tous matchs"
        self.content.add_widget(label(
            f"{poste} · {stat_label} · adv = célé {'/'.join(counters)} adverses ({advmode})",
            color=MUTED, size=10, height=22))
        if self.mercato_pool is None:
            self.ensure_mercato_pool()
            self.content.add_widget(label("Chargement des joueurs…", color=MUTED, height=50))
            return
        cache = getattr(self, "_scout_cache", None)
        if cache is None:
            cache = self._scout_cache = {}
        missing = [lg for lg in leagues if (lg, poste) not in cache]
        if missing:
            self.content.add_widget(label(f"Analyse de {len(missing)} ligue(s)…", color=MUTED, height=40))

            def work(ls=tuple(missing), pp=poste):
                for lg in ls:
                    try:
                        cache[(lg, pp)] = core.role_scout_rows(lg, pp, self.mercato_pool)
                    except Exception:
                        cache[(lg, pp)] = []
                Clock.schedule_once(lambda _dt: self.render_current(), 0)
            threading.Thread(target=work, daemon=True).start()
            return
        try:
            hi = float(self.explore_pmax)
        except (TypeError, ValueError):
            hi = 1e9
        seen, agg = set(), []
        for lg in leagues:
            for r in cache[(lg, poste)]:
                key = (r["nom"], r["team"])
                if key not in seen:
                    seen.add(key)
                    agg.append(r)
        rows = [r for r in agg if r["salaire"] is not None and r["salaire"] <= hi]
        rows.sort(key=lambda r: (r.get("stat") is None, -(r.get("stat") or 0)))
        short = stat_label.split()[0]
        self.content.add_widget(label(f"{len(rows)} {poste} sur {len(leagues)} ligue(s)", color=MUTED, size=10, height=22))
        for r in rows[:150]:
            card = Surface(orientation="vertical", color=CARD, size_hint_y=None, height=dp(66),
                           padding=(dp(10), dp(4)), spacing=0)
            top = BoxLayout(orientation="horizontal")
            top.add_widget(label(f"{r['nom']} · {r['team']}", bold=True, size=12, height=24))
            top.add_widget(label(f"{short} {r['stat'] if r['stat'] is not None else '—'}",
                                 color=ACCENT, halign="right", size=12, height=24))
            card.add_widget(top)
            bottom = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(28))
            bottom.add_widget(label(
                f"{LEAGUE_ABBR.get(r.get('competition'), '?')} · adv {r[adv_key] if r.get(adv_key) is not None else '—'} · "
                f"célé {r['celebrite']} · {r['salaire']}M", color=MUTED, size=10, height=26))
            addbtn = Button(text="+ merc", color=FG, background_normal="", background_color=GREEN,
                            font_size=dp(11), size_hint=(None, None), width=dp(78), height=dp(26))

            def _add(_b, rr=r, pp=poste):
                self.mercato_squad[pp] = {"nom": rr["nom"], "poste": pp, "nom_equipe": rr["team"],
                                          "salaire": rr["salaire"], "celebrite": rr["celebrite"], "age": rr["age"]}
                self.mercato_years.setdefault(pp, 1)
                _b.text = "ajouté ✓"
            addbtn.bind(on_release=_add)
            bottom.add_widget(addbtn)
            card.add_widget(bottom)
            self.content.add_widget(card)

    def _render_explore_teams(self):
        tmetrics = {"Buts / match": "gf_pm", "Encaissés / match": "ga_pm", "Possession %": "poss",
                    "Conversion %": "conv", "Arrêts %": "save", "Clean sheets": "clean"}
        if not self.competitions:
            self.content.add_widget(label("Compétitions en chargement…", color=MUTED, height=40))
            return
        comp = self.explore_comp if self.explore_comp in self.competitions else self.competitions[0]
        self.explore_comp = comp
        ctrl = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(42), spacing=dp(6))
        ctrl.add_widget(self._mspin(comp, self.competitions, "explore_comp"))
        ctrl.add_widget(self._mspin(self.explore_metric, list(tmetrics), "explore_metric"))
        self.content.add_widget(ctrl)
        cache = getattr(self, "_explore_team_cache", None)
        if cache is None:
            cache = self._explore_team_cache = {}
        ds = cache.get(comp)
        if ds is None:
            self.content.add_widget(label("Chargement de la compétition…", color=MUTED, height=40))

            def work():
                try:
                    groups, _ = core.fetch_competition(comp)
                    cache[comp] = core.team_domain_stats(groups)
                except Exception:
                    cache[comp] = {}
                Clock.schedule_once(lambda _dt: self.render_current(), 0)
            threading.Thread(target=work, daemon=True).start()
            return
        key = tmetrics.get(self.explore_metric, "gf_pm")
        rows = sorted(((t, s.get(key)) for t, s in ds.items() if s.get(key) is not None),
                      key=lambda r: -r[1])
        if not rows:
            self.content.add_widget(label("Aucune donnée.", color=MUTED, height=40))
            return
        vmax = max(v for _, v in rows) or 1
        for t, v in rows:
            self.content.add_widget(bar_row(t, v, vmax))

    def show_whats_new_once(self):
        build_id = getattr(core, "APP_COMMIT", "") or APP_VERSION
        if self.config_data.get("whats_new_seen_build") == build_id:
            return
        self.config_data["whats_new_seen_build"] = build_id
        self.save_config()
        notes = core.load_whats_new()
        if notes:
            self.show_popup("Nouveautés", notes.replace("## ", "").replace("# ", ""),
                            "FERMER")

    def show_popup(self, title, text, button_text, callback=None):
        body = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))
        scroll = ScrollView(do_scroll_x=False)
        note = label(text, color=FG, size=13, height=400)
        note.shorten = False
        note.valign = "top"
        note.bind(texture_size=lambda instance, value: setattr(instance, "height", value[1] + dp(20)))
        scroll.add_widget(note)
        body.add_widget(scroll)
        popup = Popup(title=title, content=body, size_hint=(0.92, 0.78),
                      background_color=HEADER, separator_color=ACCENT)

        def close(*_):
            popup.dismiss()
            if callback:
                callback()
        body.add_widget(action(button_text, close, ACCENT))
        popup.open()

    def check_update(self):
        try:
            url = f"https://api.github.com/repos/{core.GITHUB_REPO}/releases/tags/{core.UPDATE_RELEASE_TAG}"
            release = json.loads(core._http_get_url(url, timeout=core.UPDATE_TIMEOUT))
            assets = release.get("assets") or []
            apk = next((a for a in assets if a.get("name") == APK_ASSET_NAME), None)
            marker = next((a for a in assets if a.get("name") == APK_COMMIT_ASSET_NAME), None)
            current = getattr(core, "APP_COMMIT", "").strip()
            if not (apk and marker and current):
                return
            # On compare le commit RÉEL de l'APK publié (marqueur déposé par le
            # workflow Android), pas target_commitish : depuis la séparation des
            # pipelines, ce dernier suit l'exe Windows et avancerait sans qu'un
            # nouvel APK existe -> fausse « mise à jour » en boucle.
            latest = core._http_get_url(
                marker.get("browser_download_url") or "", timeout=core.UPDATE_TIMEOUT
            ).strip()
            if latest and not core._same_commit(latest, current):
                self.update_url = apk.get("browser_download_url") or ""
                Clock.schedule_once(lambda _dt: self.enable_update(), 0)
        except Exception:
            pass

    def enable_update(self):
        self.update_button.opacity = 1
        self.update_button.disabled = False
        self.update_button.width = dp(116)

    def open_update(self):
        if not self.update_url:
            return
        self.show_popup(
            "Mise à jour disponible",
            "Une nouvelle version Android est disponible. Android vous demandera "
            "de confirmer son installation.",
            "TELECHARGER",
            lambda: open_url(self.update_url),
        )

    def open_settings(self):
        body = BoxLayout(orientation="vertical", spacing=dp(12), padding=dp(16))

        # Alerte but (son · flash · vibration) : bouton bascule activée/désactivée.
        row1 = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(48), spacing=dp(8))
        row1.add_widget(label("Alerte de but (son · flash · vibration)", height=44))
        holder = {}

        def toggle_alert(*_):
            on = not bool(self.config_data.get("goal_alert", True))
            self.config_data["goal_alert"] = on
            self.save_config()
            holder["btn"].text = "Activée" if on else "Désactivée"
            holder["btn"].background_color = GREEN if on else CARD_ALT
        on0 = bool(self.config_data.get("goal_alert", True))
        holder["btn"] = action("Activée" if on0 else "Désactivée", toggle_alert,
                               GREEN if on0 else CARD_ALT, 132)
        row1.add_widget(holder["btn"])
        body.add_widget(row1)

        # Fréquence d'actualisation auto.
        row2 = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(48), spacing=dp(8))
        row2.add_widget(label("Actualisation auto", height=46))
        options = ["10 s", "15 s", "30 s", "60 s", "120 s"]
        current = f"{self._refresh_secs()} s"
        if current not in options:
            options.append(current)
        spinner = Spinner(text=current, values=options, size_hint_x=None, width=dp(120),
                          color=FG, background_normal="", background_color=CARD, font_size=dp(14))

        def on_rate(_spinner, value):
            try:
                self.config_data["refresh_secs"] = int(value.split()[0])
                self.save_config()
                self._apply_refresh_interval()
            except (ValueError, IndexError):
                pass
        spinner.bind(text=on_rate)
        row2.add_widget(spinner)
        body.add_widget(row2)

        body.add_widget(Widget())   # pousse le bouton « Fermer » en bas
        popup = Popup(title="Réglages", content=body, size_hint=(0.92, 0.55),
                      background_color=HEADER, separator_color=ACCENT)
        body.add_widget(action("FERMER", lambda *_: popup.dismiss(), ACCENT))
        popup.open()


def open_url(url):
    try:
        from jnius import autoclass
        Intent = autoclass("android.content.Intent")
        Uri = autoclass("android.net.Uri")
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        request = Intent(Intent.ACTION_VIEW)
        request.setData(Uri.parse(url))
        activity.startActivity(request)
    except Exception:
        webbrowser.open(url)
