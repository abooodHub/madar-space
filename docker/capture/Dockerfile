# XSpace Analyzer - capture/app runtime image.
#
# One container holds everything that has to share an X display and an
# audio server: Xvfb (virtual display), PulseAudio (isolated null sink),
# Google Chrome (driven by Playwright), FFmpeg (recorder) and noVNC (so the
# user can log in to X interactively). These are deliberately not split into
# microservices: they share a display/audio namespace and fail together.
#
# Base: official Playwright Python image (Ubuntu 24.04 "noble").
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=UTC \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc websockify supervisor \
        pulseaudio pulseaudio-utils \
        ffmpeg \
        fonts-noto-core fonts-noto-ui-core fonts-noto-color-emoji fonts-noto-cjk \
        fonts-kacst fonts-arabeyes \
        xdotool x11-utils procps tini curl ca-certificates \
    && ln -s /usr/share/novnc/vnc.html /usr/share/novnc/index.html \
    && rm -rf /var/lib/apt/lists/*

COPY docker/capture/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --break-system-packages playwright==1.63.0 -r /tmp/requirements.txt

# Google Chrome stable (has the proprietary AAC/H.264 codecs needed by X Spaces
# HLS audio; Playwright's bundled Chromium does not).
RUN playwright install chrome && google-chrome --version

COPY docker/capture/pulse-default.pa /etc/xsa/pulse-default.pa
COPY docker/capture/supervisord.conf /etc/xsa/supervisord.conf
COPY docker/capture/entrypoint.sh /usr/local/bin/xsa-entrypoint
RUN sed -i 's/\r$//' /usr/local/bin/xsa-entrypoint /etc/xsa/*.conf /etc/xsa/*.pa \
    && chmod +x /usr/local/bin/xsa-entrypoint \
    && mkdir -p /data /profile /app /tmp/pulse /etc/xsa/conf.d \
    && chown -R pwuser:pwuser /data /profile /app /tmp/pulse

ENV DISPLAY=:99 \
    XSA_SCREEN=1280x720x24 \
    PULSE_SERVER=unix:/tmp/pulse/native \
    PULSE_SINK=xspace \
    XSA_PULSE_SINK=xspace \
    HOME=/home/pwuser

USER pwuser
WORKDIR /app

EXPOSE 6080 8000
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/xsa-entrypoint"]
CMD ["supervisord", "-n", "-c", "/etc/xsa/supervisord.conf"]
