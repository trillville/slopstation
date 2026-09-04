"""Record provider-executed searches that Pipecat does not expose."""


def search_item(item):
    """Return the kind and query for a provider search item."""
    kind = getattr(item, "type", "") or ""
    if "search" not in kind:
        return None
    action = getattr(item, "action", None)
    query = (
        getattr(action, "query", None)
        or getattr(item, "query", None)
        or (str(action)[:200] if action else "")
    )
    return kind, str(query or "")


class _Tee:
    """Pass an async stream through while calling a sink for each event."""

    def __init__(self, stream, sink):
        self._stream, self._sink = stream, sink

    def __aiter__(self):
        return self._passthrough(self._stream.__aiter__())

    async def _passthrough(self, it):
        async for event in it:
            try:
                self._sink(event)
            except Exception:
                pass  # never break the conversation
            yield event

    async def close(self):
        await self._stream.close()


def install(service, log, spans=None, context=None):
    """Wrap an OpenAI client to record server-side searches."""
    try:
        responses = service._client.responses
        create = responses.create
    except AttributeError:
        return False  # pipecat moved; not fatal

    seen = []  # searches from the current turn

    def sink(event):
        item = getattr(event, "item", None)
        if item is None:
            return
        # Record the completed item only; added and done contain the same item.
        if "done" not in (getattr(event, "type", "") or ""):
            return
        hit = search_item(item)
        if not hit:
            return
        kind, query = hit
        status = getattr(item, "status", None)
        seen.append(query)
        log("web_search", query=query[:300], kind=kind, status=status)
        if spans is not None:
            # Nests under Pipecat's open span for this LLM call.
            spans.tool_span(kind, query, status)

    async def audited(**params):
        response = await create(**params)
        return _Tee(response, sink) if params.get("stream") else response

    responses.create = audited

    if context is not None:
        _feed_back(service, context, seen)
    return True


def _feed_back(service, context, seen):
    """Add completed searches when the next model context is built."""
    original = service._process_context

    async def _process_context(ctx):
        if seen:
            try:
                ctx.add_message(
                    {
                        "role": "system",
                        "content": "[For your own reference: you ran these web "
                        "searches on the previous turn and answered "
                        "from their results - "
                        + "; ".join(seen[:5])
                        + ". If asked where that answer came from, "
                        "this is the answer.]",
                    }
                )
            except Exception:
                pass
            seen.clear()
        return await original(ctx)

    service._process_context = _process_context
