"""couch.log rotation: two generations, keyed on size."""

from slopstation import logbook, paths


def test_rotate_moves_a_big_log_aside():
    log = paths.couch_log()
    log.parent.mkdir()
    log.write_bytes(b"x" * 10)
    logbook.rotate(max_bytes=5)
    assert not log.exists()
    assert (paths.logs() / "couch.log.1").read_bytes() == b"x" * 10


def test_rotate_leaves_a_small_log_alone():
    log = paths.couch_log()
    log.parent.mkdir()
    log.write_text("small")
    logbook.rotate(max_bytes=5_000_000)
    assert log.exists()
    assert not (paths.logs() / "couch.log.1").exists()
