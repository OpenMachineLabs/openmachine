"""Bounded GitHub REST pagination and GraphQL requests."""

from openmachine.foundation.sanitize import Fault

from .connection import object_response


MAX_GATE_PAGES = 10


def list_page(endpoint: str, page: int, limit: int, key: str = "", *, transport) -> tuple[list, int | None]:
    separator = "&" if "?" in endpoint else "?"
    value = transport("GET", f"{endpoint}{separator}per_page={limit}&page={page}")
    items = object_response(value).get(key) if key else value
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise Fault("github_invalid_response", "GitHub pagination returned an incomplete or invalid list.")
    if len(items) > limit:
        raise Fault("github_invalid_response", "GitHub exceeded the requested list limit.")
    if key == "statuses" and value.get("sha") != endpoint.split("/commits/", 1)[1].split("/", 1)[0]:
        raise Fault("github_evidence_unavailable", "Status checks refer to a different commit.")
    total = value.get("total_count") if key else None
    if key and (type(total) is not int or total < 0):
        raise Fault("github_invalid_response", "GitHub returned an invalid list count.")
    if total is not None and len(items) != min(limit, max(0, total - (page - 1) * limit)):
        raise Fault("github_evidence_unavailable", "The returned page does not match its reported count; re-read the changed or incomplete evidence.")
    more = page * limit < total if total is not None else len(items) == limit
    if more and not items:
        raise Fault("github_evidence_unavailable", "GitHub omitted a required page; do not treat missing evidence as empty.")
    return items, page + 1 if more else None


def all_pages(endpoint: str, key: str = "", *, transport) -> list:
    items = []
    for page in range(1, MAX_GATE_PAGES + 1):
        batch, following = list_page(endpoint, page, 100, key, transport=transport)
        items.extend(batch)
        if following is None:
            return items
    raise Fault("github_evidence_unavailable", "The gate exceeds 1,000 records; evidence is incomplete and no review or merge was submitted.")


def graphql_pr(repository: str, number: int, fields: str, cursor: str = "", limit: int = 100, *, transport) -> dict:
    owner, name = repository.split("/")
    paging = ", $cursor:String, $limit:Int!" if "$cursor" in fields else ""
    query = ("query($owner:String!, $name:String!, $number:Int!" + paging + "){repository(owner:$owner,name:$name){"
             "pullRequest(number:$number){headRefOid baseRefOid " + fields + "}}}")
    variables = {"owner": owner, "name": name, "number": number}
    if paging:
        variables.update(cursor=cursor or None, limit=limit)
    result = object_response(transport("POST", "graphql", {"query": query, "variables": variables}))
    if result.get("errors"):
        raise Fault("github_evidence_unavailable", "GitHub could not verify requested policy/discussion evidence; no partial GraphQL success is accepted.")
    try:
        return object_response(result["data"]["repository"]["pullRequest"])
    except (KeyError, TypeError) as error:
        raise Fault("github_evidence_unavailable", "The requested PR policy/discussion evidence is unavailable.") from error
