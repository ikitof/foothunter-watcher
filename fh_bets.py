#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Logique PURE des pronostics (paris) Foothunter — barème + indexation des résultats.

Aucune dépendance réseau ni interface graphique : importable seul et testable via
``python3 fh_bets.py --selftest``. Utilisé par le backend web (web/server.py).

Rappel du modèle de données du jeu : l'API n'expose NI identifiant de match NI date/heure.
L'identité d'un match est donc (saison, compétition, domicile, extérieur) — le couple
(domicile, extérieur) ordonné distingue les deux confrontations aller/retour."""

import sys

# Barème de points, LINÉAIRE, facile à régler. Un score exact vaut le maximum (+25) ; plus la
# prédiction s'éloigne du score réel, moins on gagne (et si le résultat est faux, on perd,
# d'autant plus que l'écart de buts est grand). Les nuls sont gérés comme un résultat à part.
POINTS = {
    "max": 25,        # score EXACT (maximum de points)
    "step_ok": 5,     # points en moins par but d'écart quand le RÉSULTAT (vainqueur/nul) est bon
    "floor_ok": 5,    # plancher POSITIF si bon résultat -> on gagne toujours des points
    "step_ko": 3,     # points en moins par but d'écart quand le résultat est FAUX
    "floor_ko": -15,  # plancher NÉGATIF (perte maximale sur un pari)
}


def outcome(h, a):
    """Issue d'un score : 'H' (domicile gagne), 'A' (extérieur gagne) ou 'D' (nul)."""
    return "H" if h > a else ("A" if h < a else "D")


def bet_points(pred_home, pred_away, act_home, act_away, points=POINTS):
    """Points d'un pronostic (barème linéaire, cf. POINTS).

    - Bon résultat (même issue, nul compris) : ``max(floor_ok, max - step_ok*err)`` — toujours
      positif, d'autant plus haut que le score est proche (max = score exact).
    - Mauvais résultat : ``max(floor_ko, -step_ko*err)`` — toujours négatif, d'autant plus bas
      que l'écart total de buts est grand.
    où ``err = |pred_home-act_home| + |pred_away-act_away|`` (distance de buts)."""
    ph, pa, ah, aa = int(pred_home), int(pred_away), int(act_home), int(act_away)
    err = abs(ph - ah) + abs(pa - aa)
    if outcome(ph, pa) == outcome(ah, aa):
        return max(points["floor_ok"], points["max"] - points["step_ok"] * err)
    return max(points["floor_ko"], -points["step_ko"] * err)


def build_played_index(rows):
    """{(compétition, domicile, extérieur): (buts_dom, buts_ext)} depuis une liste de matchs
    JOUÉS normalisés (dicts {competition, home, away, home_goals, away_goals}). Ignore les
    entrées incomplètes (score None) — un match en cours ne doit pas être réglé."""
    idx = {}
    for r in rows:
        c, h, a = r.get("competition"), r.get("home"), r.get("away")
        hg, ag = r.get("home_goals"), r.get("away_goals")
        if c and h and a and hg is not None and ag is not None:
            idx[(c, h, a)] = (int(hg), int(ag))
    return idx


def selftest():
    assert (outcome(2, 1), outcome(1, 1), outcome(0, 2)) == ("H", "D", "A")
    # (pred) vs (actual) -> points attendus (barème par défaut)
    cases = [
        ((2, 1), (2, 1), 25),   # score exact
        ((2, 1), (3, 2), 15),   # bon vainqueur, err 2
        ((2, 1), (1, 0), 15),   # bon vainqueur, err 2
        ((2, 1), (5, 0), 5),    # bon vainqueur, err 4 -> plancher +5
        ((1, 1), (1, 1), 25),   # nul exact
        ((1, 1), (3, 3), 5),    # bon nul, err 4
        ((1, 1), (0, 0), 15),   # bon nul, err 2
        ((1, 0), (0, 0), -3),   # faux (H vs D), err 1
        ((1, 0), (0, 1), -6),   # faux (H vs A), err 2
        ((0, 2), (5, 0), -15),  # faux (A vs H), err 7 -> plancher -15
    ]
    for (ph, pa), (ah, aa), exp in cases:
        got = bet_points(ph, pa, ah, aa)
        assert got == exp, f"{(ph, pa)} vs {(ah, aa)} -> {got} != {exp}"
    # bon résultat TOUJOURS positif (même très loin) ; mauvais résultat TOUJOURS négatif
    assert bet_points(9, 0, 1, 0) == 5 and bet_points(0, 0, 3, 0) < 0
    # ordre garanti : exact > bon écart > bon résultat seul > 0 > faux
    assert bet_points(2, 1, 2, 1) > bet_points(2, 1, 3, 2) > bet_points(2, 1, 5, 0) > 0
    assert bet_points(2, 1, 5, 0) > bet_points(1, 0, 0, 1)
    # idempotence (fonction pure)
    assert bet_points(2, 1, 3, 2) == bet_points(2, 1, 3, 2)
    # indexation : score None ignoré, clé (comp, home, away)
    idx = build_played_index([
        {"competition": "L1", "home": "A", "away": "B", "home_goals": 2, "away_goals": 1},
        {"competition": "L1", "home": "C", "away": "D", "home_goals": None, "away_goals": 1},
    ])
    assert idx == {("L1", "A", "B"): (2, 1)}
    print("fh_bets selftest OK ✅")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
