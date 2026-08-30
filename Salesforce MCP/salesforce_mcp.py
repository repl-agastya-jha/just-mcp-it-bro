#!/usr/bin/env python3
"""
Salesforce MCP server (read-only) for Claude Desktop.

Exposes tools to query, search, and inspect a Salesforce org over the
Model Context Protocol. Authenticates with the SOAP username-password-token
login (simple-salesforce), which works even where the OAuth 2.0 connected-app
password grant is disabled.

Credentials are read from environment variables (set them in the `env`
block of claude_desktop_config.json) — do NOT hardcode them in this file.

Required env vars:
  SF_USERNAME        Salesforce username (e.g. integrations@example.com)
  SF_PASSWORD        Salesforce password
  SF_SECURITY_TOKEN  Salesforce security token
Optional:
  SF_DOMAIN          "login" for production (default), "test" for sandbox,
                     or a My Domain host like "acme.my.salesforce.com"
"""

import json
import logging
import os
import re
import sys
import time

from mcp.server.fastmcp import FastMCP
from simple_salesforce import Salesforce

# Optional: load a local .env for testing with the MCP Inspector.
# In Claude Desktop, env vars come from the config `env` block instead.
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

try:
    from simple_salesforce.exceptions import SalesforceExpiredSession
except Exception:  # pragma: no cover
    SalesforceExpiredSession = None

# IMPORTANT: log to stderr only. Anything written to stdout corrupts the
# stdio JSON-RPC stream and silently breaks the connection to Claude Desktop.
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("salesforce-mcp")

mcp = FastMCP(
    "salesforce",
    instructions=(
        "Read-only access to a Salesforce org. Tools: search_cases (BEST for "
        "finding Cases about a topic — auto-searches Subject + Comments + "
        "Description/SOSL, merges and dedupes), search_salesforce (find any "
        "record by keyword when you don't know the object), describe_object "
        "(learn an object's fields before writing SOQL), soql_query (precise "
        "structured queries), get_record (fetch one record by Id), list_objects "
        "(discover available objects).\n\n"
        "For Case topic searches, PREFER search_cases — it does the "
        "search-everywhere reconciliation below in one call, reports the TRUE "
        "total match count per source (so you know your coverage across the "
        "org's full Case history), and takes exhaustive=true to paginate and "
        "pull EVERY matching Case (not just the newest page). Pass owner=\"Full "
        "Name\" to scope to one person's cases (\"cases I handled\").\n\n"
        "SEARCH-EVERYWHERE RULE (default for ALL topic/intent searches): the "
        "information you are looking for can live in MANY fields, not just the "
        "record title. For Cases especially, a topic (e.g. 'group migration') "
        "is frequently NOT in the Subject — it hides in the Case Description, "
        "the Case Comments (CaseComment.CommentBody), and the Case History / "
        "email body. So when a user asks you to find records about a topic, do "
        "NOT search the title/Subject alone. Cover all of these sources:\n"
        "  1. Title / Subject  -> soql_query: WHERE Subject LIKE '%term%'\n"
        "  2. Case Comments     -> soql_query on CaseComment: WHERE CommentBody "
        "LIKE '%term%', selecting Parent.CaseNumber/Subject/Status/CreatedDate, "
        "then dedupe by ParentId.\n"
        "  3. Description + everything else -> search_salesforce (SOSL FIND IN "
        "ALL FIELDS), which indexes Description, comments, emails, etc.\n"
        "Then MERGE and DEDUPE results from all sources into one answer.\n\n"
        "IMPORTANT GOTCHAS:\n"
        "- Long-text-area fields like Case.Description CANNOT be filtered in "
        "SOQL (INVALID_FIELD 'cannot be filtered'). To match on Description "
        "text, use search_salesforce (SOSL), not a SOQL LIKE.\n"
        "- SOSL (search_salesforce) is high-recall but matches terms loosely "
        "(words may appear separately, not as a phrase) and caps at ~80 records "
        "per object, relevance-ranked. The CaseComment LIKE approach is "
        "high-precision. Use BOTH and reconcile, flagging loose SOSL-only hits "
        "for verification.\n"
        "- search_salesforce returns record Ids grouped by object type; "
        "re-query with soql_query (WHERE Id IN (...)) to get fields.\n"
        "- list_objects takes a name_filter substring — this org has ~1700 "
        "objects, so filter (e.g. name_filter='case') instead of listing all."
    ),
)

