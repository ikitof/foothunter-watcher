#!/usr/bin/env python3
"""Interface Android Kivy pour Foot Live."""

import json
import os
import threading
import time
import urllib.parse
import webbrowser

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, RoundedRectangle
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
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
        self.root_layout = BoxLayout(orientation="vertical", spacing=0)
        self.root_layout.add_widget(self._build_header())
        self.root_layout.add_widget(self._build_tabs())

        self.scroll = ScrollView(do_scroll_x=False, bar_width=dp(3))
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
            height=dp(30),
            padding=(dp(10), 0),
        )
        footer.add_widget(self.status)
        self.root_layout.add_widget(footer)

        Clock.schedule_once(lambda _dt: self.startup(), 0.2)
        self.refresh_event = Clock.schedule_interval(lambda _dt: self.refresh(), 30)
        return self.root_layout

    def _build_header(self):
        header = Surface(
            orientation="horizontal",
            color=HEADER,
            size_hint_y=None,
            height=dp(64),
            padding=(dp(12), dp(8)),
            spacing=dp(8),
        )
        title_box = BoxLayout(orientation="vertical")
        title_box.add_widget(label("FOOT LIVE", color=FG, size=19, bold=True, height=28))
        title_box.add_widget(label("Scores et tendances", color=MUTED, size=11, height=22))
        header.add_widget(title_box)
        self.update_button = action("MISE A JOUR", lambda *_: self.open_update(), GREEN, 116)
        self.update_button.opacity = 0
        self.update_button.disabled = True
        header.add_widget(self.update_button)
        header.add_widget(action("ACTUALISER", lambda *_: self.refresh(force=True), ACCENT, 112))
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
            ("standing", "CLASSEMENT"),
            ("evolution", "EVOLUTIONS"),
        ):
            button = action(text, lambda _button, name=key: self.select_tab(name), CARD_ALT)
            self.tab_buttons[key] = button
            tabs.add_widget(button)
        self._style_tabs()
        return tabs

    def _style_tabs(self):
        for key, button in self.tab_buttons.items():
            button.background_color = ACCENT if key == self.current_tab else CARD_ALT

    def startup(self):
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
            names = core.parse_competitions(core.http_get(core.SAISON_PATH))
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
        if self.current_tab == "evolution" and not force:
            return
        self.loading = True
        self.set_status("Actualisation...")

        def work():
            try:
                groups, standings = core.fetch_competition(self.current_comp)
                self.groups, self.standings = groups, standings
                error = None
            except Exception as exc:
                error = str(exc)

            def finish(_dt):
                self.loading = False
                if error:
                    self.set_status(f"Hors ligne : {error[:80]}")
                else:
                    self.set_status(f"Mis à jour à {time.strftime('%H:%M:%S')}")
                    self.render_current()
            Clock.schedule_once(finish, 0)
        threading.Thread(target=work, daemon=True).start()

    def render_current(self):
        if self.current_tab == "scores":
            self.render_scores()
        elif self.current_tab == "standing":
            self.render_standing()
        else:
            self.render_evolution()

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
            latest = (release.get("target_commitish") or "").strip()
            current = getattr(core, "APP_COMMIT", "").strip()
            asset = next(
                (item for item in release.get("assets") or [] if item.get("name") == APK_ASSET_NAME),
                None,
            )
            if asset and latest and current and not core._same_commit(latest, current):
                self.update_url = asset.get("browser_download_url") or ""
                Clock.schedule_once(lambda _dt: self.enable_update(), 0)
        except Exception:
            pass

    def enable_update(self):
        self.update_button.opacity = 1
        self.update_button.disabled = False

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
