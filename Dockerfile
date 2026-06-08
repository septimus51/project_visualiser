FROM python:3.11-slim

WORKDIR /app

# Copier le script
COPY code_analyzer.py .

# Créer un répertoire pour les projets à analyser
RUN mkdir -p /project

# Point d'entrée : le script attend le chemin du projet en argument
ENTRYPOINT ["python", "code_analyzer.py"]
CMD ["--help"]