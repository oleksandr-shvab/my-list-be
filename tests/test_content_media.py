"""Clue media of the puzzle builder (SPEC-04 AC-34 to AC-40): the limits table, the stills and the
step-1 crop, the clip and its poster, and the seed's part in each. Images are generated here, no
network. Clips are small (640x360 at the real 24 fps and 12 s) to keep the run short."""

import random
from itertools import pairwise
from pathlib import Path

import av
import pytest
from PIL import Image, ImageChops, ImageStat

from app.content import media
from app.content.media import MB, MediaFacts, MediaLimitError

SMALL_CLIP = {"size": (640, 360), "fps": 24, "seconds": 12}
TINY_CLIP = {"size": (320, 180), "fps": 12, "seconds": 8}


def blobs(seed: int, size: tuple[int, int], noise: float = 0.0) -> Image.Image:
    """A made-up screenshot: smooth colour blobs, optionally mixed with per-pixel noise."""
    rng = random.Random(seed)
    image = Image.frombytes("RGB", (16, 9), rng.randbytes(16 * 9 * 3))
    image = image.resize(size, Image.Resampling.BICUBIC)
    if noise:
        grain = Image.frombytes("RGB", size, rng.randbytes(size[0] * size[1] * 3))
        image = Image.blend(image, grain, noise)
    return image


