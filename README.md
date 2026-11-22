# AlbumDL

AlbumDL is a webserver that allows you to download official music from YouTube Music without an account. It is meant to run on servers for quick downloading and catalouging.

It uses MusicBrainz public database to find metadata about songs and for grabbing the cover art.

Albums are saved straight into one folder as `Artist - Album/## - Title.mp3` plus `cover.jpg` in there too.

## Run with Docker

Make sure to edit `.env` once you make it
```sh
cp .env.example .env

sudo docker compose up -d --build
```

Open `http://<server-ip>:8080`. There is no login, so keep it on your home network.
Edit `docker-compose.yml` to change the format (`mp3`, `m4a`, `opus`) or the number of parallel downloads.

To update yt-dlp, rebuild: `docker compose build --no-cache && docker compose up -d`.

## Run without Docker

Needs Python 3.12, `ffmpeg` and `deno` (or `node`).

```sh
python3 -m venv .venv && .venv/bin/pip install .

OUTPUT_DIR=./out .venv/bin/albumdl
```

## How to use

- Search an artist to see their albums, or an artist and album to find one release. You can also paste a MusicBrainz link.
- Albums already in the folder are marked **In folder**.
- You can download an artist's entire discography.
- `Queue` shows progress and `Console` shows the log.
