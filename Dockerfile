# Say the Line in one container: ffmpeg (with libass), faster-whisper, spaCy.
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fontconfig \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m spacy download en_core_web_sm

COPY saytheline ./saytheline
COPY saytheline.example.toml ./
COPY tests ./tests

# Index, previews, clips, library and the downloaded speech model all live in /data.
ENV SAYTHELINE_DATA=/data \
    HF_HOME=/data/models \
    SAYTHELINE_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8765

CMD ["python", "-m", "saytheline", "serve"]
