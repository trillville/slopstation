"""couch.log rotation: two generations, keyed on size."""

from slopstation import logbook, paths


def test_rotate_moves_only_a_big_log_aside():
    log = paths.couch_log()
    log.parent.mkdir()
    log.write_bytes(b"x" * 10)
    rotated = paths.logs() / "couch.log.1"
    logbook.rotate(max_bytes=5_000_000)
    assert log.exists() and not rotated.exists()
    logbook.rotate(max_bytes=5)
    assert not log.exists()
    assert rotated.read_bytes() == b"x" * 10
