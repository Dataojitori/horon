"""
Tag: exit

Each tag cluster (e.g. 求職, 変現, Bluesky) may have at most ONE concept
carrying this tag. The exit is the single convergence point where all paths
in a domain must flow — like rivers converging into the sea.

Naming rule: exit-tagged concepts must be named "{tag_group_name}-exit".
Examples: Salem的求職project-exit, 我的变现探索-exit.
The concept must also carry the corresponding group tag.

on_mutation: when exit tag is added, validate name format + group tag.
audit: detect duplicate exits per group, naming violations, missing group tag.
"""

DESCRIPTION = (
    "on_mutation: rejects if name is not '{group}-exit' or group tag is missing.\n"
    "audit: flags duplicate exits per group, naming violations, orphaned exits."
)

_SUFFIX = "-exit"
_SUFFIX_LEN = 5  # len("-exit") = 5


def on_mutation(ctx):
    tags_change = ctx.changed.get("tags")
    if not tags_change:
        return

    adding_exit = False
    for a in tags_change.get("added", []):
        if a.get("tag") == ctx.tag_name and a.get("concept_id") == ctx.this.concept_id:
            adding_exit = True
            break

    if not adding_exit:
        return

    name = ctx.this.name

    if not name.endswith(_SUFFIX):
        ctx.reject(
            "exit concept must be named '{group_tag}-exit'. "
            "Current name: '" + name + "'. "
            "Fix: set " + name + " name {group}-exit"
        )
        return

    group_tag = name[:-_SUFFIX_LEN]
    if not group_tag:
        ctx.reject("'" + _SUFFIX + "' must be preceded by a group tag name.")
        return

    if group_tag not in ctx.this.tags:
        ctx.reject(
            "exit concept must also carry group tag '" + group_tag + "'. "
            "Fix: add " + name + " tag " + group_tag
        )
        return

    ctx.info(
        "'" + name + "' registered as the sole exit of '" + group_tag + "'. "
        "Run: audit exit"
    )


def audit_cluster(ctx):
    groups = {}

    for c in ctx.cluster.concepts:
        name = c.name

        # skip the source concept itself (named "exit")
        if name == ctx.tag_name:
            continue

        if not name.endswith(_SUFFIX):
            ctx.warn(
                "[naming] " + name + " (id=" + str(c.concept_id) +
                ") has exit tag but name does not end with '" + _SUFFIX + "'"
            )
            continue

        group = name[:-_SUFFIX_LEN]

        if group not in c.tags:
            ctx.warn(
                "[orphan] " + name + " (id=" + str(c.concept_id) +
                ") claims group '" + group + "' but lacks that tag"
            )

        if group not in groups:
            groups[group] = []
        groups[group].append(c)

    for group, concepts in groups.items():
        if len(concepts) > 1:
            names = ", ".join(
                [c.name + "(id=" + str(c.concept_id) + ")" for c in concepts]
            )
            ctx.warn(
                "[duplicate] group '" + group + "' has " +
                str(len(concepts)) + " exits: " + names +
                " — only one allowed per group"
            )
