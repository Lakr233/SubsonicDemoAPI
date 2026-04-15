#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, UTC
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from mutagen.id3 import APIC, COMM, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TXXX, USLT
from mutagen.mp3 import MP3
from mutagen.wave import WAVE


ROOT = Path(__file__).resolve().parent.parent
ALBUM_ROOT = ROOT / "albums"
ASSET_ROOT = ALBUM_ROOT / "_assets"
MANIFEST_PATH = ALBUM_ROOT / "library_manifest.json"
README_PATH = ALBUM_ROOT / "README.md"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0 Safari/537.36"


@dataclass(frozen=True)
class TrackPlan:
    source_name: str
    album: str
    track_number: int
    prepared_title: str


TRACK_PLAN = [
    TrackPlan("Backroads and Barstools.wav", "Suno Review Album One", 1, "Backroads and Barstools"),
    TrackPlan("Golden Hour Promise.mp3", "Suno Review Album One", 2, "Golden Hour Promise"),
    TrackPlan("Drizzle on the District Line.wav", "Suno Review Album One", 3, "Drizzle on the District Line"),
    TrackPlan("Snow on My Side of the Bed.wav", "Suno Review Album One", 4, "Snow on My Side of the Bed"),
    TrackPlan("Golden Hour Promise.wav", "Suno Review Album Two", 1, "Golden Hour Promise (WAV)"),
    TrackPlan("Tangled Up In You.wav", "Suno Review Album Two", 2, "Tangled Up In You"),
    TrackPlan("You Got Me Tilting.wav", "Suno Review Album Two", 3, "You Got Me Tilting"),
]


def sanitize_filename(value: str) -> str:
    return re.sub(r'[<>:"/\\\\|?*]', "_", value).strip()


def run_ffprobe(path: Path) -> dict[str, Any]:
    output = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_entries",
            "format=duration,size:format_tags:stream=index,codec_type,codec_name:stream_tags",
            str(path),
        ],
        text=True,
    )
    return json.loads(output)


def parse_suno_id(comment: str) -> str:
    match = re.search(r"id=([0-9a-f-]{36})", comment)
    if not match:
        raise ValueError(f"Unable to find Suno id in comment: {comment!r}")
    return match.group(1)


def fetch_json(url: str) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(request) as response:
        return json.load(response)


def download_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request) as response:
        return response.read()


def load_audio(path: Path):
    if path.suffix.lower() == ".mp3":
        audio = MP3(path)
    elif path.suffix.lower() == ".wav":
        audio = WAVE(path)
    else:
        raise ValueError(f"Unsupported audio format: {path}")

    if audio.tags is None:
        audio.add_tags()
    return audio


def write_tags(
    path: Path,
    *,
    title: str,
    artist: str,
    album: str,
    track_number: int,
    track_total: int,
    cover_bytes: bytes,
    lyrics: str,
    suno_id: str,
    source_title: str,
    source_audio_url: str,
    created_at: str,
) -> None:
    audio = load_audio(path)
    tags = audio.tags

    for frame_id in [
        "TIT2",
        "TPE1",
        "TPE2",
        "TALB",
        "TRCK",
        "TPOS",
        "TCON",
        "TDRC",
        "USLT",
        "APIC",
        "COMM",
        "TXXX",
    ]:
        tags.delall(frame_id)

    tags.add(TIT2(encoding=3, text=title))
    tags.add(TPE1(encoding=3, text=artist))
    tags.add(TPE2(encoding=3, text=artist))
    tags.add(TALB(encoding=3, text=album))
    tags.add(TRCK(encoding=3, text=f"{track_number}/{track_total}"))
    tags.add(TPOS(encoding=3, text="1/1"))
    tags.add(TCON(encoding=3, text="AI Music"))
    tags.add(TDRC(encoding=3, text=created_at[:10]))
    tags.add(USLT(encoding=3, lang="eng", desc="Lyrics", text=lyrics))
    tags.add(
        APIC(
            encoding=3,
            mime="image/jpeg",
            type=3,
            desc="Cover",
            data=cover_bytes,
        )
    )
    tags.add(COMM(encoding=3, lang="eng", desc="Source", text=f"Suno id={suno_id}; source_title={source_title}"))
    tags.add(TXXX(encoding=3, desc="SUNO_ID", text=suno_id))
    tags.add(TXXX(encoding=3, desc="SUNO_AUDIO_URL", text=source_audio_url))
    audio.save(v2_version=3)


