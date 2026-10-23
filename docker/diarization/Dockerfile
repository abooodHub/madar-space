FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/model-cache \
    XSA_DATA_DIR=/data \
    XSA_DB_PATH=/state/xsa.sqlite3 \
    PYTHONPATH=/app/backend

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libsndfile1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY docker/diarization/requirements-worker.txt /tmp/requirements-worker.txt

# CUDA-enabled wheels stay inside this disposable worker image. The XSA app
# image and its package environment are not changed by this install.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cu126 \
      torch==2.8.0 torchaudio==2.8.0 \
    && pip install --no-cache-dir -r /tmp/requirements-worker.txt

COPY backend/xsa/diarization.py /app/backend/xsa/diarization.py
COPY backend/xsa/paths.py /app/backend/xsa/paths.py
COPY backend/xsa/artifact_lock.py /app/backend/xsa/artifact_lock.py
COPY backend/xsa/worker_status.py /app/backend/xsa/worker_status.py
COPY backend/xsa/__init__.py /app/backend/xsa/__init__.py
COPY backend/scripts/diarization_worker.py /app/backend/scripts/diarization_worker.py
RUN python /app/backend/scripts/diarization_worker.py --help

RUN mkdir -p /model-cache /data /state
VOLUME ["/model-cache"]
ENTRYPOINT ["python", "/app/backend/scripts/diarization_worker.py"]
CMD ["run"]
