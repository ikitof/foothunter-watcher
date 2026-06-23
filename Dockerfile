# Conteneur pour LANCER l'app desktop Foot Live (Tkinter) — l'affichage est renvoyé
# vers le serveur X11 de l'hôte Linux (voir docker-compose.yml).
#
# On part d'une image Debian + python3 du système : tkinter (python3-tk) y est
# correctement lié, contrairement aux images officielles "python:*" qui ne
# l'embarquent pas. L'app desktop n'utilise que la bibliothèque standard + tkinter,
# donc aucune dépendance pip n'est nécessaire pour la lancer.
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-tk ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

# La fenêtre s'affiche via le DISPLAY + le socket X11 montés par docker-compose.
CMD ["python3", "foot_scores.py"]
