"""One shape for every tool that lists rows, so "show me the rest" works.

A paged tool takes `limit` and `offset` and answers with the total `count`,
the `offset` it started at, the rows, and `next_offset`: the offset of the
next page, or None when this page was the last. A tool that fetches a
server-paged list passes the server's total as `total` and the rows it
fetched for this window; a tool over a local list passes the whole list.
"""

DEFAULT, CAP = 10, 40


def properties(default=DEFAULT, cap=CAP, what="rows"):
    """The two schema properties every paged tool carries."""
    return {
        "limit": {
            "type": "integer",
            "description": f"{what} per page, default {default}, at most {cap}",
        },
        "offset": {
            "type": "integer",
            "description": "rows to skip; pass the previous page's next_offset "
            "for the rest of the list",
        },
    }


def window(args, default=DEFAULT, cap=CAP):
    """(limit, offset) from the args, clamped; (None, error) when malformed."""
    try:
        limit = int(args.get("limit") or default)
        offset = int(args.get("offset") or 0)
    except (TypeError, ValueError):
        return None, {"ok": False, "error": "limit and offset must be integers"}
    if offset < 0:
        return None, {"ok": False, "error": "offset must not be negative"}
    # No ceiling on the offset: a clamped offset would hand back the same
    # page with the same continuation, forever.
    return (max(1, min(limit, cap)), offset), None


def page(rows, args, key, default=DEFAULT, cap=CAP, total=None, **extra):
    """The result dict for one page of `rows` under `key`. `rows` is the
    whole list unless `total` says it is a server-fetched window that
    already starts at the offset."""
    bounds, err = window(args, default, cap)
    if err:
        return err
    limit, offset = bounds
    if total is None:
        total = len(rows)
        chunk = rows[offset : offset + limit]
    else:
        chunk = rows[:limit]
    after = offset + len(chunk)
    return {
        "ok": True,
        **extra,
        "count": total,
        "offset": offset,
        key: chunk,
        # An empty page is the end whatever the total said: a continuation
        # that does not advance is a loop.
        "next_offset": after if chunk and after < total else None,
    }
