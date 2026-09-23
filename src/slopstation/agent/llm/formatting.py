"""Formatting shared by assistant tools."""


def gigabytes(value, places=2):
    return round(int(value or 0) / 1024**3, places)
