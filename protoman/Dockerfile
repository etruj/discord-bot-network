FROM python:3.12-slim-bookworm

# xvfb + the GL libraries: OpenSCAD's 3D preview images. admesh: mesh check.
RUN apt-get update && apt-get install -y --no-install-recommends \
        admesh binutils ca-certificates curl fontconfig fonts-dejavu-core \
        libegl1 libgl1 libgl1-mesa-dri libglib2.0-0 libglu1-mesa libharfbuzz0b libopengl0 \
        libxcb-cursor0 libxkbcommon0 xauth xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY scripts/install_openscad.sh /tmp/install_openscad.sh
RUN sh /tmp/install_openscad.sh /opt/openscad /usr/local/bin && rm /tmp/install_openscad.sh

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY protoman.py cards.py parts.py engine.py ./
COPY engine ./engine
COPY tests ./tests

# Everything the bot writes defaults to under $HOME, so this one volume holds
# parts, log and state. Run as a normal user: the bot never needs root.
RUN useradd --uid 1000 --home-dir /data --no-create-home protoman \
    && mkdir /data && chown protoman /data
USER protoman
ENV HOME=/data PYTHONUNBUFFERED=1
VOLUME /data

CMD ["python3", "protoman.py"]
