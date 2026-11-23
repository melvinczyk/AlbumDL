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
- If MusicBrainz has no match (new or small artists), the search falls back to YouTube Music, and a **Search YouTube Music** button sits under every MusicBrainz result list. Those albums get basic tags (artist, album, year, track numbers, cover) and no MusicBrainz IDs.
- **Unofficial uploads** (game soundtracks, fan uploads): paste a YouTube playlist or video link into the search box. AlbumDL lists the videos, strips the album name and track numbers from the titles, and lets you edit the artist, album, year, genre and each title before downloading. A single video with chapters (a full album upload) is split into one track per chapter; the video is downloaded once. The cover is the video thumbnail, cropped square.
- Select a single album and an **Edition** menu appears when it has other editions (deluxe, anniversary, bonus tracks). The standard edition is the default. A non-standard edition is saved as `Title (edition name)`, so it sits next to the standard one instead of replacing it.
- Albums already in the folder are marked **In folder**.
- Tap albums, EPs or singles to select them (or **Select all** for a discography), then press **Download**. Downloading an album that is already there replaces it.
- Select albums marked **In folder** and press **Delete** (twice, to confirm) to remove their folders from disk. Navidrome keeps deleted albums as empty "missing" entries until you remove them in its Missing Files page, or set `ND_SCANNER_PURGEMISSING=full` on the Navidrome container.
- **Find missing covers** (in the Library) scans the music folder for albums without a `cover.jpg`, looks the cover up again and embeds it. Results are in the Log. Navidrome shows the new covers after its next scan.
- **Library** lists every album in the music folder with its tracks, size, age and whether it is incomplete or has no cover. Tap albums to select them, then **Delete** (twice, to confirm) or **Retry** the incomplete ones. Folders with a slightly different name still count as **In folder** in Search.
- If a track still fails after many retries, the album folder gets an `INCOMPLETE.txt` listing what is missing, and the album is tagged **Incomplete**.
- `Queue` shows progress, with the tracks of each album inside its row. `Log` shows the server log.
- If no official upload exists for a track, the best matching upload is used and marked **Unofficial upload** in the queue.

## Where data comes from

Higher entries win. A lower source only fills what is still empty and never overwrites.

```
Finding albums
 1. MusicBrainz (artist and album search)
 2. YouTube Music (when MusicBrainz has no match)

Tags for each album
 1. MusicBrainz release          (or YouTube Music when MusicBrainz has none)
 2. Deezer                       genres, label, barcode, full date, type
 3. iTunes                       genres, full date
 4. Last.fm (needs a key)        genres
 The download log shows what was filled: "metadata filled: genres from Deezer".

Cover art (the largest image found wins)
 1. Cover Art Archive (release, then release group)
 2. iTunes and Deezer            searched at the same time
 3. YouTube Music                last resort

Audio for each track
 0. A YouTube link you pasted (the import form)
 1. Official YouTube Music album playlist
 2. Official upload (label-delivered Topic track, verified artist or label channel)
 3. Best matching unofficial upload, marked in the Queue

Similar artists: Last.fm (needs a key), then ListenBrainz
Artist photos: Deezer
```
