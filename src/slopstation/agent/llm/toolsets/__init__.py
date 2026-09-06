"""One module per area of tools. Each exports SPECS and impls(ctx).

The order here is the order the model sees the tools in, so it stays fixed:
a stable tool list is a stable cache prefix."""

from slopstation.agent.llm.toolsets import (
    house,
    media,
    passthrough,
    rig,
    search,
    steam,
    storage,
    torrents,
)

ALL = (search, rig, steam, house, media, torrents, storage, passthrough)
