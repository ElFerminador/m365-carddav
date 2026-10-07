FROM python:3.12-slim

LABEL org.opencontainers.image.title="m365-carddav" \
      org.opencontainers.image.description="One-way sync of Microsoft 365 contacts to a built-in CardDAV server (Radicale)" \
      org.opencontainers.image.source="https://github.com/ElFerminador/m365-carddav" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    PATH="/app/bin:${PATH}"

RUN apt-get update \
 && apt-get install -y --no-install-recommends tini tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/
RUN chmod 755 /app/entrypoint.sh /app/bin/*

EXPOSE 5232 5233
WORKDIR /app
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/entrypoint.sh"]
