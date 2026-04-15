#!/usr/bin/env python3

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import posixpath
import re
import shutil
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent.parent
LIBRARY_ROOT = Path(os.environ.get("LIBRARY_ROOT", str(ROOT / "albums"))).resolve()
MANIFEST_PATH = Path(
    os.environ.get("LIBRARY_MANIFEST_PATH", str(LIBRARY_ROOT / "library_manifest.json"))
).resolve()
SUBSONIC_VERSION = "1.16.1"
SERVER_NAME = "my-album-subsonic"
SERVER_TYPE = "my-album"
ARTIST_ID = "artist-suno"
ROOT_DIRECTORY_ID = "root"


def slugify(value: str) -> str:
    lowered = value.strip().lower()
    lowered = re.sub(r"[^a-z0-9]+", "-", lowered)
    return lowered.strip("-") or "item"


def first(values: dict[str, list[str]], key: str, default: str = "") -> str:
    return values.get(key, [default])[0]


def year_from_manifest_timestamp(value: str) -> int:
    if len(value) < 4:
        return 0
    try:
        return int(value[:4])
    except ValueError:
        return 0


def resolve_manifest_path(raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.exists():
        return candidate.resolve()
    if not candidate.is_absolute():
        return (LIBRARY_ROOT / candidate).resolve()
    parts = list(candidate.parts)
    if "albums" in parts:
        album_index = parts.index("albums")
        return (LIBRARY_ROOT / Path(*parts[album_index + 1 :])).resolve()
    return candidate.resolve()


def decode_subsonic_password(raw_password: str) -> str:
    if raw_password.startswith("enc:"):
        return bytes.fromhex(raw_password[4:]).decode("utf-8")
    return raw_password


@dataclass(frozen=True)
class Track:
    id: str
    title: str
    album_id: str
    album_name: str
    artist_id: str
    artist_name: str
    track_number: int
    track_total: int
    duration_seconds: int
    audio_path: Path
    lyrics_path: Path
    cover_path: Path
    year: int

    @property
    def suffix(self) -> str:
        return self.audio_path.suffix.lower().lstrip(".")

    @property
    def content_type(self) -> str:
        guessed, _ = mimetypes.guess_type(self.audio_path.name)
        return guessed or "application/octet-stream"

    @property
    def size(self) -> int:
        return self.audio_path.stat().st_size

    def song_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parent": self.album_id,
            "isDir": False,
            "title": self.title,
            "album": self.album_name,
            "artist": self.artist_name,
            "track": self.track_number,
            "discNumber": 1,
            "year": self.year,
            "genre": "AI Music",
            "coverArt": self.album_id,
            "size": self.size,
            "contentType": self.content_type,
            "suffix": self.suffix,
            "duration": self.duration_seconds,
            "bitRate": 0,
            "path": posixpath.join(self.album_name, self.audio_path.name),
            "isVideo": False,
            "albumId": self.album_id,
            "artistId": self.artist_id,
            "type": "music",
        }


@dataclass(frozen=True)
class Album:
    id: str
    name: str
    artist_id: str
    artist_name: str
    cover_path: Path
    year: int
    tracks: list[Track]

    def payload(self, include_songs: bool) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "parent": self.artist_id,
            "isDir": True,
            "title": self.name,
            "name": self.name,
            "album": self.name,
            "artist": self.artist_name,
            "artistId": self.artist_id,
            "coverArt": self.id,
            "songCount": len(self.tracks),
            "duration": sum(track.duration_seconds for track in self.tracks),
            "created": "",
            "year": self.year,
            "genre": "AI Music",
        }
        if include_songs:
            payload["song"] = [track.song_payload() for track in self.tracks]
        return payload


@dataclass(frozen=True)
class Artist:
    id: str
    name: str
    albums: list[Album]

    def payload(self, include_albums: bool) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "name": self.name,
            "albumCount": len(self.albums),
        }
        if include_albums:
            payload["album"] = [album.payload(include_songs=False) for album in self.albums]
        return payload


