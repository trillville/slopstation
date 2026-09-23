"""Byte counts in gigabytes, the unit the media code and the tools report."""

GB = 1024**3


def gigabytes(value, places=2):
    return round(int(value or 0) / GB, places)
