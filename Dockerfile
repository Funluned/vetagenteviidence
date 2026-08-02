FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# Runtime stores derive the project root from the imported package path.  An
# editable install keeps that path under /app instead of a root-owned system
# site-packages directory.
RUN python -m pip install --editable .

COPY --chown=appuser:appuser app.py ./
COPY --chown=appuser:appuser data ./data
RUN mkdir -p /app/.workbench \
    && chown appuser:appuser /app/.workbench

USER appuser

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health', timeout=5)"

CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", "--browser.gatherUsageStats=false"]
