FROM python:3.12-slim

# tzdata so RUN_TIMES follow the TZ Unraid passes in; tini reaps the browser's
# leftover processes after a sign-in (Python as PID 1 would leave zombies)
RUN apt-get update && apt-get install -y --no-install-recommends tzdata tini \
    && rm -rf /var/lib/apt/lists/*

# Headless Chromium for Amazon's JavaScript sign-in challenge. Shared path so
# the unprivileged runtime user can launch it; only used while signing in.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
# Install the dependencies (and the browser) before copying the source, so
# code changes don't re-download Chromium.
COPY pyproject.toml README.md ./
RUN mkdir -p src/itemizer && touch src/itemizer/__init__.py \
    && pip install --no-cache-dir . \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R o+rX /ms-playwright
COPY src ./src
RUN pip install --no-cache-dir --no-deps --force-reinstall .
# Unraid's nobody:users, so files in /data stay editable from the share
RUN useradd --uid 99 --gid 100 --no-create-home --shell /usr/sbin/nologin itemizer

ENV DATA_DIR=/data PYTHONUNBUFFERED=1 HOME=/tmp
VOLUME /data
EXPOSE 8686
USER 99:100
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"WEB_PORT\",\"8686\")}/healthz')"
ENTRYPOINT ["tini", "--", "itemizer"]
CMD ["serve"]
