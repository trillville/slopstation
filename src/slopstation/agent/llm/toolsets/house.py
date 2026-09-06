"""Tools that read the house's own state: operations, and later the lanes."""

from slopstation.agent.llm.registry import ToolContext, ToolSpec

LIST_OPERATIONS = """\
Read Slopstation's durable operations. Use scope 'active' for current work and
'recent' for what just finished or what an announcement referred to. Use this
for every general question about current downloads, installs, searches, waiting
work, imports, or recent completion: current operation state never comes from
the catalog or conversation memory. Report each operation's actual phase: only
phase=downloading is downloading; name every other phase accurately. Never
infer current state from conversation history, the catalog, or an absent
download. Returns the count and up to `limit` rows."""

LIMIT_DEFAULT = 10
LIMIT_MAX = 25

SPECS = [
    ToolSpec(
        "list_operations",
        LIST_OPERATIONS,
        {
            "scope": {"type": "string", "enum": ["active", "recent"]},
            "limit": {
                "type": "integer",
                "description": f"rows to return, default {LIMIT_DEFAULT}, "
                f"at most {LIMIT_MAX}",
            },
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
    dispatch, operations = ctx.dispatch, ctx.operations

    def list_operations(args):
        scope = args.get("scope", "active")
        if scope not in ("active", "recent"):
            return {"ok": False, "error": f"unknown operation scope {scope}"}
        try:
            limit = int(args.get("limit") or LIMIT_DEFAULT)
        except (TypeError, ValueError):
            return {"ok": False, "error": "limit must be an integer"}
        limit = max(1, min(limit, LIMIT_MAX))
        rows = operations.for_assistant(
            scope, limit=limit, acknowledge=(scope == "recent" and not dispatch.dry_run)
        )
        return {"ok": True, "scope": scope, "count": len(rows), "operations": rows}

    return {"list_operations": list_operations}
