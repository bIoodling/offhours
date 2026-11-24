FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt main.py ./
RUN python main.py --install-only
COPY . .
RUN mkdir -p /app/data && useradd --uid 10001 --no-create-home bot && chown -R bot:bot /app
USER bot
ENV OFFHOURS_DB_PATH=/app/data/offhours.sqlite3
VOLUME ["/app/data"]
STOPSIGNAL SIGTERM
CMD ["python", "main.py"]
