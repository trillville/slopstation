"""Render a conversation's loaded tools as Pipecat function schemas. This is
the voice lane's adapter over the shared Tools.call: the text lane calls the
same Tools without any of this."""

from slopstation.agent.llm.assistant import as_tools


def function_schemas(impls, log):
    """Pipecat schemas for a bare impls dict (tests, the REPL)."""
    return pipecat_schemas(as_tools(impls, log), log)


def pipecat_schemas(tools, log):
    """Pipecat schemas whose handlers run `tools.call` in a worker thread and
    turn the result into speech: an acknowledgment is spoken as-is with no
    second model turn, and end_turn closes the turn to a closing mic."""
    import asyncio

    from pipecat.adapters.schemas.function_schema import FunctionSchema
    from pipecat.frames.frames import FunctionCallResultProperties, TTSSpeakFrame

    def wrap(name):
        async def handler(params):
            # `call` never raises and records the call itself. The await does
            # not lose the OTel context (contextvars are per-task), so the
            # span still parents onto Pipecat's llm span.
            out = await asyncio.to_thread(tools.call, name, dict(params.arguments))
            acknowledgment = (
                out.get("acknowledgment") if isinstance(out, dict) else None
            )
            end_turn = isinstance(out, dict) and bool(out.get("end_turn"))
            if end_turn:
                # The session ends on this call: no goodbye to a closing mic.
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
