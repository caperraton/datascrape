FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system --gid 10001 datascrape \
    && useradd --system --uid 10001 --gid datascrape --home-dir /home/datascrape datascrape

COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml README.md ./
COPY datascrape ./datascrape
RUN pip install --no-cache-dir --no-build-isolation --no-deps . \
    && mkdir -p /data /app/reports /app/logs /app/runs /app/backups \
    && chown -R datascrape:datascrape /data /app/reports /app/logs /app/runs /app/backups

USER 10001:10001

ENTRYPOINT ["datascrape"]
CMD ["list-reports"]
