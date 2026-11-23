# AlbumDL

A small web app that finds albums on MusicBrainz and downloads them from YouTube as tagged MP3s, no account needed. Albums are saved as `Artist/Album/## - Title.mp3` with a `cover.jpg`.

## Run with Docker

```sh
cp .env.example .env    # then edit it
sudo docker compose up -d --build
```

Open `http://<server-ip>:8080`. There is no login, so keep it on your home network.

| `.env` | Meaning |
| --- | --- |
| `MUSIC_DIR` | Folder the albums are saved to |
| `PORT` | Web port (default 8080) |
| `CONTACT` | Your email or a URL, so MusicBrainz doesn't throttle you |
| `LASTFM_KEY` | Optional, for similar artists (falls back to ListenBrainz) |

Parallel downloads are set by `ALBUMDL_JOBS` in `docker-compose.yml`. To update yt-dlp, run `docker compose build --no-cache && docker compose up -d`.

## Run without Docker

Needs Python 3.12, `ffmpeg` and `deno` (or `node`).

```sh
python3 -m venv .venv && .venv/bin/pip install .
OUTPUT_DIR=./out .venv/bin/albumdl
```

## Using it

- **Search** takes an artist, `Artist - Album` (any word order), or a MusicBrainz or YouTube link. Tap albums to select them, then **Download**.
- **Not on MusicBrainz?** It falls back to YouTube Music, then to YouTube playlists and videos. Paste a YouTube link to import unofficial music; full-album videos are split by chapter.
- **Library** lists what you have. Select albums to delete them, retry incomplete ones, edit tags and covers, or link a YouTube video for a missing track.
- **Queue** shows progress and **Log** shows the server log.

## Where data comes from

Higher sources win; lower ones only fill what is empty.

- **Albums:** MusicBrainz, then YouTube Music
- **Tags:** MusicBrainz, then Deezer, iTunes, Last.fm
- **Covers:** Cover Art Archive, then iTunes and Deezer, then YouTube Music
- **Audio:** your pasted link, the official album playlist, an official upload, then the best unofficial one