def ensure_hardlink(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if os.path.samefile(source, target):
            return
        target.unlink()
    os.link(source, target)


def main() -> None:
    ALBUM_ROOT.mkdir(exist_ok=True)
    ASSET_ROOT.mkdir(exist_ok=True)

    plan_by_source = {item.source_name: item for item in TRACK_PLAN}
    missing = [item.source_name for item in TRACK_PLAN if not (ROOT / item.source_name).exists()]
    if missing:
        raise SystemExit(f"Missing source files: {missing}")

    track_totals = {}
    for item in TRACK_PLAN:
        track_totals[item.album] = max(track_totals.get(item.album, 0), item.track_number)

    manifest: dict[str, Any] = {
        "prepared_at": datetime.now(UTC).isoformat(),
        "artist": "Suno",
        "albums": [],
        "tracks": [],
    }

    first_cover_by_album: dict[str, Path] = {}

    for source_name in [item.source_name for item in TRACK_PLAN]:
        source_path = ROOT / source_name
        plan = plan_by_source[source_name]
        probe = run_ffprobe(source_path)
        format_tags = probe.get("format", {}).get("tags", {})
        comment = format_tags.get("comment", "")
        suno_id = parse_suno_id(comment)
        clip = fetch_json(f"https://studio-api-prod.suno.com/api/clip/{suno_id}")
        lyrics = clip["metadata"]["prompt"].strip()
        cover_url = clip.get("image_large_url") or clip["image_url"]
        cover_bytes = download_bytes(cover_url)
        cover_path = ASSET_ROOT / f"{suno_id}.jpg"
        cover_path.write_bytes(cover_bytes)

        write_tags(
            source_path,
            title=plan.prepared_title,
            artist="Suno",
            album=plan.album,
            track_number=plan.track_number,
            track_total=track_totals[plan.album],
            cover_bytes=cover_bytes,
            lyrics=lyrics,
            suno_id=suno_id,
            source_title=clip["title"],
            source_audio_url=clip["audio_url"],
            created_at=clip["created_at"],
        )

        album_dir = ALBUM_ROOT / plan.album
        safe_name = sanitize_filename(f"{plan.track_number:02d} - {plan.prepared_title}{source_path.suffix.lower()}")
        link_path = album_dir / safe_name
        ensure_hardlink(source_path, link_path)

        lyrics_path = album_dir / f"{plan.track_number:02d} - {sanitize_filename(plan.prepared_title)}.lyrics.txt"
        lyrics_path.write_text(lyrics + "\n", encoding="utf-8")

        if plan.album not in first_cover_by_album:
            first_cover_by_album[plan.album] = cover_path

        has_cover_before = any(stream.get("codec_type") == "video" for stream in probe.get("streams", []))
        has_lyrics_before = any("lyrics" in key.lower() for key in format_tags)

        manifest["tracks"].append(
            {
                "source_file": str(source_path),
                "prepared_title": plan.prepared_title,
                "source_title": clip["title"],
                "album": plan.album,
                "track_number": plan.track_number,
                "track_total": track_totals[plan.album],
                "artist": "Suno",
                "suno_id": suno_id,
                "source_audio_url": clip["audio_url"],
                "cover_url": cover_url,
                "duration_seconds": float(probe["format"]["duration"]),
                "source_has_embedded_cover_before": has_cover_before,
                "source_has_embedded_lyrics_before": has_lyrics_before,
                "lyrics_sidecar": str(lyrics_path),
                "album_link": str(link_path),
            }
        )

    manifest["tracks"].sort(key=lambda item: (item["album"], item["track_number"], item["prepared_title"]))

    for album in sorted(track_totals):
        album_dir = ALBUM_ROOT / album
        album_dir.mkdir(exist_ok=True)
        cover_target = album_dir / "cover.jpg"
        shutil.copyfile(first_cover_by_album[album], cover_target)
        manifest["albums"].append(
            {
                "name": album,
                "track_total": track_totals[album],
                "cover_file": str(cover_target),
            }
        )

    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    README_PATH.write_text(
        "\n".join(
            [
                "# Suno Library Prep",
                "",
                "This folder contains the review-ready library grouped into two albums.",
                "",
                "Files in the album folders are hardlinks to the source audio, so metadata edits stay in sync.",
                "",
                "Each track now carries embedded title, artist, album, track number, cover art, and lyrics.",
                "",
                "Use `library_manifest.json` as the source of truth for later Subsonic API work.",
                "",
            ]
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
