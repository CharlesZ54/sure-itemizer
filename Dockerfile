FROM python:3.12-slim

# tzdata so RUN_TIMES follow the TZ Unraid passes in
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
# Unraid's nobody:users, so files in /data stay editable from the share
RUN useradd --uid 99 --gid 100 --no-create-home --shell /usr/sbin/nologin itemizer

ENV DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8686
USER 99:100
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"WEB_PORT\",\"8686\")}/healthz')"
ENTRYPOINT ["itemizer"]
CMD ["serve"]
