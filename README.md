# AlbumDL

AlbumDL is a webserver that allows you to download albums from YouTube Music without an account. It is meant to run on servers for quick downloading and catalouging.

It uses MusicBrainz public database to find metadata about songs and for grabbing the cover art.

Albums are saved straight into one folder as `Artist/Album/## - Title.mp3` plus `cover.jpg` in there too.

## Run with Docker

Make sure to edit `.env` once you make it
```sh
cp .env.example .env

sudo docker compose up -d --build
```

Open `http://<server-ip>:8080`. There is no login, so keep it on your home network.
Edit `docker-compose.yml` to change the number of parallel downloads. Set `CONTACT` in `.env` to your email or a URL so MusicBrainz can identify the app.

To update yt-dlp, rebuild: `docker compose build --no-cache && docker compose up -d`.

## Run without Docker

Needs Python 3.12, `ffmpeg` and `deno` (or `node`).

```sh
python3 -m venv .venv && .venv/bin/pip install .

OUTPUT_DIR=./out .venv/bin/albumdl
```

## How to use

- Search an artist to see their albums, or type `Artist - Album` to find one release. You can also paste a MusicBrainz link.
- Artist pages show **Similar** artists. Tap one to open it. Artists you already have a folder for get a check mark, and artist photos come from Deezer. Add `LASTFM_KEY=<your key>` to `.env` to use Last.fm (a free API key from last.fm/api); without it the list comes from ListenBrainz.
- Albums already in the folder are marked **In folder**.
- Tap albums, EPs or singles to select them (or **Select all** for a discography), then press **Download**. Downloading an album that is already there replaces it.
- Select albums marked **In folder** and press **Delete** (twice, to confirm) to remove their folders from disk. Navidrome keeps deleted albums as empty "missing" entries until you remove them in its Missing Files page, or set `ND_SCANNER_PURGEMISSING=full` on the Navidrome container.
- **Find missing covers** (bottom of the Queue) scans the music folder for albums without a `cover.jpg`, looks the cover up again and embeds it. Results are in the Log. Navidrome shows the new covers after its next scan.
- **Check incomplete** (bottom of the Queue) lists albums with missing tracks and can retry them all. Albums already queued are skipped, and folders with a slightly different name still count as **In folder**.
- If a track still fails after many retries, the album folder gets an `INCOMPLETE.txt` listing what is missing, and the album is tagged **Incomplete**.
- `Queue` shows progress, with the tracks of each album inside its row. `Log` shows the server log.
- If no official upload exists for a track, the best matching upload is used and marked **Unofficial upload** in the queue.
