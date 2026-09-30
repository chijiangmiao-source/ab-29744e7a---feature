FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=0 \
    HOST=0.0.0.0 \
    PORT=8080 \
    DATA_DIR=/data

WORKDIR /srv

# Application + bundled tests and sample images. Pure standard library:
# no pip install step is required.
COPY app/ ./app/
COPY tests/ ./tests/
COPY data/ ./data/

RUN mkdir -p /data \
    && useradd --create-home --uid 10001 auditor \
    && chown -R auditor:auditor /data /srv

USER auditor

EXPOSE 8080

HEALTHCHECK --interval=5s --timeout=3s --start-period=3s --retries=10 \
    CMD python -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2); sys.exit(0 if r.status==200 else 1)"

CMD ["python", "-m", "app.serve"]
