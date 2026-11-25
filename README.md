<p align="center"><img src="docs/banner.gif" alt="AlbumDL" width="640"></p>

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
| `LASTFM_KEY` | Optional, adds Last.fm to similar artists (Deezer and ListenBrainz are always used) |

Parallel downloads are set by `ALBUMDL_JOBS` in `docker-compose.yml`. To update yt-dlp, run `docker compose build --no-cache && docker compose up -d`.

## Run without Docker

Needs Python 3.12, `ffmpeg` and `deno` (or `node`).

```sh
python3 -m venv .venv && .venv/bin/pip install .
OUTPUT_DIR=./out .venv/bin/albumdl
```

## Using it

- **Search** takes an artist, `Artist - Album` (any word order), or a MusicBrainz or YouTube link. Tap albums to select them, then **Download**.
- **Artist pages** show the artist's popular songs, their most loved releases, the full discography and **Fans also like** (with a **Deeper cuts** tab). Rows scroll sideways with a trackpad, Shift+wheel, the arrow buttons, or by dragging with the mouse; **See all** shows a row as a grid.
- **Single songs:** download a song from **Popular**, or open an album's **Songs** and pick the ones you want. Songs are saved into their album's folder (`Artist/Album/## - Title.mp3`) like any album. Downloading the whole album later only fetches the songs that are missing; **Download again** on an album you already have replaces it.
- **Not on MusicBrainz?** It falls back to YouTube Music, then to YouTube playlists and videos. Paste a YouTube link to import unofficial music; full-album videos are split by chapter.
- **Discover** opens with a map of your library: each artist is a dot placed by the genres in its files, and genre families are coloured areas that blend where styles meet (hip-hop beside R&B beside soul, rock beside prog and punk, pop in the middle). Drag the circle, change its size, or tap a genre to pick the part of your library to explore; **Whole library** uses everything. Below it Discover recommends albums and artists from that area: picks for you, rails for each artist you have, artists you might like, lesser-known ones under the radar, and albums missing from artists you already have. **Shuffle** picks different seeds. Select any album and tap **More like this** for albums like it, close in era and genre. Artist pages show **Fans also like**, with a **Deeper cuts** tab.
- **Library** lists what you have. Select albums to delete them, retry incomplete ones, edit tags and covers, or link a YouTube video for a missing track. **Scan library** looks up missing lyrics or covers for every album in the background.
- **Lyrics** are looked up on [LRCLIB](https://lrclib.net) during each download and saved next to the song as a `.lrc` file. Existing `.lrc` files are kept.
- **Export** copies albums (selected ones, or everything from **Tools**) to a folder such as an MP3 player, skipping files that are already there with the same checksum. Copying straight to a folder needs Chrome or Edge on `https` or `localhost`; elsewhere AlbumDL compares the player's folder and downloads a ZIP of only what is new.
- **Queue** shows progress and **Log** shows the server log.

## Where data comes from

Higher sources win; lower ones only fill what is empty.

- **Albums:** MusicBrainz, then YouTube Music
- **Tags:** MusicBrainz, then Deezer, iTunes, Last.fm
- **Lyrics:** LRCLIB, or a file you add
- **Covers:** Cover Art Archive, then iTunes and Deezer, then YouTube Music
- **Audio:** your pasted link, the official album playlist, an official upload, then the best unofficial one

## Credits

The background art is pixel art based on [this reference image](https://pin.it/3ZOsQX7u2).
