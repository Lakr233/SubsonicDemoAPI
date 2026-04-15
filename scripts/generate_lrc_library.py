#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from mutagen.id3 import USLT
from mutagen.mp3 import MP3
from mutagen.wave import WAVE


ROOT = Path(__file__).resolve().parent.parent
ALBUM_ROOT = ROOT / "albums"
MANIFEST_PATH = ALBUM_ROOT / "library_manifest.json"
README_PATH = ALBUM_ROOT / "README.md"
WHISPER_MODEL = Path("/Users/qaq/Desktop/whisper-ggml-large-v3-turbo-q5_0.bin")
WHISPER_BINARY = "whisper-cli"
TEMP_ROOT = ROOT / ".tmp_whisper"
CACHE_ROOT = ROOT / ".suno_cache"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0 Safari/537.36"


@dataclass
class WhisperWord:
    text: str
    normalized: str
    start: float
    end: float


@dataclass
class TimedLine:
    time: float
    text: str


def fetch_json(url: str) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            with urlopen(request) as response:
                return json.load(response)
        except URLError as error:
            last_error = error
            time.sleep(1 + attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def cached_clip(suno_id: str) -> dict[str, Any]:
    CACHE_ROOT.mkdir(exist_ok=True)
    cache_path = CACHE_ROOT / f"{suno_id}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    data = fetch_json(f"https://studio-api-prod.suno.com/api/clip/{suno_id}")
    cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def normalize_token(text: str) -> str:
    cleaned = text.lower().strip()
    cleaned = cleaned.replace("’", "'")
    cleaned = cleaned.replace("“", '"').replace("”", '"')
    cleaned = re.sub(r"^\W+|\W+$", "", cleaned)
    cleaned = cleaned.replace("'cause", "cause")
    cleaned = cleaned.replace("til", "till")
    cleaned = cleaned.replace("okay", "ok")
    cleaned = cleaned.replace("alright", "allright")
    cleaned = cleaned.replace("gonna", "goingto")
    cleaned = cleaned.replace("wanna", "wantto")
    return cleaned


def normalize_for_compare(text: str) -> str:
    text = text.lower().replace("’", "'")
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"[^a-z0-9'\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def lyric_lines_from_prompt(prompt: str) -> list[str]:
    result: list[str] = []
    for raw_line in prompt.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            continue
        line = re.sub(r"\s+", " ", line)
        result.append(line)
    return result


def official_tokens(line: str) -> list[str]:
    normalized = normalize_for_compare(line)
    if not normalized:
        return []
    return [token for token in (normalize_token(part) for part in normalized.split()) if token]


def run_whisper_json(audio_path: Path, output_stem: Path) -> dict[str, Any]:
    output_stem.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            WHISPER_BINARY,
            "-m",
            str(WHISPER_MODEL),
            "-f",
            str(audio_path),
            "-l",
            "en",
            "-ojf",
            "-np",
            "-of",
            str(output_stem),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return json.loads(output_stem.with_suffix(".json").read_text(encoding="utf-8"))


def extract_words(whisper_json: dict[str, Any]) -> list[WhisperWord]:
    words: list[WhisperWord] = []
    for segment in whisper_json.get("transcription", []):
        for token in segment.get("tokens", []):
            text = token.get("text", "")
            if text.startswith("[_") and text.endswith("_]"):
                continue
            normalized = normalize_token(text)
            if not normalized:
                continue
            offsets = token.get("offsets", {})
            start = offsets.get("from", 0) / 1000
            end = offsets.get("to", offsets.get("from", 0)) / 1000
            words.append(WhisperWord(text=text.strip(), normalized=normalized, start=start, end=end))
    return words


def line_similarity(line_tokens: list[str], candidate_words: list[WhisperWord]) -> float:
    candidate_tokens = [word.normalized for word in candidate_words if word.normalized]
    if not line_tokens or not candidate_tokens:
        return 0.0

    common = 0
    remaining = candidate_tokens.copy()
    for token in line_tokens:
        if token in remaining:
            common += 1
            remaining.remove(token)
    coverage = common / len(line_tokens)

    line_joined = " ".join(line_tokens)
    candidate_joined = " ".join(candidate_tokens)
    subsequence_bonus = 1.0 if line_joined in candidate_joined else 0.0
    return coverage + subsequence_bonus


def match_line_start(
    line_tokens: list[str],
    words: list[WhisperWord],
    search_start: int,
) -> tuple[int | None, float | None, int]:
    if not line_tokens:
        return None, None, search_start

    best_score = -1.0
    best_start: int | None = None
    best_end = search_start
    min_window = max(1, len(line_tokens) - 2)
    max_window = len(line_tokens) + 4

    for candidate_start in range(search_start, len(words)):
        for window in range(min_window, max_window + 1):
            candidate_end = min(len(words), candidate_start + window)
            candidate_words = words[candidate_start:candidate_end]
            if not candidate_words:
                continue
            score = line_similarity(line_tokens, candidate_words)
            if score > best_score:
                best_score = score
                best_start = candidate_start
                best_end = candidate_end
        if best_score >= 1.75:
            break

    if best_start is None or best_score < 0.55:
        return None, None, search_start

    return best_start, words[best_start].start, best_end


def align_lines_to_words(lines: list[str], words: list[WhisperWord], fallback_duration: float) -> list[TimedLine]:
    timed: list[TimedLine] = []
    search_index = 0
    matched_positions: list[tuple[int, float]] = []

    for line_index, line in enumerate(lines):
        tokens = official_tokens(line)
        start_index, start_time, next_index = match_line_start(tokens, words, search_index)
        if start_time is not None:
            matched_positions.append((line_index, start_time))
            timed.append(TimedLine(time=start_time, text=line))
            search_index = next_index
        else:
            timed.append(TimedLine(time=-1, text=line))

    if not matched_positions:
        step = fallback_duration / max(len(lines), 1)
        return [TimedLine(time=index * step, text=line.text) for index, line in enumerate(timed)]

    first_matched_index, first_matched_time = matched_positions[0]
    for index in range(first_matched_index):
        previous_time = max(0.0, first_matched_time - (first_matched_index - index) * 2.5)
        timed[index].time = previous_time

    for (left_index, left_time), (right_index, right_time) in zip(matched_positions, matched_positions[1:]):
        gap = right_index - left_index
        if gap <= 1:
            continue
        step = max((right_time - left_time) / gap, 0.25)
        for missing_index in range(left_index + 1, right_index):
            timed[missing_index].time = left_time + step * (missing_index - left_index)

    last_index, last_time = matched_positions[-1]
    tail_step = 3.0
    for index in range(last_index + 1, len(timed)):
        timed[index].time = min(fallback_duration, last_time + tail_step * (index - last_index))

    cleaned: list[TimedLine] = []
    previous_time = 0.0
    for item in timed:
        time = item.time if item.time >= 0 else previous_time
        time = max(time, previous_time)
        cleaned.append(TimedLine(time=time, text=item.text))
        previous_time = time
    return cleaned


def format_lrc_timestamp(seconds: float) -> str:
    hundredths = int(round(seconds * 100))
    minutes, remainder = divmod(hundredths, 6000)
    secs, hundredths = divmod(remainder, 100)
    return f"{minutes:02d}:{secs:02d}.{hundredths:02d}"


def build_lrc(title: str, artist: str, album: str, timed_lines: list[TimedLine]) -> str:
    output = [
        f"[ti:{title}]",
        f"[ar:{artist}]",
        f"[al:{album}]",
    ]
    for line in timed_lines:
        output.append(f"[{format_lrc_timestamp(line.time)}]{line.text}")
    return "\n".join(output) + "\n"


def update_embedded_lyrics(audio_path: Path, lrc_text: str) -> None:
    if audio_path.suffix.lower() == ".mp3":
        audio = MP3(audio_path)
    elif audio_path.suffix.lower() == ".wav":
        audio = WAVE(audio_path)
    else:
        return

    if audio.tags is None:
        audio.add_tags()
    audio.tags.delall("USLT")
    audio.tags.add(USLT(encoding=3, lang="eng", desc="Lyrics", text=lrc_text.strip()))
    audio.save(v2_version=3)


def rewrite_readme(track_count: int) -> None:
    README_PATH.write_text(
        "\n".join(
            [
                "# Suno Library Prep",
                "",
                "This folder contains the review-ready library grouped into two albums.",
                "",
                "The root-level source files have been removed. The album folders now hold the canonical audio files.",
                "",
                f"Generated {track_count} timed `.lrc` files for MuseAmp-compatible lyric playback.",
                "",
                "Use `library_manifest.json` as the source of truth for later Subsonic API work.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def main() -> None:
    if not WHISPER_MODEL.exists():
        raise SystemExit(f"Missing whisper model: {WHISPER_MODEL}")

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    TEMP_ROOT.mkdir(exist_ok=True)
    target_track = os.environ.get("TARGET_TRACK")
    keep_source_files = os.environ.get("KEEP_SOURCE_FILES") == "1"

    for track in manifest["tracks"]:
        if target_track and Path(track["album_link"]).name != target_track:
            continue
        audio_path = Path(track["album_link"])
        source_title = track["source_title"]
        suno_id = track["suno_id"]
        clip = cached_clip(suno_id)
        official_lines = lyric_lines_from_prompt(clip["metadata"]["prompt"])
        whisper_json = run_whisper_json(audio_path, TEMP_ROOT / audio_path.stem)
        words = extract_words(whisper_json)
        duration = float(track["duration_seconds"])
        timed_lines = align_lines_to_words(official_lines, words, duration)
        lrc_text = build_lrc(
            title=track["prepared_title"],
            artist=track["artist"],
            album=track["album"],
            timed_lines=timed_lines,
        )

        lrc_path = audio_path.with_suffix(".lrc")
        lrc_path.write_text(lrc_text, encoding="utf-8")
        Path(track["lyrics_sidecar"]).unlink(missing_ok=True)
        update_embedded_lyrics(audio_path, lrc_text)

        track["lyrics_sidecar"] = str(lrc_path)
        track["lyrics_format"] = "lrc"
        track["source_title"] = source_title

    if not target_track and not keep_source_files:
        source_files = sorted(
            path for path in ROOT.iterdir()
            if path.is_file() and path.suffix.lower() in {".mp3", ".wav"}
        )
        for source_path in source_files:
            source_path.unlink()

        for track in manifest["tracks"]:
            album_audio_path = Path(track["album_link"])
            track["source_file"] = str(album_audio_path)

        manifest["source_files_removed"] = True
        MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        rewrite_readme(len(manifest["tracks"]))

    shutil.rmtree(TEMP_ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
