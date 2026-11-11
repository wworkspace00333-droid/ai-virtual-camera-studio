"""Video assets: probing, decoding, timing, loop/pause/offset/rate, sync, compositing, memory."""
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from make_test_clips import decode_index  # noqa: E402

from vcstudio.ai.tracking import TrackedObject
from vcstudio.replacement.assets import AssetError, AssetLibrary
from vcstudio.replacement.compositor import Compositor
from vcstudio.replacement.spec import ReplacementSpec
from vcstudio.replacement.video import (MAX_BUFFER, PlaybackSettings, PlayerPool, VideoPlayer, ffmpeg_path, open_decoder,
                                        probe)
from vcstudio.roi.model import ROI, new_rect
from vcstudio.roi.resolver import resolve

DATA = Path(__file__).parent / "data"
MP4, MOV, MKV = DATA / "clip_h264.mp4", DATA / "clip_alpha.mov", DATA / "clip_h264.mkv"
needs_ffmpeg = pytest.mark.skipif(ffmpeg_path() is None, reason="ffmpeg not installed")


def settle(player, t, expect=None, timeout=2.0):
    """Ask for time t until the decoder has caught up (decode is asynchronous by design)."""
    end = time.monotonic() + timeout
    img = player.frame_at(t)
    while time.monotonic() < end:
        img = player.frame_at(t)
        if img is not None and (expect is None or decode_index(img) == expect):
            return img
        time.sleep(0.01)
    return img


# ------------------------------------------------------------------ probe / decode
def test_probe_formats():
    a, b = probe(MP4), probe(MOV)
    assert (a.codec, a.width, a.height, a.fps, a.duration, a.has_alpha, a.decoder) == ("avc1", 320, 180, 30.0, 2.0, False, "avfoundation")
    assert a.hardware                                       # VideoToolbox hardware H.264 on Apple Silicon
    assert b.codec == "ap4h" and b.has_alpha


@needs_ffmpeg
def test_probe_ffmpeg_fallback_for_mkv():
    c = probe(MKV)
    assert c.decoder == "ffmpeg" and c.width == 320 and abs(c.duration - 2.0) < 0.05 and not c.hardware


@pytest.mark.parametrize("path", [MP4, pytest.param(MKV, marks=needs_ffmpeg)])
def test_seek_is_frame_accurate(path):
    dec = open_decoder(path, probe(path), 0.5, None)
    frames = [(p, decode_index(i)) for _, (p, i) in zip(range(3), dec)]
    dec.close()
    assert [i for _, i in frames] == [15, 16, 17]
    assert abs(frames[0][0] - 0.5) < 1e-3


def test_alpha_is_premultiplied():
    dec = open_decoder(MOV, probe(MOV), 0.0, None)
    _, img = next(iter(dec)); dec.close()
    assert tuple(img[50, 40]) == (0, 0, 0, 0)               # transparent half -> zero colour
    assert img[50, 250, 3] == 255


def test_decode_downscale():
    dec = open_decoder(MP4, probe(MP4), 0.0, 160)
    _, img = next(iter(dec)); dec.close()
    assert img.shape == (90, 160, 4)


# ------------------------------------------------------------------ player timing
@pytest.fixture
def player():
    made = []

    def make(path=MP4, **kw):
        p = VideoPlayer(path, probe(path), PlaybackSettings(**kw))
        made.append(p)
        return p
    yield make
    for p in made:
        p.close()


def test_realtime_60fps_output_of_30fps_video_with_loops(player):
    p = player(loop=True)
    t0 = time.monotonic()
    errs = []
    for k in range(150):                     # 2.5 s at 60 fps, crosses one loop
        t = t0 + k / 60
        while time.monotonic() < t:
            time.sleep(0.0005)
        img = p.frame_at(t)
        if img is None or k < 6:
            continue
        exp = int((k / 60) * 30) % 60
        got = decode_index(img)
        errs.append(min(abs(got - exp), 60 - abs(got - exp)))
    # Wall-clock paced: allow scheduler jitter of one extra frame on rare samples, but the average must
    # stay essentially exact (a timeline bug shows up as a large or growing error).
    assert errs and max(errs) <= 2 and np.mean(errs) < 0.15, (max(errs), np.mean(errs))
    assert p.stats.loops >= 1 and p.stats.late_frames <= 5


def test_offset_rate_pause_resume_restart(player):
    p = player(start_offset_s=1.0)
    t0 = 1000.0
    assert decode_index(settle(p, t0, 30)) == 30                      # offset 1.0 s -> frame 30
    assert decode_index(settle(p, t0 + 0.2, 36)) == 36
    p.pause(t0 + 0.2)
    assert decode_index(settle(p, t0 + 0.9, 36)) == 36                # frozen while paused
    p.resume(t0 + 0.9)
    assert decode_index(settle(p, t0 + 1.0, 39)) == 39                # continues where it paused
    p.restart(t0 + 1.0)
    assert decode_index(settle(p, t0 + 1.0, 30)) == 30
    q = player(rate=2.0)
    assert decode_index(settle(q, 50.0, 0)) == 0
    assert decode_index(settle(q, 50.25, 15)) == 15                   # 2x speed


def test_end_actions_without_loop(player):
    hold = player(loop=False)
    settle(hold, 10.0, 0)
    assert decode_index(settle(hold, 12.5, 59)) == 59 and hold.stats.ended
    hide = player(loop=False, end_action="hide")
    settle(hide, 10.0, 0)
    assert hide.frame_at(12.5) is None