@pytest.fixture(scope="module")
def shots(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    folder = tmp_path_factory.mktemp("shots")
    paths = [folder / f"shot{i}.png" for i in range(3)]
    for index, path in enumerate(paths):
        blobs(100 + index, (1280, 720)).save(path)
    return paths


def mean_difference(a: Image.Image, b: Image.Image) -> float:
    return sum(ImageStat.Stat(ImageChops.difference(a.convert("RGB"), b.convert("RGB"))).mean) / 3


def decoded_frame(path: Path, index: int) -> Image.Image:
    with av.open(str(path)) as container:
        for position, frame in enumerate(container.decode(video=0)):
            if position == index:
                return frame.to_image()
    raise AssertionError(f"no frame {index}")


@pytest.mark.parametrize(
    ("content_type", "width", "height", "byte_size", "duration_ms", "expected"),
    [
        ("video/mp4", 1280, 720, 4 * MB, 8_000, []),
        ("video/mp4", 1280, 720, 1000, 15_000, []),
        ("video/mp4", 720, 1280, 1000, 12_000, ["height"]),  # A portrait pair fits as a pair.
        ("video/mp4", 1281, 720, 1000, 12_000, ["width"]),
        ("video/mp4", 1280, 721, 1000, 12_000, ["height"]),
        ("video/mp4", 640, 360, 4 * MB + 1, 12_000, ["byte_size"]),
        ("video/mp4", 640, 360, 1000, 7_999, ["duration"]),
        ("video/mp4", 640, 360, 1000, 15_001, ["duration"]),
        ("video/webm", 640, 360, 1000, 12_000, ["content_type"]),
        ("image/webp", 1920, 1080, 1 * MB, None, []),
        ("image/webp", 1921, 1080, 1000, None, ["width"]),
        ("image/webp", 1920, 1081, 1000, None, ["height"]),
        ("image/webp", 640, 360, 1 * MB + 1, None, ["byte_size"]),
        ("image/webp", 640, 360, 1000, 12_000, ["duration"]),
    ],
)
def test__broken_limits__each_limit_is_checked_on_its_own(
    content_type: str,
    width: int,
    height: int,
    byte_size: int,
    duration_ms: int | None,
    expected: list[str],
) -> None:
    facts = MediaFacts(content_type, byte_size, width, height, duration_ms)
    assert media.broken_limits(facts) == expected


def test__still__fits_the_limits_with_no_metadata_and_refuses_what_cannot_fit(
    tmp_path: Path,
) -> None:
    # 2400x1500 of pure grain: too big until it is scaled down, and over 1 MB at quality 82, so the
    # quality steps are needed (about 0.9 MB at 74).
    source = tmp_path / "big.jpg"
    exif = Image.Exif()
    exif[0x010F] = "Test camera"
    exif.get_ifd(0x8825)[1] = "N"  # A GPS tag.
    blobs(1, (2400, 1500), noise=1.0).save(
        source, exif=exif, icc_profile=b"not a real profile", quality=95
    )
    out = tmp_path / "out.webp"

    facts = media.still(source, out)

    assert facts == media.measure(out)
    assert (facts.width, facts.height) == (1728, 1080)  # 16:10, fitted to the height.
    assert facts.byte_size <= 1 * MB
    with Image.open(out) as result:
        assert not {"exif", "icc_profile", "xmp"} & set(result.info)
        assert not result.getexif()

    # Pure noise does not compress under 1 MB at any quality: refused, and no file is left.
    noise = tmp_path / "noise.png"
    Image.frombytes("RGB", (1920, 1080), random.Random(2).randbytes(1920 * 1080 * 3)).save(noise)
    refused = tmp_path / "refused.webp"
    with pytest.raises(MediaLimitError):
        media.still(noise, refused)
    assert not refused.exists()


@pytest.mark.parametrize(
    ("size", "crop_width"),
    [
        ((1280, 720), 427),
        ((1920, 1080), 640),
        ((1500, 1000), 500),
        ((3000, 500), 889),  # Very wide and low: the height limits the window, not the third.
    ],
)
def test__pixelated_crop__is_a_third_wide_inside_the_image_and_flat_in_blocks(
    tmp_path: Path, size: tuple[int, int], crop_width: int
) -> None:
    source = tmp_path / "source.png"
    blobs(5, size).save(source)
    for seed in range(25):
        left, top, right, bottom = media.crop_box(size, random.Random(seed))
        assert 0 <= left < right <= size[0]
        assert 0 <= top < bottom <= size[1]
        assert right - left == crop_width
        assert (right - left) / (bottom - top) == pytest.approx(16 / 9, abs=0.02)

    out = tmp_path / "crop.webp"
    facts = media.pixelated_crop(source, out, random.Random(3))

    assert (facts.width, facts.height) == (960, 540)
    with Image.open(out) as result:
        picture = result.convert("RGB")
    block = media.CROP_BLOCK_PX
    colours = set()
    for x in range(0, 960, block):
        for y in range(0, 540, block):
            block_pixels = picture.crop((x, y, x + block, y + block))
            assert len(block_pixels.getcolors()) == 1  # One flat colour per block.
            colours.add(block_pixels.getpixel((0, 0)))
    assert len(colours) > 100  # A real picture, not a flat one.
    # The first block is the average colour of its part of the crop the box names.
    left, top, right, bottom = media.crop_box(size, random.Random(3))
    with Image.open(source) as original:
        part = original.crop((left, top, left + (right - left) // 48, top + (bottom - top) // 27))
        expected = ImageStat.Stat(part.convert("RGB")).mean
    assert all(abs(a - b) < 12 for a, b in zip(picture.getpixel((0, 0)), expected, strict=True))


def test__clip__is_a_silent_12_second_h264_mp4_and_the_poster_is_its_first_frame(
    tmp_path: Path, shots: list[Path]
) -> None:
    out = tmp_path / "clip.mp4"

    facts = media.clip(shots, out, random.Random(1), **SMALL_CLIP)

    assert facts == media.measure(out)
    assert facts.content_type == "video/mp4"
    assert media.broken_limits(facts) == []
    assert abs(facts.duration_ms - 12_000) < 100
    assert facts.byte_size <= 4 * MB
    with av.open(str(out)) as container:
        assert len(container.streams) == 1
        assert not container.streams.audio
        video = container.streams.video[0]
        assert (video.codec_context.name, video.codec_context.pix_fmt) == ("h264", "yuv420p")
        pictures = [frame.to_image().resize((64, 36)) for frame in container.decode(video=0)]
    assert len(pictures) == 12 * 24
    # Shots fade into each other over a second: with a hard cut one step would jump by about 70.
    steps = [mean_difference(a, b) for a, b in pairwise(pictures)]
    assert max(steps) < 8
    data = out.read_bytes()
    assert data.index(b"moov") < data.index(b"mdat")  # +faststart

    poster_out = tmp_path / "poster.webp"
    poster_facts = media.poster(out, poster_out)
    assert poster_facts == media.measure(poster_out)
    assert poster_facts.content_type == "image/webp"
    with Image.open(poster_out) as poster_image:
        assert mean_difference(poster_image, decoded_frame(out, 0)) < 3
        assert mean_difference(poster_image, decoded_frame(out, 150)) > 10


def test__clip__slow_full_size_default_clip_fits_the_limits(
    tmp_path: Path, shots: list[Path]
) -> None:
    """The real clip: no size, fps or length given, so the defaults are what is checked (about 5 s)."""
    out = tmp_path / "clip.mp4"

    facts = media.clip(shots, out, random.Random(1))

    assert (facts.width, facts.height) == (1280, 720)
    assert facts.duration_ms == 12_000
    assert facts.byte_size <= 4 * MB
    assert media.broken_limits(facts) == []
    with av.open(str(out)) as container:
        assert len(container.streams) == 1
        assert container.streams.video[0].codec_context.name == "h264"


def test__clip__over_the_size_limit_is_refused_and_leaves_no_file(
    tmp_path: Path, shots: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    limits = media.LIMITS["video/mp4"]
    monkeypatch.setitem(
        media.LIMITS, "video/mp4", media.Limits(1280, 720, 1000, limits.duration_ms)
    )
    out = tmp_path / "clip.mp4"

    with pytest.raises(MediaLimitError):
        media.clip(shots[:2], out, random.Random(1), **TINY_CLIP)

    assert not out.exists()


def test__measure__refuses_a_clip_with_an_audio_track(tmp_path: Path) -> None:
    path = tmp_path / "with-audio.mp4"
    with av.open(str(path), "w", format="mp4") as container:
        video = container.add_stream("libx264", rate=12)
        video.width, video.height, video.pix_fmt = 64, 64, "yuv420p"
        audio = container.add_stream("aac", rate=44100)
        for index in range(12):
            frame = av.VideoFrame.from_image(Image.new("RGB", (64, 64), (index * 10, 0, 0)))
            frame.pts = index
            container.mux(video.encode(frame))
        silence = av.AudioFrame(format="fltp", layout="mono", samples=1024)
        for plane in silence.planes:
            plane.update(bytes(plane.buffer_size))
        silence.sample_rate = 44100
        for index in range(20):
            silence.pts = index * 1024
            container.mux(audio.encode(silence))
        container.mux(video.encode())
        container.mux(audio.encode())

    with pytest.raises(MediaLimitError):
        media.measure(path)


def test__seed__decides_the_crop_and_the_clip(tmp_path: Path, shots: list[Path]) -> None:
    def crop(seed: int, name: str) -> bytes:
        out = tmp_path / name
        media.pixelated_crop(shots[0], out, random.Random(seed))
        return out.read_bytes()

    assert media.crop_box((1920, 1080), random.Random(9)) == media.crop_box(
        (1920, 1080), random.Random(9)
    )
    assert crop(9, "a.webp") == crop(9, "b.webp")
    assert crop(9, "a.webp") != crop(10, "c.webp")

    # The encoder is not asked to give identical bytes, so the clips are compared by their pictures.
    def clip_frame(seed: int, name: str) -> Image.Image:
        out = tmp_path / name
        media.clip(shots, out, random.Random(seed), **TINY_CLIP)
        return decoded_frame(out, 30)

    same = mean_difference(clip_frame(4, "a.mp4"), clip_frame(4, "b.mp4"))
    other = mean_difference(clip_frame(4, "a.mp4"), clip_frame(5, "c.mp4"))
    assert same < 3
    assert other > 10
