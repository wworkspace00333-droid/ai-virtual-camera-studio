"""Every PyObjC symbol used in the source must be pre-resolved (lazy resolution is not thread-safe)."""
import re
import threading
from pathlib import Path

from vcstudio.core.pyobjc_prewarm import ALIASES, SYMBOLS, prewarm

SRC = Path(__file__).resolve().parents[1] / "vcstudio"


def test_prewarm_list_covers_source():
    used = set()
    for f in SRC.rglob("*.py"):
        for alias, name in re.findall(r"\b(AV|CM|Q|CoreML|Vision|Foundation|VT)\.([A-Za-z_][A-Za-z0-9_]*)", f.read_text()):
            used.add((ALIASES[alias], name))
    listed = {(m, n) for m, names in SYMBOLS.items() for n in names}
    missing = sorted(used - listed)
    assert not missing, f"add to vcstudio/core/pyobjc_prewarm.py: {missing}"


def test_prewarm_resolves_everything():
    assert prewarm() == []


def test_concurrent_video_players_start_cleanly(tmp_path):
    """Regression for the stress-test failure: six decoders with start offsets starting at once."""
    from vcstudio.replacement.video import PlaybackSettings, VideoPlayer, probe
    clip = Path(__file__).parent / "data" / "clip_h264.mp4"
    info = probe(clip)
    players = []
    barrier = threading.Barrier(6)

    def make(i):
        barrier.wait()
        players.append(VideoPlayer(clip, info, PlaybackSettings(start_offset_s=0.3 * i)))
    ts = [threading.Thread(target=make, args=(i,)) for i in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    import time
    end = time.monotonic() + 3
    while time.monotonic() < end and not all(p.frame_at(1.0) is not None for p in players):
        time.sleep(0.05)
    try:
        assert all(not p.failed for p in players), [p.stats.error for p in players]
        assert all(p.frame_at(1.0) is not None for p in players)
    finally:
        for p in players:
            p.close()
