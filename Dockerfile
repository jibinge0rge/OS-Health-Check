FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_PORT=8000 \
    UVICORN_RELOAD=false

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY docker/entrypoint.sh /entrypoint.sh
COPY docker/import_if_empty.py /import_if_empty.py
RUN sed -i 's/\r$//' /entrypoint.sh \
    && chmod +x /entrypoint.sh

COPY . .

# _data/ ships baked-in (the seed CSV -- see docker/import_if_empty.py);
# _config/ starts empty and is where persisted Settings get written.
RUN mkdir -p _data _config

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
