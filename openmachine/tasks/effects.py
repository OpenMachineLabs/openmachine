"""Small resource scopes for retained effects, separate from operation identity."""

from . import records


def github_scope(method: str, arguments: dict) -> dict:
    """Derive only replaced resources; callers cannot provide their own scope."""
    kind = method.removeprefix("github_")
    action = arguments.get("action")
    number = arguments.get("number")
    positive = lambda value: value if type(value) is int and value > 0 else None
    scope = {"resource": "object",
             "number": positive(number), "parent_number": None, "fields": ["*"]}
    if kind == "project":
        scope.update(resource="project", number=positive(arguments.get("project_number")))
    elif kind in {"issue", "pull_request"}:
        if action == "create":
            scope.update(number=None, fields=["create"])
            if kind == "issue":
                scope["creation_key"] = records.digest([kind])
            elif arguments.get("head") and arguments.get("base"):
                owner, colon, branch = arguments["head"].partition(":")
                if not colon:
                    owner, branch = arguments["repository"].split("/", 1)[0], owner
                scope["creation_key"] = records.digest([kind, owner.casefold(), branch, arguments["base"]])
        elif action == "update":
            scope["fields"] = sorted(key for key in ("title", "body", "state", "labels", "assignees")
                                     if arguments.get(key) is not None) or ["*"]
        elif kind == "pull_request" and action == "ready_for_review":
            scope["fields"] = ["draft"]
    elif kind == "merge":
        # Omitted merge messages can be derived by GitHub from PR metadata.
        # These read dependencies must not race an unresolved replacement.
        scope["fields"] = sorted(["merge"] + (["title"] if not arguments.get("commit_title") else [])
                                 + (["body"] if not arguments.get("commit_message") else []))
    elif kind == "review_thread":
        scope["fields"] = ["threads"]
    elif kind in {"comment", "review"}:
        scope.update(resource=kind, number=positive(arguments.get(kind + "_id")),
                     parent_number=positive(number), fields=["body"])
        if action == "create":
            scope["number"] = None
            scope["fields"] = ["create"]
        elif kind == "comment":
            # Comment updates address the global comment ID; the API does not
            # use the caller's parent number to select that comment.
            scope["parent_number"] = None
    return records.GitHubEffectScope.model_validate(scope).model_dump()


def operation_scope(record: dict, operation: dict) -> dict | None:
    locator = operation["locator"]
    if locator["kind"] != "github":
        return None
    declared = operation.get("effect_scope")
    if declared is None:
        witness = next((item for item in record.get("effect_scopes", [])
                        if item["operation_id"] == operation["id"]), None)
        declared = witness["scope"] if witness is not None else None
    if declared is not None:
        repository = locator["repository"].casefold()
        if declared["resource"] == "project":
            repository = repository.split("/", 1)[0]
        value = dict(declared, repository=repository)
        # A later verified destination can narrow an unknown creation's target.
        if value["number"] is None and locator.get("number") is not None:
            value["number"] = locator["number"]
        return value
    kind = locator["object_type"]
    repository = locator["repository"].casefold()
    return {"repository": repository.split("/", 1)[0] if kind == "project" else repository,
            "resource": kind if kind in {"comment", "review", "project"} else "object",
            "number": locator.get("number"),
            "parent_number": locator.get("parent_number") if kind == "review" else None,
            "fields": ["*"]}


def github_conflict(first: dict, second: dict) -> bool:
    if first["repository"].casefold() != second["repository"].casefold():
        return False
    left, right = first["resource"], second["resource"]
    if left == right == "project":
        return first["number"] is None or second["number"] is None or first["number"] == second["number"]
    unnumbered = [item for item in (first, second)
                  if item["resource"] == "object" and item["number"] is None]
    if unnumbered:
        # Historical opaque creations without a proven source remain conservative.
        if any(item["fields"] != ["create"] or not item.get("creation_key") for item in unnumbered):
            return True
        # A new PR cannot replace any already-numbered Issue/PR or review.
        return (len(unnumbered) == 2 and left == right == "object"
                and first["creation_key"] == second["creation_key"])
    if left == right == "object":
        if first["number"] != second["number"]:
            return False
        a, b = set(first["fields"]), set(second["fields"])
        lifecycle = {"state", "draft", "merge", "threads"}
        return "*" in a or "*" in b or "create" in a or "create" in b or bool(a & b) or bool(a & lifecycle and b & lifecycle)
    if left == right:
        if first["number"] is not None and second["number"] is not None:
            return first["number"] == second["number"]
        a, b = first["parent_number"], second["parent_number"]
        return a is None or b is None or a == b
    if {left, right} == {"object", "review"}:
        core, review = (first, second) if left == "object" else (second, first)
        same_parent = review["parent_number"] is None or core["number"] == review["parent_number"]
        submitted_review = bool(set(review["fields"]) & {"*", "create"})
        return same_parent and submitted_review and bool(set(core["fields"]) & {"*", "state", "draft", "merge"})
    return False


def unresolved(operation: dict) -> bool:
    return records.pending(operation) or (operation.get("result") or {}).get("status") == "partial"


def conflicts(first_record: dict, first: dict, second_record: dict, second: dict) -> bool:
    a, b = first["locator"], second["locator"]
    if a["kind"] == "github" or b["kind"] == "github":
        return a["kind"] == b["kind"] and github_conflict(
            operation_scope(first_record, first), operation_scope(second_record, second))
    # Uncertain local execution or partial file effects retain the target-wide
    # exclusion. Different configured targets have independent worker owners.
    return (a["target_identity"] or "local") == (b["target_identity"] or "local")
