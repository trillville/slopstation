"""Provider-executed tool calls, made visible.

OpenAI's web_search runs server-side inside the completion call, and pipecat's
Responses service (still in 1.8.1) handles only function_call and reasoning
items - a search streams past ignored, pushes no frame, and never enters the
context.
So nothing records the lookup and the model itself cannot tell it searched;
hence the write-back into the context, not just telemetry.

A stream tee, not a hook: LLMService's event handlers are function-call
shaped and observers see frames, of which a server-side tool pushes none.
Every event reaches Pipecat untouched and in order.
"""
def search_item(item):
    """-> (kind, query) for a provider-executed search item, else None.
    Matches a type CONTAINING 'search', not an exact string: the family grows
    (web_search_call, file_search_call, ...)."""
    kind = getattr(item, "type", "") or ""
    if "search" not in kind:
        return None
    action = getattr(item, "action", None)
    query = (getattr(action, "query", None)
             or getattr(item, "query", None)
             or (str(action)[:200] if action else ""))
    return kind, str(query or "")


class _Tee:
    """Passes an OpenAI AsyncStream through unchanged, calling `sink` for each
    event. Mirrors the surface Pipecat uses: __aiter__, then close/aclose."""

    def __init__(self, stream, sink):
        self._stream, self._sink = stream, sink

    def __aiter__(self):
        return self._passthrough(self._stream.__aiter__())

    async def _passthrough(self, it):
        async for event in it:
            try:
                self._sink(event)
            except Exception:
                pass                            # never break the conversation
            yield event

    async def close(self):
        await self._close()

    async def aclose(self):
        await self._close()

    async def _close(self):
        for name in ("close", "aclose"):
            fn = getattr(self._stream, name, None)
            if fn:
                try:
                    await fn()
                except Exception:
                    pass
                return


def install(service, log, tracing=None, context=None):
    """Wrap `service`'s OpenAI client so server-side searches are recorded.
    Fail-soft: a Pipecat internal that has moved leaves the service untouched
    and voice runs on. True if the audit is live."""
    try:
        responses = service._client.responses
        create = responses.create
    except AttributeError:
        return False                            # pipecat moved; not fatal

    seen = []                                   # searches from the current turn

    def sink(event):
        item = getattr(event, "item", None)
        if item is None:
            return
        # Both output_item.added and .done carry the item; record only on
        # `done` (final query, status present) or every search logs twice.
        if "done" not in (getattr(event, "type", "") or ""):
            return
        hit = search_item(item)
        if not hit:
            return
        kind, query = hit
        status = getattr(item, "status", None)
        seen.append(query)
        log("web_search", query=query[:300], kind=kind, status=status)
        if tracing is not None:
            # Nests under Pipecat's open span for this LLM call.
            tracing.tool_span(kind, query, status)

    async def audited(**params):
        return _Tee(await create(**params), sink)

    responses.create = audited

    if context is not None:
        _feed_back(service, context, seen)
    return True


def _feed_back(service, context, seen):
    """Put the searches into the model's own context, on the NEXT context
    build - mutating it while the aggregator is mid-turn corrupts the
    conversation."""
    original = service._process_context

    async def _process_context(ctx):
        if seen:
            try:
                ctx.add_message({
                    "role": "system",
                    "content": "[For your own reference: you ran these web "
                               "searches on the previous turn and answered "
                               "from their results - " + "; ".join(seen[:5])
                               + ". If asked where that answer came from, "
                               "this is the answer.]"})
            except Exception:
                pass
            seen.clear()
        return await original(ctx)

    service._process_context = _process_context
