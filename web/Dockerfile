# Image web « Foothunter Analyzer » — INDÉPENDANTE du desktop/mobile.
# Ne contient que le cœur (foot_scores + fh_mercato, sans tkinter/kivy) + le backend web
# + la SPA. À builder depuis la RACINE du repo : docker build -f web/Dockerfile -t foothunter-analyzer .
FROM python:3.12-slim
WORKDIR /app

COPY web/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Cœur réutilisé (logique pure, importable sans interface graphique)
COPY foot_scores.py fh_mercato.py ./
# Backend + SPA
COPY web/server.py ./server.py
COPY web/static ./static

ENV FH_WEB_DATA=/data
VOLUME ["/data"]
EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
