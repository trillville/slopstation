"""Pipecat function schemas for a conversation's loaded tools: the voice lane's
adapter over Tools.call. The text lane calls Tools directly."""


def pipecat_schemas(tools):
    """Pipecat schemas whose handlers run `tools.call` on a worker thread. An
    acknowledgment is spoken as-is with no second model turn; end_turn closes
    the turn without a reply."""
    import asyncio

    from pipecat.adapters.schemas.function_schema import FunctionSchema
    from pipecat.frames.frames import FunctionCallResultProperties, TTSSpeakFrame

    def wrap(name):
        async def handler(params):
            # `call` never raises and records the call itself. Contextvars are
            # per task, so the span still parents onto Pipecat's llm span.
            out = await asyncio.to_thread(tools.call, name, dict(params.arguments))
            acknowledgment = (
                out.get("acknowledgment") if isinstance(out, dict) else None
            )
            end_turn = isinstance(out, dict) and bool(out.get("end_turn"))
            if end_turn:
                # The session ends on this call: nothing is spoken.
                await params.result_callback(
                    out, properties=FunctionCallResultProperties(run_llm=False)
                )
            elif acknowledgment:

                async def speak():
                    await params.pipeline_worker.queue_frame(
                        TTSSpeakFrame(str(acknowledgment))
                    )

                properties = FunctionCallResultProperties(
                    run_llm=False, on_context_updated=speak
                )
                await params.result_callback(out, properties=properties)
            else:
                await params.result_callback(out)

        return handler

    return [
        FunctionSchema(
            name=spec.name,
            description=spec.description,
            properties=spec.properties,
            required=list(spec.required),
            handler=wrap(spec.name),
        )
        for spec in tools.registry.select(list(tools.loaded))
    ]