# Cached connection, created lazily on first tool call.
_sf = None

# Cap tool output so a huge result set can't blow up the context window.
# Kept below the client's per-result token budget, allowing for the extra
# characters added when this JSON is escaped inside the transport envelope.
_MAX_CHARS = 45000

# Salesforce object key prefixes (first 3 chars of an Id) -> object name, used
# to label/group SOSL results (which come back as bare Ids). Covers the common
# standard objects; custom objects fall back to a "prefix:XXX" label.
_KEY_PREFIX = {
    "001": "Account", "003": "Contact", "005": "User", "006": "Opportunity",
    "500": "Case", "00Q": "Lead", "701": "Campaign", "800": "Order",
    "00T": "Task", "00U": "Event", "02s": "EmailMessage", "00P": "Attachment",
    "068": "ContentVersion", "0D5": "FeedItem", "01t": "Product2",
    "0Q0": "Quote", "807": "Contract", "00a": "CaseComment",
}

# Validation for tool arguments that flow into API paths / getattr. Tool args
# come from an LLM and are NOT trusted: an object name must look like an SObject
# API name, and a record Id must be a 15- or 18-char Salesforce Id.
_OBJECT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_ID_RE = re.compile(r"^[a-zA-Z0-9]{15,18}$")

# In-memory describe caches (per process). Describe calls are expensive and the
# schema rarely changes within a session; pass refresh=True to bypass.
_CACHE_TTL = 3600  # seconds
_describe_cache = {}   # object_name -> (timestamp, payload dict)
_objects_cache = None  # (timestamp, [ {name, label}, ... ])


def _normalize_domain(raw: str) -> str:
    """Reduce any SF_DOMAIN form to the value simple-salesforce expects.

    Accepts "login", "test", a full URL like "https://login.salesforce.com",
    or a My Domain host like "replicon.my.salesforce.com". Returns "login"
    (production), "test" (sandbox), or a bare My Domain host.
    """
    d = (raw or "").strip()
    if not d:
        return "login"
    # Strip scheme and any path.
    d = d.replace("https://", "").replace("http://", "").split("/")[0]
    if d in ("login.salesforce.com", "login", ""):
        return "login"
    if d in ("test.salesforce.com", "test"):
        return "test"
    # A My Domain host (e.g. acme.my.salesforce.com) is passed through as-is.
    return d


def _connect() -> Salesforce:
    """Authenticate with the SOAP username-password-token login.

    This is the standard simple-salesforce login and works for orgs where the
    OAuth 2.0 connected-app password grant is disabled. The connected-app
    consumer key/secret are optional and only used as a fallback.
    """
    required = ["SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            "Missing environment variable(s): "
            + ", ".join(missing)
            + ". Set them in the `env` block of the MCP server config."
        )

    domain = _normalize_domain(os.environ.get("SF_DOMAIN"))
    sf = Salesforce(
        username=os.environ["SF_USERNAME"],
        password=os.environ["SF_PASSWORD"],
        security_token=os.environ["SF_SECURITY_TOKEN"],
        domain=domain,
    )
    logger.info("Authenticated to %s", sf.sf_instance)
    return sf


def _get_sf(force_reconnect: bool = False) -> Salesforce:
    global _sf
    if _sf is None or force_reconnect:
        _sf = _connect()
    return _sf


def _with_retry(fn):
    """Run fn(sf); on an expired/invalid session, reconnect once and retry."""
    try:
        return fn(_get_sf())
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        session_err = (
            (SalesforceExpiredSession is not None and isinstance(exc, SalesforceExpiredSession))
            or "session" in msg
            or "invalid_session" in msg
            or "expired" in msg
            or "401" in msg
        )
        if session_err:
            logger.info("Session error, reconnecting: %s", exc)
            return fn(_get_sf(force_reconnect=True))
        raise