class Library:
    def __init__(self, manifest_path: Path) -> None:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_year = year_from_manifest_timestamp(raw.get("prepared_at", ""))
        artist_name = raw.get("artist", "Suno")
        album_entries = raw.get("albums", [])
        track_entries = raw.get("tracks", [])

        albums_by_name: dict[str, Album] = {}
        album_order: list[str] = []
        album_cover_by_name = {
            item["name"]: resolve_manifest_path(item["cover_file"])
            for item in album_entries
        }

        tracks_by_album: dict[str, list[Track]] = {}
        self.tracks_by_id: dict[str, Track] = {}
        for index, item in enumerate(track_entries, start=1):
            album_name = item["album"]
            if album_name not in album_order:
                album_order.append(album_name)
            album_id = f"album-{slugify(album_name)}"
            track_id = f"track-{index:02d}"
            track = Track(
                id=track_id,
                title=item["prepared_title"],
                album_id=album_id,
                album_name=album_name,
                artist_id=ARTIST_ID,
                artist_name=item.get("artist", artist_name),
                track_number=int(item["track_number"]),
                track_total=int(item["track_total"]),
                duration_seconds=round(float(item["duration_seconds"])),
                audio_path=resolve_manifest_path(item["album_link"]),
                lyrics_path=resolve_manifest_path(item["lyrics_sidecar"]),
                cover_path=album_cover_by_name[album_name],
                year=manifest_year,
            )
            tracks_by_album.setdefault(album_name, []).append(track)
            self.tracks_by_id[track.id] = track

        self.albums_by_id: dict[str, Album] = {}
        albums: list[Album] = []
        for album_name in album_order:
            album_id = f"album-{slugify(album_name)}"
            album = Album(
                id=album_id,
                name=album_name,
                artist_id=ARTIST_ID,
                artist_name=artist_name,
                cover_path=album_cover_by_name[album_name],
                year=manifest_year,
                tracks=sorted(tracks_by_album.get(album_name, []), key=lambda track: track.track_number),
            )
            albums_by_name[album_name] = album
            self.albums_by_id[album.id] = album
            albums.append(album)

        self.artist = Artist(id=ARTIST_ID, name=artist_name, albums=albums)
        self.directory_children = {
            ROOT_DIRECTORY_ID: [self.artist.payload(include_albums=False)],
            self.artist.id: [album.payload(include_songs=False) for album in self.artist.albums],
        }
        for album in self.artist.albums:
            self.directory_children[album.id] = [track.song_payload() for track in album.tracks]

    def resolve_cover(self, item_id: str) -> Path:
        if item_id in self.albums_by_id:
            return self.albums_by_id[item_id].cover_path
        if item_id in self.tracks_by_id:
            return self.tracks_by_id[item_id].cover_path
        raise KeyError(item_id)

    def resolve_lyrics(self, item_id: str) -> tuple[Track, str]:
        track = self.tracks_by_id[item_id]
        return track, track.lyrics_path.read_text(encoding="utf-8").strip()

    def search(self, query: str) -> tuple[list[Track], list[Album], list[Artist]]:
        query_lower = query.strip().lower()
        matched_tracks = [
            track
            for track in self.tracks_by_id.values()
            if query_lower in track.title.lower()
            or query_lower in track.album_name.lower()
            or query_lower in track.artist_name.lower()
        ]
        matched_albums = [
            album
            for album in self.albums_by_id.values()
            if query_lower in album.name.lower() or query_lower in album.artist_name.lower()
        ]
        matched_artists = [self.artist] if query_lower in self.artist.name.lower() else []
        return matched_tracks, matched_albums, matched_artists

    def find_track(
        self,
        *,
        track_id: str = "",
        song_id: str = "",
        title: str = "",
        artist: str = "",
    ) -> Track | None:
        candidate_ids = [track_id.strip(), song_id.strip()]
        for candidate_id in candidate_ids:
            if candidate_id and candidate_id in self.tracks_by_id:
                return self.tracks_by_id[candidate_id]

        normalized_title = title.strip().lower()
        normalized_artist = artist.strip().lower()
        if normalized_title:
            for track in self.tracks_by_id.values():
                title_matches = (
                    normalized_title == track.title.lower()
                    or normalized_title in track.title.lower()
                )
                artist_matches = (
                    not normalized_artist
                    or normalized_artist == track.artist_name.lower()
                    or normalized_artist in track.artist_name.lower()
                )
                if title_matches and artist_matches:
                    return track
        return None

    def direct_search_payload(self, query: str, search_type: str, limit: int, offset: int) -> dict[str, Any]:
        songs, albums, artists = self.search(query)
        if search_type == "song":
            return {
                "results": {
                    "songs": {
                        "data": [track.song_payload() for track in songs][offset : offset + limit],
                    }
                }
            }
        if search_type == "album":
            return {
                "results": {
                    "albums": {
                        "data": [album.payload(include_songs=False) for album in albums][offset : offset + limit],
                    }
                }
            }
        return {
            "results": {
                "artists": {
                    "data": [artist.payload(include_albums=False) for artist in artists][offset : offset + limit],
                }
            }
        }


