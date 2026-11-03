# PatchPilot web app. Works on Render, Hugging Face Spaces, Fly.io or any Docker host.
#   docker build -t patchpilot . && docker run -p 8501:8501 patchpilot
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATCHPILOT_PUBLIC=1

# Unprivileged user: fix verification runs generated tests, so never as root.
RUN useradd --create-home --uid 1000 app
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=app:app . .
USER app

# Hosts like Render inject $PORT; default for local runs.
EXPOSE 8501
CMD streamlit run app.py --server.address 0.0.0.0 --server.port ${PORT:-8501} \
    --server.headless true --browser.gatherUsageStats false
