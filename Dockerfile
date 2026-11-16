FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    IMAGEIO_FFMPEG_EXE=/usr/bin/ffmpeg \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/cache

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 tapo \
    && useradd --uid 10001 --gid tapo --no-create-home tapo \
    && mkdir -p /data/captures \
    && chown -R tapo:tapo /data

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install . \
    && python -m pip check \
    && python -c "import imageio_ffmpeg; assert imageio_ffmpeg.get_ffmpeg_exe() == '/usr/bin/ffmpeg'" \
    && ffmpeg -version \
    && chmod -R a+rX /app

USER 10001:10001
RUN tapo-watch --help \
    && test -w /data/captures
STOPSIGNAL SIGINT
ENTRYPOINT ["tapo-watch"]
CMD ["telegram", "--output", "/data/captures"]
