"""The one tool that acts on the tool set itself: find_tools."""

from slopstation.agent.llm import toolsearch
from slopstation.agent.llm.registry import AREAS, ToolContext, ToolSpec

FIND_TOOLS = """\
Find and load more tools. Only a small set is loaded at first; many more
exist for torrents, disk space, movie and TV management, Steam data and
downloads, the house's own state, and raw API calls. Call this BEFORE saying
something cannot be done: pass what the user wants in a few plain words
('pause a torrent', 'what airs this week', 'free disk space'). The matches
are loaded at once and stay loaded, so call them straight after. On a miss
the result lists the areas and how many tools each holds: search again with
other words, or say plainly that there is no tool for it - never guess."""

SPECS = [
    ToolSpec(
        "find_tools",
        FIND_TOOLS,
        {
            "query": {
                "type": "string",
                "description": "what the user wants, in plain words",
            }
        },
        ("query",),
        risk="read",
        area="house",
        keywords=("find tools", "more tools", "can you", "is there a way", "how do i"),
    ),
]


def impls(ctx: ToolContext):
    def find_tools(args):
        query = str(args.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "say what the user wants, in a few words"}
        toolkit = ctx.toolkit
        matches = toolsearch.search(
            toolkit.registry, query, exclude=set(toolkit.loaded)
        )
        if matches:
            names = toolkit.load([spec.name for spec, _ in matches])
            ctx.log("tools_found", query=query[:120], found=names, n=len(names))
            return {
                "ok": True,
                "loaded": [
                    {
                        "tool": spec.name,
                        "does": toolsearch.summary(spec),
                        "area": spec.area,
                        "risk": spec.risk,
                    }
                    for spec, _ in matches
                ],
                "detail": "these tools are loaded now - call the right one directly",
            }
        by_area = toolkit.registry.by_area(set(toolkit.offered) - set(toolkit.loaded))
        ctx.log("tools_found", query=query[:120], found=[], n=0)
        return {
            "ok": False,
            "error": "no tool matches those words",
            "areas": [
                {"area": area, "about": AREAS[area], "tools": len(specs)}
                for area, specs in by_area.items()
            ],
            "detail": "search again with words from an area above, or tell the "
            "user there is no tool for this",
        }

    return {"find_tools": find_tools}
