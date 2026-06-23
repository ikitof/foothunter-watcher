#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Modèle Mercato / simulation de Foot Live (logique pure, sans réseau ni état).

Règles tirées du manuel Présentation-générale.pdf. Importé par foot_scores (cœur)
et, via lui, par l'app desktop (fh_gui) et l'app Android (mobile_app)."""


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
# Composition réelle d'une équipe Foothunter : exactement 1 joueur par poste
# (vérifié sur les 140 équipes de l'API — 7 joueurs, un par poste).
TEAM_POSTES = ["GAR", "DC", "LAT", "MDEF", "MOFF", "AIL", "AC"]


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


def team_domain_investment(signings):
    """Argent investi par domaine (M€) : la dépense de chaque joueur est répartie sur
    les domaines de son poste au prorata de POSTE_DOMAIN_WEIGHTS. C'est l'indicateur
    « où va le budget » du mercato. signings = [(poste, montant)]."""
    out = {d: 0.0 for d in DOMAINS}
    for poste, money in signings:
        m = _num(money)
        if m is None:
            continue
        for dom, w in POSTE_DOMAIN_WEIGHTS.get((poste or "").upper(), {}).items():
            out[dom] += m * w / 100.0
    return {d: round(v, 2) for d, v in out.items()}


# Pour chaque poste : (stat d'équipe pertinente, libellé, postes adverses dont la
# célébrité mesure l'« adversité » affrontée dans ce rôle). Stats issues de
# team_domain_stats ; « plus c'est haut, mieux c'est ».
ROLE_RELEVANCE = {
    "GAR":  ("save",       "Arrêts %",            ["AC", "AIL", "MOFF"]),
    "DC":   ("clean",      "Clean sheets",        ["AC", "AIL", "MOFF"]),
    "LAT":  ("clean",      "Clean sheets",        ["AIL", "AC"]),
    "MDEF": ("poss",       "Possession %",        ["MOFF", "AIL"]),
    "MOFF": ("occ_for_pm", "Occasions créées /m", ["MDEF", "DC"]),
    "AIL":  ("conv",       "Conversion %",        ["LAT", "DC"]),
    "AC":   ("gf_pm",      "Buts / match",        ["GAR", "DC"]),
}

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
