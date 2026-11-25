# syntax=docker/dockerfile:1
FROM python:3.12.10-slim-bookworm

WORKDIR /srv/walkwithdog

COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --disable-pip-version-check -r requirements.txt

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
    && install -d -o app -g app -m 0750 /srv/uploads /srv/uploads/dogs

COPY app/ ./app/
COPY policies/ ./policies/

USER app:app
EXPOSE 8000

CMD ["python", "-B", "-u", "-c", "import uvicorn; uvicorn.run('app.main:app', host='0.0.0.0', port=8000, workers=1, reload=False)"]
