# Build the complete application image directly so `docker compose up --build`
# does not depend on a locally prebuilt xsa-capture image.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=UTC \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc websockify supervisor python3-venv \
        pulseaudio pulseaudio-utils ffmpeg \
        fonts-noto-core fonts-noto-ui-core fonts-noto-color-emoji fonts-noto-cjk \
        fonts-kacst fonts-arabeyes \
        xdotool x11-utils procps tini curl ca-certificates \
    && ln -s /usr/share/novnc/vnc.html /usr/share/novnc/index.html \
    && rm -rf /var/lib/apt/lists/*

COPY docker/capture/pulse-default.pa /etc/xsa/pulse-default.pa
COPY docker/capture/supervisord.conf /etc/xsa/supervisord.conf
COPY docker/app/supervisord-api.conf /etc/xsa/conf.d/supervisord-api.conf
COPY docker/capture/entrypoint.sh /usr/local/bin/xsa-entrypoint
COPY docker/app/requirements.lock /tmp/app-requirements.lock

RUN sed -i 's/\r$//' /usr/local/bin/xsa-entrypoint /etc/xsa/*.conf /etc/xsa/*.pa \
    && chmod +x /usr/local/bin/xsa-entrypoint \
    && mkdir -p /data /profile /state /app /tmp/pulse /etc/xsa/conf.d \
    && chown -R pwuser:pwuser /data /profile /state /app /tmp/pulse /etc/xsa/conf.d

ENV DISPLAY=:99 \
    PULSE_SERVER=unix:/tmp/pulse/native \
    PULSE_SINK=xsa_ui \
    XSA_PULSE_SINK=xsa_ui \
    XSA_RECORD_SIZE=720x1280 \
    XSA_DATA_DIR=/data \
    XSA_DB_PATH=/state/xsa.sqlite3 \
    XSA_PROFILE_DIR=/profile \
    HOME=/home/pwuser

ENV VIRTUAL_ENV=/opt/xsa-venv
ENV PATH="/opt/xsa-venv/bin:$PATH"
RUN python -m venv /opt/xsa-venv \
    && pip install --no-cache-dir --require-hashes -r /tmp/app-requirements.lock \
    && pip check
# Chrome Stable includes the AAC/H.264 codecs used by X Spaces.
RUN playwright install chrome && google-chrome --version

COPY --chown=pwuser:pwuser backend /app/backend
WORKDIR /app/backend
RUN pip install --no-cache-dir --no-deps -e . && pip check

USER pwuser
EXPOSE 6080 8765
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/xsa-entrypoint"]
CMD ["supervisord", "-n", "-c", "/etc/xsa/supervisord.conf"]
