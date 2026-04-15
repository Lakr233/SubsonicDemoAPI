# SubsonicDemoAPI

A self-contained Subsonic demo server built from a prepared Suno music library.

The image bundles:

- 2 albums
- 7 tracks
- embedded cover art assets
- `.lrc` sidecar lyrics
- a lightweight Python Subsonic-compatible API

## Included Library

- Artist: `Suno`
- Album: `Suno Review Album One` with 4 tracks
- Album: `Suno Review Album Two` with 3 tracks

## What Works

Subsonic endpoints:

- `GET /rest/ping.view`
- `GET /rest/getArtists.view`
- `GET /rest/getArtist.view`
- `GET /rest/getAlbumList2.view`
- `GET /rest/getMusicDirectory.view`
- `GET /rest/search3.view`
- `GET /rest/getAlbum.view`
- `GET /rest/getSong.view`
- `GET /rest/getLyrics.view`
- `GET /rest/getLyricsBySongId.view`
- `GET /rest/getCoverArt.view`
- `GET /rest/stream.view`
- `GET /rest/download.view`

Direct utility endpoints:

- `GET /search`
- `GET /album/<id>`
- `GET /song/<id>`
- `GET /lyrics/<id>`
- `GET /playback/<id>`
- `GET /health`

## Quick Start

Run with Docker:

```bash
docker run --rm -p 8080:8080 \
  -e SUBSONIC_USERNAME=admin \
  -e SUBSONIC_PASSWORD=admin123 \
  ghcr.io/lakr233/subsonicdemoapi:latest
```

Or run locally with Compose:

```bash
docker compose up -d --build
```

The server listens on:

- `http://127.0.0.1:8080`

Default credentials:

- Username: `admin`
- Password: `admin123`

## MuseAmp Example

Base URL:

```text
http://127.0.0.1:8080
```

Ping:

```bash
curl 'http://127.0.0.1:8080/rest/ping.view?u=admin&p=admin123&f=json&v=1.16.1&c=MuseAmp'
```

Album list:

```bash
curl 'http://127.0.0.1:8080/rest/getAlbumList2.view?u=admin&p=admin123&size=10&offset=0'
```

## GitHub Container Registry

GitHub Actions publishes images to:

```text
ghcr.io/lakr233/subsonicdemoapi
```

Published tags:

- `latest` on `main`
- branch refs
- git tags
- short SHA tags

## Repository Layout

```text
albums/                 bundled music library and manifest
api/server.py           Subsonic demo server
api/Dockerfile          image build definition
compose.yml             local compose entrypoint
.github/workflows/      CI and GHCR publish workflow
```