LIBRARY = Library(MANIFEST_PATH)


class SubsonicHandler(BaseHTTPRequestHandler):
    server_version = "MyAlbumSubsonic/1.0"

    def do_GET(self) -> None:
        self.handle_request(send_body=True)

    def do_HEAD(self) -> None:
        self.handle_request(send_body=False)

    def handle_request(self, *, send_body: bool) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query, keep_blank_values=True)
        route = parsed.path

        try:
            if route == "/":
                self.write_json({"ok": True, "service": SERVER_NAME, "libraryRoot": str(LIBRARY_ROOT)}, send_body=send_body)
                return
            if route == "/health":
                self.write_json({"ok": True}, send_body=send_body)
                return

            if route == "/search":
                query = first(params, "query").strip()
                search_type = first(params, "type", "song").strip().lower()
                limit = max(0, int(first(params, "limit", "10")))
                offset = max(0, int(first(params, "offset", "0")))
                if not query:
                    self.write_json({"error": "query parameter is required"}, status=HTTPStatus.BAD_REQUEST, send_body=send_body)
                    return
                if search_type not in {"song", "album", "artist"}:
                    self.write_json(
                        {"error": f"invalid search type: {search_type}. Use 'album', 'song', or 'artist'"},
                        status=HTTPStatus.BAD_REQUEST,
                        send_body=send_body,
                    )
                    return
                self.write_json(LIBRARY.direct_search_payload(query, search_type, limit, offset), send_body=send_body)
                return

            if route.startswith("/album/"):
                album = LIBRARY.albums_by_id.get(route.rsplit("/", 1)[-1])
                if album is None:
                    self.write_json({"error": "album not found"}, status=HTTPStatus.NOT_FOUND, send_body=send_body)
                    return
                self.write_json(album.payload(include_songs=True), send_body=send_body)
                return

            if route.startswith("/song/"):
                track = LIBRARY.tracks_by_id.get(route.rsplit("/", 1)[-1])
                if track is None:
                    self.write_json({"error": "song not found"}, status=HTTPStatus.NOT_FOUND, send_body=send_body)
                    return
                self.write_json(track.song_payload(), send_body=send_body)
                return

            if route.startswith("/lyrics/"):
                track = LIBRARY.find_track(track_id=route.rsplit("/", 1)[-1])
                if track is None:
                    self.write_json({"error": "lyrics not found"}, status=HTTPStatus.NOT_FOUND, send_body=send_body)
                    return
                _, lyrics = LIBRARY.resolve_lyrics(track.id)
                self.write_json({"lyrics": lyrics}, send_body=send_body)
                return

            if route.startswith("/playback/"):
                track = LIBRARY.find_track(track_id=route.rsplit("/", 1)[-1])
                if track is None:
                    self.write_json({"error": "track not found"}, status=HTTPStatus.NOT_FOUND, send_body=send_body)
                    return
                if first(params, "redirect", "false").lower() == "true":
                    target = f"/rest/stream.view?u={os.environ.get('SUBSONIC_USERNAME', 'admin')}&p={os.environ.get('SUBSONIC_PASSWORD', 'admin123')}&id={track.id}"
                    self.send_response(HTTPStatus.FOUND)
                    self.send_header("Location", target)
                    self.end_headers()
                    return
                self.write_json(
                    {
                        "id": track.id,
                        "albumId": track.album_id,
                        "title": track.title,
                        "artist": track.artist_name,
                        "url": f"/rest/stream.view?id={track.id}",
                        "path": posixpath.join(track.album_name, track.audio_path.name),
                        "contentType": track.content_type,
                        "size": track.size,
                    },
                    send_body=send_body,
                )
                return

            if route not in {
                "/rest/ping.view",
                "/rest/getLicense.view",
                "/rest/getIndexes.view",
                "/rest/getArtists.view",
                "/rest/getArtist.view",
                "/rest/getAlbumList2.view",
                "/rest/getMusicDirectory.view",
                "/rest/search3.view",
                "/rest/getAlbum.view",
                "/rest/getSong.view",
                "/rest/getLyrics.view",
                "/rest/getLyricsBySongId.view",
                "/rest/getCoverArt.view",
                "/rest/stream.view",
                "/rest/download.view",
            }:
                self.send_error(HTTPStatus.NOT_FOUND, "endpoint not found")
                return

            if not self.authorize(params, binary=route in {"/rest/getCoverArt.view", "/rest/stream.view", "/rest/download.view"}):
                return

            if route == "/rest/ping.view":
                self.write_subsonic_ok({}, send_body=send_body)
                return
            if route == "/rest/getLicense.view":
                self.write_subsonic_ok({"license": {"valid": True, "email": "", "trialExpires": ""}}, send_body=send_body)
                return
            if route == "/rest/getIndexes.view":
                self.write_subsonic_ok(
                    {
                        "indexes": {
                            "lastModified": int(time.time() * 1000),
                            "ignoredArticles": "",
                            "index": [
                                {
                                    "name": "S",
                                    "artist": [LIBRARY.artist.payload(include_albums=False)],
                                }
                            ],
                        }
                    },
                    send_body=send_body,
                )
                return
            if route == "/rest/getArtists.view":
                self.write_subsonic_ok(
                    {
                        "artists": {
                            "ignoredArticles": "",
                            "index": [
                                {
                                    "name": "S",
                                    "artist": [LIBRARY.artist.payload(include_albums=False)],
                                }
                            ],
                        }
                    },
                    send_body=send_body,
                )
                return
            if route == "/rest/getArtist.view":
                artist_id = first(params, "id")
                if artist_id != LIBRARY.artist.id:
                    self.write_subsonic_failure(70, "artist not found")
                    return
                self.write_subsonic_ok({"artist": LIBRARY.artist.payload(include_albums=True)}, send_body=send_body)
                return
            if route == "/rest/getAlbumList2.view":
                size = max(0, int(first(params, "size", "50")))
                offset = max(0, int(first(params, "offset", "0")))
                albums = [album.payload(include_songs=False) for album in LIBRARY.artist.albums][offset : offset + size]
                self.write_subsonic_ok({"albumList2": {"album": albums}}, send_body=send_body)
                return
            if route == "/rest/getMusicDirectory.view":
                directory_id = first(params, "id", ROOT_DIRECTORY_ID)
                children = LIBRARY.directory_children.get(directory_id)
                if children is None:
                    self.write_subsonic_failure(70, "directory not found")
                    return
                self.write_subsonic_ok({"directory": {"id": directory_id, "child": children}}, send_body=send_body)
                return
            if route == "/rest/search3.view":
                query = first(params, "query").strip()
                if not query:
                    self.write_subsonic_failure(10, "query is required")
                    return
                songs, albums, artists = LIBRARY.search(query)
                song_count = int(first(params, "songCount", "20"))
                song_offset = int(first(params, "songOffset", "0"))
                album_count = int(first(params, "albumCount", "20"))
                album_offset = int(first(params, "albumOffset", "0"))
                artist_count = int(first(params, "artistCount", "20"))
                artist_offset = int(first(params, "artistOffset", "0"))
                self.write_subsonic_ok(
                    {
                        "searchResult3": {
                            "song": [track.song_payload() for track in songs][song_offset : song_offset + song_count],
                            "album": [album.payload(include_songs=False) for album in albums][album_offset : album_offset + album_count],
                            "artist": [artist.payload(include_albums=False) for artist in artists][artist_offset : artist_offset + artist_count],
                        }
                    },
                    send_body=send_body,
                )
                return
            if route == "/rest/getAlbum.view":
                album = LIBRARY.albums_by_id.get(first(params, "id"))
                if album is None:
                    self.write_subsonic_failure(70, "album not found")
                    return
                self.write_subsonic_ok({"album": album.payload(include_songs=True)}, send_body=send_body)
                return
            if route == "/rest/getSong.view":
                track = LIBRARY.tracks_by_id.get(first(params, "id"))
                if track is None:
                    self.write_subsonic_failure(70, "song not found")
                    return
                self.write_subsonic_ok({"song": track.song_payload()}, send_body=send_body)
                return
            if route == "/rest/getLyrics.view":
                track = LIBRARY.find_track(
                    track_id=first(params, "id"),
                    song_id=first(params, "songId"),
                    title=first(params, "title"),
                    artist=first(params, "artist"),
                )
                if track is None:
                    self.write_subsonic_failure(70, "lyrics not found")
                    return
                _, lyrics = LIBRARY.resolve_lyrics(track.id)
                self.write_subsonic_ok(
                    {
                        "lyrics": {
                            "artist": track.artist_name,
                            "title": track.title,
                            "value": lyrics,
                        }
                    },
                    send_body=send_body,
                )
                return
            if route == "/rest/getLyricsBySongId.view":
                track = LIBRARY.find_track(track_id=first(params, "id"), song_id=first(params, "songId"))
                if track is None:
                    self.write_subsonic_failure(70, "lyrics not found")
                    return
                _, lyrics = LIBRARY.resolve_lyrics(track.id)
                self.write_subsonic_ok(
                    {
                        "structuredLyrics": {
                            "lang": "eng",
                            "displayArtist": track.artist_name,
                            "displayTitle": track.title,
                            "line": [
                                {"start": 0, "value": lyrics}
                            ],
                        }
                    },
                    send_body=send_body,
                )
                return
            if route == "/rest/getCoverArt.view":
                cover_id = first(params, "id")
                try:
                    self.send_file(LIBRARY.resolve_cover(cover_id), "image/jpeg", send_body=send_body)
                except KeyError:
                    self.send_error(HTTPStatus.NOT_FOUND, "cover not found")
                return
            if route in {"/rest/stream.view", "/rest/download.view"}:
                track = LIBRARY.tracks_by_id.get(first(params, "id"))
                if track is None:
                    self.send_error(HTTPStatus.NOT_FOUND, "track not found")
                    return
                self.send_file(track.audio_path, track.content_type, send_body=send_body)
                return
        except ValueError as exc:
            self.write_subsonic_failure(10, str(exc))
        except BrokenPipeError:
            return

    def authorize(self, params: dict[str, list[str]], *, binary: bool) -> bool:
        username = os.environ.get("SUBSONIC_USERNAME", "admin")
        password = os.environ.get("SUBSONIC_PASSWORD", "admin123")
        request_user = first(params, "u")
        if request_user != username:
            return self.auth_failure(binary, 40, "wrong username or password")

        plain_password = first(params, "p")
        if plain_password:
            if decode_subsonic_password(plain_password) == password:
                return True
            return self.auth_failure(binary, 40, "wrong username or password")

        token = first(params, "t")
        salt = first(params, "s")
        if token and salt:
            expected = hashlib.md5(f"{password}{salt}".encode("utf-8")).hexdigest()
            if token.lower() == expected:
                return True
            return self.auth_failure(binary, 41, "token authentication failed")

        return self.auth_failure(binary, 40, "missing subsonic credentials")

    def auth_failure(self, binary: bool, code: int, message: str) -> bool:
        if binary:
            self.send_error(HTTPStatus.UNAUTHORIZED, message)
        else:
            self.write_subsonic_failure(code, message)
        return False

    def write_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK, *, send_body: bool = True) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def subsonic_envelope(self, status: str) -> dict[str, Any]:
        return {
            "status": status,
            "version": SUBSONIC_VERSION,
            "type": SERVER_TYPE,
            "serverVersion": SERVER_NAME,
            "openSubsonic": True,
        }

    def write_subsonic_ok(self, payload: dict[str, Any], *, send_body: bool = True) -> None:
        envelope = self.subsonic_envelope("ok")
        envelope.update(payload)
        self.write_json({"subsonic-response": envelope}, send_body=send_body)

    def write_subsonic_failure(self, code: int, message: str) -> None:
        envelope = self.subsonic_envelope("failed")
        envelope["error"] = {"code": code, "message": message}
        self.write_json({"subsonic-response": envelope})

    def send_file(self, path: Path, content_type: str, *, send_body: bool = True) -> None:
        file_size = path.stat().st_size
        range_header = self.headers.get("Range", "").strip()
        start = 0
        end = file_size - 1
        status = HTTPStatus.OK

        if range_header.startswith("bytes="):
            raw_range = range_header.split("=", 1)[1]
            start_text, _, end_text = raw_range.partition("-")
            if start_text:
                start = int(start_text)
            if end_text:
                end = min(int(end_text), file_size - 1)
            status = HTTPStatus.PARTIAL_CONTENT

        content_length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(content_length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()
        if not send_body:
            return

        with path.open("rb") as handle:
            handle.seek(start)
            remaining = content_length
            while remaining > 0:
                chunk = handle.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[my-album-api] {self.address_string()} - {fmt % args}")


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), SubsonicHandler)
    print(f"Serving {SERVER_NAME} on http://{host}:{port}")
    print(f"Manifest: {MANIFEST_PATH}")
    print(f"Albums root: {LIBRARY_ROOT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
