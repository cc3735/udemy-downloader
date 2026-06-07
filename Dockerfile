# udemy-downloader (cc3735 fork) — bundle Dockerfile.
#
# Differences from upstream:
#   - Adds pywidevine + pycryptodome so scripts/get_udemy_keys.py can drive
#     the user's existing Widevine L3 CDM (mounted at /cdm/widevine.wvd by
#     docker-compose.yml) to populate keyfile.json without manual key entry.
#   - Adds Bento4 mp4decrypt as a fallback / verify tool (upstream uses
#     ffmpeg -decryption_key for the actual decrypt; mp4decrypt is here so
#     the sidecar can sanity-check a freshly-fetched KID:KEY pair against
#     the encrypted MP4 it came from).
#   - Adds libicu-dev (needed by N_m3u8DL-RE on Debian; harmless even
#     though we don't currently use N_m3u8DL-RE here).
#
# Upstream maintains the rest of this file (johnvansickle ffmpeg + shaka).
FROM python:3.12-slim-bullseye

WORKDIR /app

RUN apt-get update && apt-get install -y \
    curl \
    wget \
    aria2 \
    unzip \
    xz-utils \
    jq \
    libicu-dev \
    && rm -rf /var/lib/apt/lists/*

# Install FFmpeg from johnvansickle's builds (upstream recipe verbatim).
RUN wget https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz \
    && tar xvf ffmpeg-release-amd64-static.tar.xz \
    && mv ffmpeg-*-amd64-static/ffmpeg /usr/local/bin/ \
    && mv ffmpeg-*-amd64-static/ffprobe /usr/local/bin/ \
    && rm -rf ffmpeg-*-amd64-static* \
    && chmod +x /usr/local/bin/ffmpeg \
    && chmod +x /usr/local/bin/ffprobe

# Install Shaka Packager (upstream recipe verbatim — checked by main.py at
# startup; not actively used for decryption).
RUN LATEST_TAG=$(curl -s https://api.github.com/repos/shaka-project/shaka-packager/releases/latest | jq -r .tag_name) && \
    wget https://github.com/shaka-project/shaka-packager/releases/download/$LATEST_TAG/packager-linux-x64 -O /usr/local/bin/shaka-packager && \
    chmod +x /usr/local/bin/shaka-packager && \
    echo "Shaka Packager version $LATEST_TAG installed."

# Install Bento4 mp4decrypt (HA-ripper recipe; fallback / sidecar verify).
RUN curl -sL "https://www.bok.net/Bento4/binaries/Bento4-SDK-1-6-0-641.x86_64-unknown-linux.zip" \
        -o /tmp/bento4.zip \
    && unzip -q /tmp/bento4.zip -d /tmp/ \
    && cp /tmp/Bento4-SDK-*/bin/mp4decrypt /usr/local/bin/ \
    && chmod +x /usr/local/bin/mp4decrypt \
    && rm -rf /tmp/bento4.zip /tmp/Bento4-SDK-*

# Copy the current directory contents into the container at /app.
COPY . /app

# Upstream Python deps + bundle additions (pywidevine drives the CDM in
# scripts/get_udemy_keys.py; pycryptodome is a pywidevine transitive that
# is faster than the pure-python fallback).
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir pywidevine pycryptodome
