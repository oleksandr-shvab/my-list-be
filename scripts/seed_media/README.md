# Seed media

Placeholder files for the dev seed (`scripts/seed_dev.py`, AC-82). We made them from ffmpeg's built-in test patterns, so no third-party footage or screenshot is in Git (decision 59). Together they are about 64 KB.

- `step-1.webp` to `step-5.webp`: the images of clue steps 1 to 5. Step 1 is a pixelated test pattern.
- `clip.mp4`: the step-6 clip. H.264, `yuv420p`, `+faststart`, no audio, 12 seconds, 640x360.
- `poster.webp`: the still frame of the clip.
- `manifest.json`: each file's content type, width, height and, for the clip, duration. The seed stores these values, and takes the byte size from the file itself. They stay inside the limits of AC-74b (`CONTRACTS.md`). `tests/test_seed_media.py` checks every file against the limits.

The seed gives every puzzle the same seven files. Each one is stored under its SHA-256 as the media key, so the keys are content hashes (AC-74).

## How they were made

There was no ffmpeg on the machine, so a throwaway Docker container ran it (ffmpeg 7). Run from this folder:

```
ff() { docker run --rm -u "$(id -u):$(id -g)" -v "$PWD:/out" -w /out --entrypoint ffmpeg jrottenberg/ffmpeg:7-alpine -hide_banner -loglevel error -y "$@"; }
ff -f lavfi -i "testsrc=size=640x360:rate=1:duration=1" -vf "scale=32:18:flags=area,scale=640:360:flags=neighbor" -frames:v 1 -c:v libwebp -quality 80 step-1.webp
ff -f lavfi -i "testsrc2=size=640x360:rate=1:duration=1" -frames:v 1 -c:v libwebp -quality 80 step-2.webp
ff -f lavfi -i "smptebars=size=640x360:rate=1:duration=1" -frames:v 1 -c:v libwebp -quality 80 step-3.webp
ff -f lavfi -i "rgbtestsrc=size=640x360:rate=1:duration=1" -frames:v 1 -c:v libwebp -quality 80 step-4.webp
ff -f lavfi -i "pal75bars=size=640x360:rate=1:duration=1" -frames:v 1 -c:v libwebp -quality 80 step-5.webp
ff -f lavfi -i "testsrc=size=640x360:rate=15:duration=12" -an -c:v libx264 -profile:v main -pix_fmt yuv420p -crf 30 -preset slow -movflags +faststart clip.mp4
ff -f lavfi -i "testsrc=size=640x360:rate=15:duration=1" -frames:v 1 -c:v libwebp -quality 80 poster.webp
```

After a rebuild, update the sizes in `manifest.json` if they changed. Check the clip with `ffprobe -show_entries stream=codec_name,codec_type,width,height,duration,pix_fmt`: one `h264` video stream, no audio stream.
