"""Download the selected podcast track and prepare 16 kHz mono G.711 for Xiaozhi.

Run this on macOS with ``python tools/prepare_podcast_bgm.py``. The MP3 and
derived audio stay under the ignored data/podcast directory, outside Git.
"""

import audioop
import subprocess
import urllib.request
import wave
from pathlib import Path


SONG_ID = 1406259632  # Yoli: 柠檬水的香气和夏天的风.
SOURCE_URL = f"https://music.163.com/song/media/outer/url?id={SONG_ID}.mp3"
DIRECTORY = Path(__file__).resolve().parents[1] / "data" / "podcast"
MP3 = DIRECTORY / "lemon_summer_yoli.mp3"
WAV = DIRECTORY / "lemon_summer_yoli_16k.wav"
OUTPUT = DIRECTORY / "lemon_summer_yoli_16k.ulaw"


def main():
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    if not MP3.exists():
        request = urllib.request.Request(
            SOURCE_URL,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"},
        )
        temporary = MP3.with_suffix(".mp3.part")
        try:
            with urllib.request.urlopen(request, timeout=30) as response, temporary.open("wb") as file:
                if not response.headers.get("Content-Type", "").startswith("audio/"):
                    raise ValueError("Music source did not return audio")
                size = 0
                while chunk := response.read(65536):
                    size += len(chunk)
                    if size > 20 * 1024 * 1024:
                        raise ValueError("Music file exceeds 20 MB")
                    file.write(chunk)
            if size < 1024:
                raise ValueError("Music file is empty")
            temporary.replace(MP3)
        finally:
            temporary.unlink(missing_ok=True)

    try:
        subprocess.run(
            ["afconvert", str(MP3), "-o", str(WAV), "-f", "WAVE", "-d", "LEI16@16000", "-c", "1"],
            check=True,
        )
        temporary = OUTPUT.with_suffix(".ulaw.part")
        with wave.open(str(WAV), "rb") as source, temporary.open("wb") as target:
            if (source.getframerate(), source.getnchannels(), source.getsampwidth()) != (16000, 1, 2):
                raise ValueError("Converted track must be 16 kHz, mono, 16-bit PCM")
            while pcm := source.readframes(16000):
                target.write(audioop.lin2ulaw(pcm, 2))
        temporary.replace(OUTPUT)
    finally:
        WAV.unlink(missing_ok=True)
        OUTPUT.with_suffix(".ulaw.part").unlink(missing_ok=True)
    print(f"MP3: {MP3} ({MP3.stat().st_size} bytes)")
    print(f"Xiaozhi audio: {OUTPUT} ({OUTPUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