def _paginate(res, rows, cap):
    """Follow queryMore from an initial response until `done` or `cap` rows.

    Salesforce returns ~2000 rows per page. Returns the raw (uncleaned) rows.
    """
    while not res.get("done") and (cap is None or len(rows) < cap):
        nxt = res.get("nextRecordsUrl")
        if not nxt:
            break
        res = _with_retry(lambda sf: sf.query_more(nxt, identifier_is_url=True))
        rows.extend(res.get("records", []))
    return rows[:cap] if cap is not None else rows


def _query_all(soql, cap=None):
    """Run a SOQL query and paginate to gather up to `cap` rows (all if None)."""
    res = _with_retry(lambda sf: sf.query(soql))
    return _paginate(res, list(res.get("records", [])), cap)


def _count(soql):
    """Run a COUNT()/COUNT_DISTINCT() aggregate query; return the integer."""
    res = _with_retry(lambda sf: sf.query(soql))
    recs = res.get("records") or [{}]
    # The aliased aggregate is the sole non-'attributes' value in the row.
    for k, v in _clean(recs[0]).items():
        if isinstance(v, int):
            return v
    return res.get("totalSize", 0)


def _clean(record):
    """Strip Salesforce 'attributes' metadata noise from records."""
    if isinstance(record, dict):
        return {k: _clean(v) for k, v in record.items() if k != "attributes"}
    if isinstance(record, list):
        return [_clean(r) for r in record]
    return record


def _dump(obj) -> str:
    """Serialize to pretty JSON, capped at _MAX_CHARS.

    If the output is too large and `obj` is a dict containing a list under a
    known key, the list is trimmed (binary-searched to the largest count that
    fits) so the result stays VALID JSON — callers can always json.parse it.
    A `_truncated` note records how many items were dropped.
    """
    text = json.dumps(obj, indent=2, default=str)
    if len(text) <= _MAX_CHARS:
        return text

    if isinstance(obj, dict):
        for key in ("cases", "records", "objects"):
            items = obj.get(key)
            if isinstance(items, list) and items:
                return _dump_trimmed_list(obj, key, items)

    # Fallback: no trimmable list — hard cut (may be partial) with a marker.
    return text[:_MAX_CHARS] + "\n... [output truncated; narrow your query]"


def _dump_trimmed_list(obj, key, items) -> str:
    """Return valid JSON with obj[key] trimmed to the most items that fit."""
    def render(n):
        trimmed = dict(obj)
        trimmed[key] = items[:n]
        trimmed["_truncated"] = (
            f"Output capped at {_MAX_CHARS} chars: showing {n} of {len(items)} "
            f"{key}. Narrow your query (add WHERE/LIMIT or a name_filter)."
        )
        return json.dumps(trimmed, indent=2, default=str)

    lo, hi, best = 0, len(items), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if len(render(mid)) <= _MAX_CHARS:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return render(best)


