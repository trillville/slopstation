"""Tools that read the house's own state: operations, and later the lanes."""

from slopstation.agent.llm import paging
from slopstation.agent.llm.registry import Bindings, ToolContext, ToolSpec

LIST_OPERATIONS = """\
Read Slopstation's durable operations. Use scope 'active' for current work and
'recent' for what just finished or what an announcement referred to. Use this
for every general question about current downloads, installs, searches, waiting
work, imports, or recent completion: current operation state never comes from
the catalog or conversation memory. Report each operation's actual phase: only
phase=downloading is downloading; name every other phase accurately. Never
infer current state from conversation history, the catalog, or an absent
download. Several operations can share a title: report their scopes separately;
a search only promises a search, not a new file. Returns the count and one page
of rows."""

SPECS = [
    ToolSpec(
        "list_operations",
        LIST_OPERATIONS,
        {
            "scope": {"type": "string", "enum": ["active", "recent"]},
            **paging.properties(cap=25),
        },
        (),
        risk="read",
        area="house",
        keywords=(
            "what is downloading",
            "status",
            "progress",
            "queue",
            "recently finished",
            "operations",
        ),
        needs=("operations",),
        paged=True,
    ),
]


def impls(ctx: ToolContext):
    bind = Bindings(ctx, SPECS)
    dispatch, operations = ctx.dispatch, ctx.operations

    @bind
    def list_operations(args):
        scope = args.get("scope", "active")
        if scope not in ("active", "recent"):
            return {"ok": False, "error": f"unknown operation scope {scope}"}
        bounds, err = paging.window(args, cap=25)
        if err:
            return err
        limit, offset = bounds
        rows, total = operations.for_assistant(
            scope,
            limit=limit,
            offset=offset,
            acknowledge=(scope == "recent" and not dispatch.dry_run),
        )
        return paging.page(rows, args, "operations", cap=25, total=total, scope=scope)

    return bind.impls()
