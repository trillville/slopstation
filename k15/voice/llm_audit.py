"""Provider-executed tool calls, made visible.

THE HOLE THIS FILLS. OpenAI's web_search runs server-side, inside the same
API call as the completion. Pipecat 1.7's Responses service handles exactly
two output item types - function_call and reasoning - and there is no
`web_search` anywhere in it. So a search streams past, is ignored, pushes no
frame, and never enters the context. Consequences, both observed for real on
2026-08-11:

  * Nothing recorded that a lookup happened. Five correct, searched game
    recommendations were written off as hallucinations, and it took a store
    page and a token-count comparison to establish otherwise.
  * The MODEL could not tell either. Asked where the picks came from, it saw
    only its own prior text - the searches were not in its context - so it
    reasoned about plausibility and disowned a good answer.

The second one is why this module writes back into the context rather than
only emitting telemetry. Observability alone would have let us audit the
model while leaving the model itself amnesiac.

WHY A STREAM TEE AND NOT A HOOK. There is no hook. LLMService registers
event handlers only for on_function_calls_started / _cancelled /
on_completion_timeout / on_connection_error - all function-call shaped.
Observers see FRAMES, and no frame is ever pushed for a server-side tool, so
an observer sees nothing. What is left is the stream itself, and the smallest
honest seam is to wrap the iterator: every event still reaches Pipecat
untouched and in order, we only look. Nothing here reimplements Pipecat's
handling, so its parsing can change freely without breaking this.
"""
log = None                                      # set by install()


def _search_item(item):
    """-> (kind, query) for a provider-executed search item, else None.

    Matches on the type CONTAINING 'search' rather than on an exact string:
    the family has grown before (web_search_call, file_search_call, and
    provider-specific spellings) and a new member should be recorded, not
    silently dropped the way the whole class was until today."""
    kind = getattr(item, "type", "") or ""
    if "search" not in kind:
        return None
    action = getattr(item, "action", None)
    query = (getattr(action, "query", None)
             or getattr(item, "query", None)
             or (str(action)[:200] if action else ""))
    return kind, str(query or "")


class _Tee:
    """Passes an OpenAI AsyncStream through unchanged, calling `sink` for
    each event. Mirrors the surface Pipecat uses on the real stream:
    __aiter__ (whose result it closes first), then close/aclose."""

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


def install(service, logger, tracing=None, context=None):
    """Wrap `service`'s OpenAI client so server-side searches are recorded.

    Fail-soft in every direction: a Pipecat internal that has moved leaves
    the service exactly as it was and voice runs on. Returns True if the
    audit is live, so startup can say which."""
    global log
    log = logger
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
        # output_item.added fires before the search runs and .done after, so
        # both carry the item. Only record on `done`, where the query is
        # final and a status exists - otherwise every search is logged twice.
        if "done" not in (getattr(event, "type", "") or ""):
            return
        hit = _search_item(item)
        if not hit:
            return
        kind, query = hit
        status = getattr(item, "status", None)
        seen.append(query)
        log("web_search", query=query[:300], kind=kind, status=status)
        if tracing is not None:
            # Nests under whatever span Pipecat has open for this LLM call,
            # so in Langfuse the search sits beside the turn that ran it -
            # the same shape the background worker's tool spans use.
            tracing.tool_span(kind, query, status)

    async def audited(**params):
        return _Tee(await create(**params), sink)

    responses.create = audited

    if context is not None:
        _feed_back(service, context, seen)
    return True


def _feed_back(service, context, seen):
    """Put the searches into the model's own context.

    Written on the NEXT context build, never mid-stream: mutating the context
    while the aggregator is mid-turn is how you corrupt a conversation. One
    turn late is the right trade - by then the assistant's reply has been
    appended, so the note reads as a true statement about a moment that has
    already passed, and the model stops having to guess whether it looked
    anything up."""
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