@mcp.tool()
def soql_query(query: str, max_rows: int = 2000) -> str:
    """Run a SOQL query and return matching records as JSON.

    Use when you know the object and fields you want, e.g.
    "SELECT Id, Name, StageName FROM Opportunity WHERE Amount > 10000 LIMIT 50".

    By default returns the first page (~2000 rows). To pull a larger result set,
    raise `max_rows` (capped at 50000) — the server follows queryMore pagination
    until `max_rows` is reached or the result is exhausted. `complete` is false
    when more rows matched than were returned.
    """
    try:
        max_rows = max(1, min(int(max_rows), 50000))
        first = _with_retry(lambda sf: sf.query(query))
        total = first.get("totalSize")
        rows = _paginate(first, list(first.get("records", [])), max_rows)
        records = _clean(rows)
        return _dump(
            {
                "totalSize": total,
                "returned": len(records),
                "complete": total is None or len(records) >= total,
                "records": records,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error running SOQL query: {exc}"


@mcp.tool()
def search_salesforce(search_term: str) -> str:
    """Full-text search (SOSL) across all Salesforce objects for a term.

    Use when you don't know which object holds the data, or to find a person,
    company, case number, or keyword anywhere in the org. Example terms:
    "Acme" or "00012345". Returns matching records across objects.

    SEARCH-EVERYWHERE: when finding records about a TOPIC (e.g. "group
    migration"), the term often is NOT in the title/Subject. Combine this SOSL
    search (covers Description, comments, emails — all indexed text) with a
    soql_query on CaseComment.CommentBody (high precision) and on Subject, then
    merge and dedupe. Note: Case.Description cannot be SOQL-filtered, so SOSL is
    the only way to match Description text. For Cases specifically, prefer the
    search_cases tool, which does all of this for you.

    Returns matching record Ids GROUPED BY object type (with a per-type count).
    SOSL matches terms loosely (words may appear separately, not as a phrase)
    and caps at ~80 records per object. Re-query the Ids with soql_query
    (WHERE Id IN (...)) to fetch fields.
    """
    try:
        sosl = "FIND {" + _escape_sosl(search_term) + "} IN ALL FIELDS"
        result = _with_retry(lambda sf: sf.search(sosl))
        if isinstance(result, dict):
            records = result.get("searchRecords", [])
        else:
            records = result or []
        records = _clean(records)

        by_object = {}
        for r in records:
            rid = (isinstance(r, dict) and r.get("Id")) or ""
            name = _KEY_PREFIX.get(rid[:3], f"prefix:{rid[:3]}") if rid else "unknown"
            by_object.setdefault(name, []).append(rid)

        return _dump(
            {
                "count": len(records),
                "note": (
                    "SOSL full-text matches, as record Ids grouped by object "
                    "type. Re-query Ids with soql_query for fields. For Case "
                    "topic searches, use search_cases instead."
                ),
                "by_object": {
                    k: {"count": len(v), "ids": v}
                    for k, v in sorted(by_object.items(), key=lambda kv: -len(kv[1]))
                },
            }
        )
    except Exception as exc:  # noqa: BLE001
        return f"Error searching Salesforce: {exc}"


def _escape_soql_str(term: str) -> str:
    """Escape a term for a SOQL string literal used with `=` (only \\ and ')."""
    return term.replace("\\", "\\\\").replace("'", "\\'")


def _escape_soql_like(term: str) -> str:
    """Escape a term for a SOQL LIKE '%...%' literal.

    In addition to \\ and ', the LIKE wildcards % and _ are escaped so a term
    containing them matches literally instead of widening the pattern (e.g. a
    term of "%" would otherwise match every row).
    """
    return _escape_soql_str(term).replace("%", "\\%").replace("_", "\\_")


def _resolve_user(owner: str):
    """Resolve an owner name/username to a User. Returns a dict on success, or
    a JSON error string (no match / ambiguous) suitable for returning to the caller."""
    s = _escape_soql_str(owner)
    like = _escape_soql_like(owner)
    rows = _clean(_query_all(
        "SELECT Id, Name, Username FROM User "
        f"WHERE Name = '{s}' OR Username = '{s}' OR Name LIKE '%{like}%' LIMIT 6"))
    if not rows:
        return _dump({"error": f"No Salesforce user matches owner={owner!r}.",
                      "hint": "Pass the user's full Name or Username."})
    exact = [r for r in rows if r.get("Name") == owner or r.get("Username") == owner]
    chosen = exact[0] if exact else (rows[0] if len(rows) == 1 else None)
    if chosen is None:
        return _dump({"error": f"owner={owner!r} is ambiguous — specify exactly.",
                      "candidates": [{"Name": r.get("Name"), "Username": r.get("Username")} for r in rows]})
    return {"Id": chosen["Id"], "Name": chosen.get("Name"), "Username": chosen.get("Username")}


def _escape_sosl(term: str) -> str:
    """Escape SOSL reserved characters in a FIND term."""
    out = term
    for ch in [
        "\\", "?", "&", "|", "!", "{", "}", "[", "]", "(", ")",
        "^", "~", "*", ":", '"', "'", "+", "-",
    ]:
        out = out.replace(ch, "\\" + ch)
    return out


# Per-source fetch cap in normal (non-exhaustive) mode.
_CASE_PAGE = 200


@mcp.tool()
def search_cases(term: str, limit: int = 20, exhaustive: bool = False, owner: str = "") -> str:
    """Find Cases about a TOPIC across ALL sources, merged and deduped.

    This is the bulletproof "search-everywhere" tool for Cases: a topic (e.g.
    "group migration") is often NOT in the Subject but buried in the Case
    Description, Case Comments, or email/history bodies. It runs all three
    searches and reconciles them, AND reports the TRUE total match count per
    source so you always know your coverage:

      1. Subject     -> SOQL  Case.Subject LIKE '%term%'
      2. Comments    -> SOQL  CaseComment.CommentBody LIKE '%term%' (high precision)
      3. Everything  -> SOSL  FIND {term} IN ALL FIELDS (covers Description, which
                              CANNOT be SOQL-filtered, plus emails/other text)

    Coverage: SOQL spans the org's ENTIRE Case history (no date floor). By
    default each SOQL source fetches the newest ~200 matches (good for specific
    terms). `totals` always reports the real match counts, so you can see when a
    common term has more. Set `exhaustive=True` to paginate and pull EVERY
    matching Case across all history (can be large/slow for common terms).

    `limit` caps how many merged Cases are returned (newest first). Each Case is
    annotated with `matched_in` (which sources hit) and `loose_match` (true when
    ONLY SOSL matched — terms may appear separately, so verify those).

    Pass `owner` (a User's full Name or Username) to scope the search to cases
    owned by that person, e.g. owner="Agastya Jha" for "cases I handled".
    """
    try:
        limit = max(1, min(int(limit), 500))
        like = _escape_soql_like(term)
        cases = {}  # CaseNumber -> record dict

        # Optional owner scope. Resolve once; bail with a helpful message if the
        # name is unknown or ambiguous.
        owner_case = owner_parent = ""
        owner_info = None
        if owner.strip():
            resolved = _resolve_user(owner.strip())
            if isinstance(resolved, str):  # error / disambiguation JSON
                return resolved
            owner_info = resolved
            owner_case = f" AND OwnerId = '{owner_info['Id']}'"
            owner_parent = f" AND Parent.OwnerId = '{owner_info['Id']}'"

        def _add(rec, source):
            num = rec.get("CaseNumber")
            if not num:
                return
            if num not in cases:
                cases[num] = {
                    "CaseNumber": num,
                    "Id": rec.get("Id"),
                    "Subject": rec.get("Subject"),
                    "Status": rec.get("Status"),
                    "Priority": rec.get("Priority"),
                    "CreatedDate": rec.get("CreatedDate"),
                    "matched_in": [],
                }
            entry = cases[num]
            if source not in entry["matched_in"]:
                entry["matched_in"].append(source)
            for f in ("Id", "Subject", "Status", "Priority", "CreatedDate"):
                if not entry.get(f) and rec.get(f):
                    entry[f] = rec.get(f)

        # True totals across ALL history (cheap aggregates), independent of caps.
        subject_total = _count(
            f"SELECT COUNT(Id) c FROM Case WHERE Subject LIKE '%{like}%'" + owner_case
        )
        comment_case_total = _count(
            "SELECT COUNT_DISTINCT(ParentId) c FROM CaseComment "
            f"WHERE CommentBody LIKE '%{like}%'" + owner_parent
        )

        cap = None if exhaustive else _CASE_PAGE
        tail = "" if exhaustive else f" LIMIT {_CASE_PAGE}"

        # 1. Subject match.
        subj_soql = (
            "SELECT Id, CaseNumber, Subject, Status, Priority, CreatedDate "
            f"FROM Case WHERE Subject LIKE '%{like}%'" + owner_case
            + " ORDER BY CreatedDate DESC" + tail
        )
        for r in _clean(_query_all(subj_soql, cap)):
            _add(r, "Subject")

        # 2. Case Comments match (high precision); pull parent Case fields.
        comm_soql = (
            "SELECT ParentId, Parent.CaseNumber, Parent.Subject, Parent.Status, "
            "Parent.Priority, Parent.CreatedDate FROM CaseComment "
            f"WHERE CommentBody LIKE '%{like}%'" + owner_parent
            + " ORDER BY CreatedDate DESC" + tail
        )
        for r in _clean(_query_all(comm_soql, cap)):
            parent = r.get("Parent") or {}
            parent["Id"] = r.get("ParentId")
            _add(parent, "Comments")

        # 3. SOSL full-text (covers Description + everything indexed). Ids only,
        #    so filter to Cases (prefix 500) and re-query for fields. SOSL itself
        #    is index-capped (~2000 total / ~200 per object) — not exhaustive.
        sosl = "FIND {" + _escape_sosl(term) + "} IN ALL FIELDS"
        res = _with_retry(lambda sf: sf.search(sosl))
        sosl_records = res.get("searchRecords", []) if isinstance(res, dict) else (res or [])
        sosl_ids = list(dict.fromkeys(
            r.get("Id") for r in _clean(sosl_records)
            if isinstance(r, dict) and (r.get("Id") or "").startswith("500")
        ))
        if sosl_ids:
            id_list = "','".join(sosl_ids)
            soslq = _query_all(
                "SELECT Id, CaseNumber, Subject, Status, Priority, CreatedDate "
                f"FROM Case WHERE Id IN ('{id_list}')" + owner_case
                + " ORDER BY CreatedDate DESC"
            )
            for r in _clean(soslq):
                _add(r, "SOSL")

        # Reclassify SOSL-only hits that genuinely match Subject/Comments but
        # fell outside the capped page, so loose_match stays accurate.
        needle = term.strip().lower()
        for rec in cases.values():
            if rec["matched_in"] == ["SOSL"] and needle and needle in (rec.get("Subject") or "").lower():
                rec["matched_in"] = ["Subject", "SOSL"]
        sosl_only = [rec["Id"] for rec in cases.values()
                     if rec["matched_in"] == ["SOSL"] and rec.get("Id")]
        if sosl_only:
            in_ids = "','".join(sosl_only)
            verify = _clean(_query_all(
                "SELECT ParentId FROM CaseComment "
                f"WHERE CommentBody LIKE '%{like}%' AND ParentId IN ('{in_ids}')" + owner_parent))
            hit = {r.get("ParentId") for r in verify}
            for rec in cases.values():
                if rec.get("Id") in hit and rec["matched_in"] == ["SOSL"]:
                    rec["matched_in"] = ["Comments", "SOSL"]

        # Loose = matched ONLY by SOSL full-text (terms may appear separately).
        merged = []
        for rec in cases.values():
            rec["loose_match"] = rec["matched_in"] == ["SOSL"]
            merged.append(rec)
        merged.sort(key=lambda x: x.get("CreatedDate") or "", reverse=True)

        # In exhaustive mode show all distinct (capped only by output trimming);
        # otherwise show up to `limit`.
        shown = merged if exhaustive else merged[:limit]
        capped_note = ""
        if not exhaustive and (
            subject_total > _CASE_PAGE or comment_case_total > _CASE_PAGE
        ):
            capped_note = (
                " A source has MORE matches than were fetched (see totals); pass "
                "exhaustive=true to pull every matching Case across all history."
            )

        result = {
            "term": term,
            "exhaustive": exhaustive,
            "totals": {
                "subject_matches": subject_total,
                "comment_distinct_cases": comment_case_total,
                "sosl_case_hits": len(sosl_ids),
                "sosl_note": "SOSL is index-capped (~2000/200-per-object), not exhaustive",
            },
            "distinct_cases_found": len(merged),
            "returned": len(shown),
            "note": (
                "matched_in shows which sources hit (Subject/Comments/SOSL). "
                "loose_match=true means ONLY SOSL full-text matched — verify, as "
                "the terms may appear separately rather than as a phrase." + capped_note
            ),
            "cases": shown,
        }
        if owner_info:
            result["owner"] = {"id": owner_info["Id"], "name": owner_info.get("Name"),
                               "username": owner_info.get("Username")}
        return _dump(result)
    except Exception as exc:  # noqa: BLE001
        return f"Error in search_cases: {exc}"


@mcp.tool()
def describe_object(object_name: str, refresh: bool = False) -> str:
    """Return the schema of a Salesforce object: field API names, labels, types.

    Use this before writing a SOQL query against an unfamiliar object so you
    know the exact field names. Example object_name: "Account", "Case",
    "Opportunity", or a custom object like "TimeOff__c". Results are cached per
    process; pass refresh=True to bypass the cache.
    """
    if not _OBJECT_RE.match(object_name or ""):
        return (f"Invalid object_name {object_name!r}: expected a Salesforce API "
                "name like 'Account' or 'TimeOff__c'.")
    now = time.time()
    cached = _describe_cache.get(object_name)
    if not refresh and cached and now - cached[0] < _CACHE_TTL:
        return _dump(cached[1])
    try:
        meta = _with_retry(lambda sf: getattr(sf, object_name).describe())
        fields = [
            {
                "name": f.get("name"),
                "label": f.get("label"),
                "type": f.get("type"),
                "length": f.get("length"),
                "referenceTo": f.get("referenceTo") or None,
            }
            for f in meta.get("fields", [])
        ]
        payload = {
            "name": meta.get("name"),
            "label": meta.get("label"),
            "queryable": meta.get("queryable"),
            "fieldCount": len(fields),
            "fields": fields,
        }
        _describe_cache[object_name] = (now, payload)
        return _dump(payload)
    except Exception as exc:  # noqa: BLE001
        return f"Error describing object '{object_name}': {exc}"


@mcp.tool()
def get_record(object_name: str, record_id: str) -> str:
    """Fetch all fields of a single Salesforce record by its Id.

    Example: get_record("Account", "0015g00000XyZ12AAB"). Accepts 15- or
    18-character Salesforce Ids.
    """
    if not _OBJECT_RE.match(object_name or ""):
        return (f"Invalid object_name {object_name!r}: expected a Salesforce API "
                "name like 'Account' or 'TimeOff__c'.")
    if not _ID_RE.match(record_id or ""):
        return (f"Invalid record_id {record_id!r}: expected a 15- or 18-character "
                "Salesforce Id.")
    try:
        rec = _with_retry(lambda sf: getattr(sf, object_name).get(record_id))
        return _dump(_clean(rec))
    except Exception as exc:  # noqa: BLE001
        return f"Error fetching {object_name} {record_id}: {exc}"


@mcp.tool()
def list_objects(name_filter: str = "", refresh: bool = False) -> str:
    """List the queryable objects available in this Salesforce org.

    Returns each object's API name and label. Use to discover what data
    exists before searching or querying.

    This org can have ~1700 objects, so pass `name_filter` (a case-insensitive
    substring matched against API name and label) to narrow the list, e.g.
    name_filter="case" or name_filter="invoice". Leave it empty to list all
    (the output is trimmed to fit, with a `_truncated` note if it overflows).
    The org describe is cached per process; pass refresh=True to bypass it.
    """
    global _objects_cache
    try:
        now = time.time()
        if refresh or _objects_cache is None or now - _objects_cache[0] >= _CACHE_TTL:
            meta = _with_retry(lambda sf: sf.describe())
            objs_all = [
                {"name": s.get("name"), "label": s.get("label")}
                for s in meta.get("sobjects", [])
                if s.get("queryable")
            ]
            objs_all.sort(key=lambda x: x["name"] or "")
            _objects_cache = (now, objs_all)
        objs = _objects_cache[1]
        nf = (name_filter or "").strip().lower()
        if nf:
            objs = [
                o for o in objs
                if nf in (o["name"] or "").lower() or nf in (o["label"] or "").lower()
            ]
        return _dump({
            "count": len(objs),
            "filter": name_filter or None,
            "objects": objs,
        })
    except Exception as exc:  # noqa: BLE001
        return f"Error listing objects: {exc}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
