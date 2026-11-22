FROM denoland/deno:bin AS deno
FROM mwader/static-ffmpeg:7.1 AS ffmpeg

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 HOME=/tmp OUTPUT_DIR=/music ALBUMDL_HOST=0.0.0.0
COPY --from=deno /deno /usr/local/bin/deno
COPY --from=ffmpeg /ffmpeg /ffprobe /usr/local/bin/
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src src
RUN pip install --no-cache-dir .
CMD ["albumdl"]