def test_blend_interpolation(player):
    p = player(interpolation="blend")
    a = settle(p, 0.0, 0)
    # blending needs frame 1 decoded too; frame_at() never blocks for it by design, so wait here
    end = time.monotonic() + 3
    while time.monotonic() < end and not any(abs(pts - 1 / 30) < 1e-3 for pts, _ in list(p._buf)):
        p.frame_at(0.0)
        time.sleep(0.01)
    mid = settle(p, 0.5 / 30)                   # halfway between frames 0 and 1
    b = settle(p, 1 / 30, 1)
    assert a is not None and b is not None and mid is not None
    probe_px = (60, 160)
    va, vm, vb = (int(x[probe_px][0]) for x in (a, mid, b))
    assert min(va, vb) < vm < max(va, vb)


def test_buffer_bounded(player):
    p = player()
    settle(p, 0.0, 0)
    time.sleep(0.5)
    assert len(p._buf) <= MAX_BUFFER


def test_pool_eviction_and_idle_sweep():
    pool = PlayerPool()
    info = probe(MP4)
    ps = [pool.get(("a", i), MP4, info, PlaybackSettings()) for i in range(PlayerPool.MAX_PLAYERS + 2)]
    assert len(pool.players()) == PlayerPool.MAX_PLAYERS
    same = pool.get(("a", PlayerPool.MAX_PLAYERS + 1), MP4, info, PlaybackSettings())
    assert same is ps[-1]
    changed = pool.get(("a", PlayerPool.MAX_PLAYERS + 1), MP4, info, PlaybackSettings(rate=2))
    assert changed is not same                                     # settings change -> new player
    for p in pool.players().values():
        p.last_used -= 10
    pool.sweep()
    assert pool.players() == {}
    pool.close_all()


# ------------------------------------------------------------------ assets + compositing
@pytest.fixture
def lib(tmp_path):
    return AssetLibrary(tmp_path / "assets")


def test_import_video_asset(lib, tmp_path):
    a = lib.import_file(MP4)
    assert (a.kind, a.width, a.height, a.fps, a.duration, a.decoder, a.hardware) == ("video", 320, 180, 30.0, 2.0, "avfoundation", True)
    assert lib.thumbnail(a).shape[2] == 4
    bad = tmp_path / "broken.mp4"; bad.write_bytes(b"\x00" * 1000)
    with pytest.raises(AssetError):
        lib.import_file(bad)


def test_compositor_shows_correct_video_frame(lib):
    a = lib.import_file(MP4)
    roi = new_rect("V", 0.0, 0.0, 1.0, 1.0)
    regs = resolve([roi], [], 640, 360)
    spec = ReplacementSpec(enabled=True, mode="rect", asset_id=a.id, fit="stretch")
    c = Compositor(lib)
    frame = np.zeros((360, 640, 4), np.uint8)
    try:
        for t, exp in ((100.0, 0), (100.5, 15), (101.0, 30)):
            end = time.monotonic() + 2
            while time.monotonic() < end:
                out = c.render(frame, regs, {roi.id: spec}, {a.id: a}, t)
                if c.stats.regions_drawn and decode_index(out) == exp:
                    break
                time.sleep(0.01)
            assert decode_index(out) == exp
        assert c.stats.video_players == 1
    finally:
        c.close()


def test_per_object_sync_gives_independent_timelines(lib):
    a = lib.import_file(MP4)
    roi = ROI(name="Objs", mode="auto", padding=0.0)
    spec = ReplacementSpec(enabled=True, mode="rect", asset_id=a.id, fit="stretch", video_sync="per_object")
    c = Compositor(lib)
    frame = np.zeros((360, 1280, 4), np.uint8)
    o1 = TrackedObject(1, "person", 0.9, 0, 0, 640, 360, "tracked", 1, 0)
    o2 = TrackedObject(2, "person", 0.9, 640, 0, 1280, 360, "tracked", 1, 0)
    try:
        c.render(frame, resolve([roi], [o1], 1280, 360), {roi.id: spec}, {a.id: a}, 0.0)   # object 1 appears at t=0
        t = 1.0
        ok = False
        for _ in range(200):
            out = c.render(frame, resolve([roi], [o1, o2], 1280, 360), {roi.id: spec}, {a.id: a}, t)  # object 2 appears at t=1
            if decode_index(out[:, :640]) == 30 and decode_index(out[:, 640:]) == 0:
                ok = True
                break
            time.sleep(0.01)
        assert ok and c.stats.video_players == 2
    finally:
        c.close()


def test_video_failure_isolated(lib, tmp_path):
    a = lib.import_file(MP4)
    lib.path(a).unlink()                                 # file vanishes after import
    roi = new_rect("V", 0, 0, 0.5, 0.5)
    good = new_rect("G", 0.5, 0.5, 1, 1)
    img = lib.import_file(DATA / "logo_alpha.png")
    specs = {roi.id: ReplacementSpec(enabled=True, asset_id=a.id), good.id: ReplacementSpec(enabled=True, asset_id=img.id)}
    c = Compositor(lib)
    try:
        c.render(np.zeros((360, 640, 4), np.uint8), resolve([roi, good], [], 640, 360), specs, {a.id: a, img.id: img}, 0.0)
        assert roi.id in c.stats.failed_rois and good.id not in c.stats.failed_rois and c.stats.regions_drawn == 1
    finally:
        c.close()
