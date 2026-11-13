#!/usr/bin/env python
"""Inventory every item in an ArcGIS organization, and refuse to report a total the server cannot prove is complete.

Esri's REST reference says it plainly about /sharing/rest/search: the total "is
counted accurately up to 10,000; if the total number is greater than this value,
10,000 will be returned", and num has a maximum of 100. Every inventory, backup and cleanup script
in the wild inherits that ceiling and reports success at it. You run a census
before a migration, it says 10,000 items, and it is wrong by an unknown amount.
At the cap a true total and a truncated one are identical, which is why nobody
notices.

The ArcGIS API for Python is the obvious tool and it is good at what it does.
gis.content.search takes a max_items, advanced_search pages for you, and both
are far less code than this. The gap is not paging, it is proof. This tool pages
the same way and then refuses to print a number it cannot reconcile against the
server's own count, and when a slice of the org sits on the cap it bisects that
slice until every piece is provably under it.

It is read-only. No flag changes anything in the organization, and the only
thing it writes is an inventory file, which needs --apply.

    python itemcensus.py --self-test
    python itemcensus.py --url https://portal.example.com --username gis_admin
    python itemcensus.py --url https://portal.example.com --token TOKEN \
        --out inventory.csv --format csv --apply

Exit codes: 0 a complete census, 1 the census did NOT reconcile, or the token's
role is not org_admin, so its total is a floor, 2 the census could not be completed, 64 usage error (a mistyped or
unknown flag included, and a run with no credential and no --anonymous), 73 the census ran but the inventory file could not be
written.
"""

from __future__ import print_function

import argparse
import csv
import datetime
import getpass
import io
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# =============================================================================
# CONFIGURATION. Deliberately not constants at the call site.
# =============================================================================

# The server caps num on /sharing/rest/search whatever you ask for. Asking for
# 1000 does not fail and does not warn. It returns 100, which is how a paging
# loop that advances by its own num walks past 90 percent of an organization.
SEARCH_MAX_NUM = 100

# search counts accurately only to 10,000. At or beyond that the number IS
# 10,000, and the rows past it cannot be paged to at all. A partition that
# reaches this has to be split, never reported.
SEARCH_CEILING = 10000

# Narrowest created-date range the bisect will produce. One day is the point at
# which splitting the date further stops being useful: an organization with more
# than 10,000 items created in a single day was loaded by a script, and a script
# loads under one owner and one item type, which is what the next two bisect
# levels split on.
DAY_MS = 86400000

# Created dates are epoch milliseconds and no ArcGIS item predates the epoch, so
# the root partition starts here rather than at a date somebody has to maintain.
EPOCH_START_MS = 0

# End of the tail partition that covers everything created after the census
# started: 9999-12-31T23:59:59.999Z. The root partition stops at this machine's
# clock reading, so an item created during the run, or on a portal whose clock
# is ahead of this one, would otherwise be missing with no warning.
FAR_FUTURE_MS = 253402300799999

# Ceiling on how many partitions one census may visit. A portal that reports the
# cap for every query no matter how narrow would otherwise bisect until the
# process dies. This turns that into a refusal with a partition query attached.
MAX_PARTITIONS = 4096

# Item types the third bisect level splits on, used only when a single day owned
# by a single user still sits on the cap. The list does not have to be complete:
# every split is a complement pair, so the half that is "none of these types"
# catches whatever is missing here.
BISECT_TYPES = (
    "Feature Service", "Map Service", "Image Service", "Vector Tile Service",
    "Scene Service", "Web Map", "Web Scene", "Web Mapping Application",
    "Dashboard", "StoryMap", "Form", "Notebook", "Geoprocessing Service",
    "Shapefile", "CSV", "File Geodatabase", "Service Definition", "PDF",
    "Microsoft Excel", "Code Attachment", "Dashboard Add In",
)

# Seconds before a portal call is abandoned.
HTTP_TIMEOUT = 60

# What replaces a secret anywhere it could otherwise be printed.
REDACTED = "[redacted]"

# Environment variable the password is read from. It is never a command line
# flag: argv is readable by every process on the box, and it lands in shell
# history, in scheduler logs and in this tool's own error messages.
SECRET_ENV = "ITEMCENSUS_PASSWORD"

# Environment variable an existing token is read from when --token is not
# given. --token still works, but the value on argv is readable by every
# process on the box and is kept in process-creation audit logs and in shell
# history, which is the reason the password has no flag at all.
TOKEN_ENV = "ITEMCENSUS_TOKEN"

# Exit status when the census finished but the inventory file could not be
# written (sysexits EX_CANTCREAT). Not 1: exit 1 means the total is a floor.
EXIT_WRITE_FAILED = 73

# Exit status for a usage error, from the hand-written checks and from argparse.
EXIT_USAGE = 64

# The role portals/self reports for an organization administrator, custom roles
# with administrative privileges included.
ADMIN_ROLE = "org_admin"

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

# Columns of the CSV inventory, in order. Fixed, because a spreadsheet somebody
# built a pivot table on top of must not gain a column between runs.
CSV_FIELDS = ("id", "title", "owner", "type", "created", "modified", "access",
              "size", "numViews")

# A text cell that starts with one of these is read by a spreadsheet as a
# formula (OWASP, CSV Injection). A tuple and not a string, because "" in
# "=+-@" is True and an empty cell must not be quoted.
CSV_FORMULA_LEAD = ("=", "+", "-", "@", chr(9), chr(13))


class CensusIncomplete(RuntimeError):
    """The organization could not be counted, so no total is returned.

    Raised rather than returning a number, because the whole point of the tool
    is that a wrong total and a right one look identical. A caller that wanted a
    number and got an exception knows something; a caller that wanted a number
    and got 10,000 knows nothing.
    """


# ----------------------------------------------------------------- pure core

def clamp_num(num):
    """Clamp a page size to what the server will actually honour.

    Clamped before the request rather than after the response, so the paging
    loop and the server agree about the page size from the first call.
    """
    if not isinstance(num, int) or isinstance(num, bool):
        raise ValueError("page size must be an integer, got %r" % (num,))
    if num < 1:
        raise ValueError("page size must be at least 1, got %r" % (num,))
    return min(num, SEARCH_MAX_NUM)


def is_capped(total):
    """True when a reported total has reached the ceiling and means nothing.

    The comparison is >=, not >. Exactly 10,000 is the cap value itself: an org
    with 10,000 items and an org with 400,000 both report 10,000 here, and there
    is no way to tell them apart from the number. Treating the boundary as a
    real total is the defect this tool exists to stop.
    """
    return int(total) >= SEARCH_CEILING


def is_complete(total, collected):
    """True when a partition's crawl is provably everything the query matched.

    Both halves are needed. Under the ceiling the total is trustworthy, and the
    rows actually returned have to match it, because a total read from page one
    says nothing about whether page forty arrived.
    """
    return not is_capped(total) and int(collected) == int(total)


def next_start(page, start, returned):
    """Where the next page begins, or None when this query is finished.

    The server's own nextStart is authoritative and -1 is its end marker. When
    the response omits it, the next page begins after the rows that ACTUALLY
    came back, never after the page size that was asked for: the server clamps
    num without saying so, and a loop that advanced by its own num skipped
    everything in between.
    """
    if start < 1:
        raise ValueError("start is 1-based, got %r" % (start,))
    if returned < 0:
        raise ValueError("a page cannot return %r rows" % (returned,))
    if returned == 0:
        # No rows means no next page, whatever nextStart claims. A start past
        # the end of the result set lands here, and a server that returns
        # nothing while still pointing forward would otherwise page for ever.
        return None
    nxt = page.get("nextStart")
    if nxt is None:
        nxt = start + returned
    nxt = int(nxt)
    if nxt <= 0:
        return None
    if nxt <= start:
        raise ValueError("nextStart %d does not advance past start %d, which "
                         "would page the same rows for ever" % (nxt, start))
    if nxt > SEARCH_CEILING:
        # search refuses a start beyond the ceiling. Stopping here rather than
        # asking is the difference between a partition that gets bisected and a
        # crawl that dies on the portal's own error.
        return None
    return nxt


def quote_term(term):
    """Quote one query term, and refuse a term that would break the query.

    A term carrying its own double quote closes the string early and silently
    changes which items the query selects, which in this tool means a slice of
    the org that nothing ever counts. An owner named with a space is ordinary
    on Enterprise and needs the quotes.
    """
    term = "%s" % (term,)
    if not term.strip():
        raise ValueError("a query term cannot be empty")
    if '"' in term:
        raise ValueError("query term %r contains a double quote" % (term,))
    if " " in term or ":" in term or "(" in term or ")" in term:
        return '"%s"' % term
    return term


def field_clause(field, terms, negated=False):
    """An 'owner:(a OR b)' clause, or its complement with a leading minus.

    The complement is what makes a bisect on a list safe. Splitting owners into
    "these" and "those" is only exhaustive when the owner list is complete, and
    a user list is never complete for long. Splitting into "these" and "not
    these" is exhaustive by construction.
    """
    terms = tuple(terms)
    if not terms:
        raise ValueError("a %s clause needs at least one term" % field)
    body = " OR ".join(quote_term(t) for t in terms)
    return "%s%s:(%s)" % ("-" if negated else "", field, body)


class Partition(object):
    """One slice of the organization: a created-date range and its clauses.

    owners and types are the terms still available to split THIS partition on.
    They shrink as the bisect descends, which is what makes the recursion
    terminate rather than emitting the same clause for ever.
    """

    def __init__(self, base, lo, hi, clauses=(), owners=(), types=(),
                 owner_half=False):
        lo, hi = int(lo), int(hi)
        if lo > hi:
            raise ValueError("partition range is backwards: %d > %d" % (lo, hi))
        if lo < 0:
            raise ValueError("a created date cannot be negative: %d" % lo)
        self.base = base or ""
        self.lo = lo
        self.hi = hi
        self.clauses = tuple(clauses)
        self.owners = tuple(owners)
        self.types = tuple(types)
        # True for the 'owner:(...)' half of an owner split. census() reads its
        # total again at the end, because an item moved into that owner between
        # the two halves is in neither half.
        self.owner_half = owner_half

    @property
    def width_ms(self):
        """Width of the created-date range. Inclusive at both ends."""
        return self.hi - self.lo + 1

    def divisible(self):
        """True when bisect() has something left to split this on."""
        return (self.hi - self.lo >= DAY_MS) or bool(self.owners) or bool(self.types)

    def child(self, lo=None, hi=None, clause=None, owners=None, types=None):
        """A copy with one more clause, or a narrower date range."""
        return Partition(
            self.base,
            self.lo if lo is None else lo,
            self.hi if hi is None else hi,
            self.clauses + ((clause,) if clause else ()),
            self.owners if owners is None else owners,
            self.types if types is None else types)

    def __repr__(self):
        return "Partition(%r)" % (partition_query(self),)


def partition_query(part):
    """Render a partition as the q parameter search is given.

    The base query is parenthesised. An operator passing 'type:Web Map OR
    owner:jsmith' would otherwise have the date range AND itself onto the last
    term only, and the census would quietly cover a different org than the one
    asked for.
    """
    parts = []
    if part.base:
        parts.append("(%s)" % part.base)
    parts.append("created:[%d TO %d]" % (part.lo, part.hi))
    parts.extend(part.clauses)
    return " AND ".join(parts)


def _narrow(clauses, field, clause):
    """Add a positive clause, dropping the wider positive clause it replaces.

    owner:(ann) implies owner:(ann OR bob), so carrying both only makes the
    query longer. search is a GET, and a query that gained a clause at every
    bisect level reached the url length limit and came back 414 before it
    reached the level that would have divided the partition.
    """
    prefix = "%s:(" % field
    return tuple(c for c in clauses if not c.startswith(prefix)) + (clause,)


def _split_terms(part, field, terms):
    """Split a partition into 'the first half of these terms' and 'not those'."""
    cut = max(1, len(terms) // 2)
    head, tail = tuple(terms[:cut]), tuple(terms[cut:])
    # A single term cannot be halved again, so the matching child carries no
    # candidates forward and falls through to the next bisect level instead of
    # emitting owner:(a) AND owner:(a) until the partition budget runs out.
    inside = head if len(head) > 1 else ()
    hit = Partition(part.base, part.lo, part.hi,
                    _narrow(part.clauses, field, field_clause(field, head)),
                    owners=inside if field == "owner" else part.owners,
                    types=part.types if field == "owner" else inside,
                    owner_half=field == "owner")
    miss = part.child(clause=field_clause(field, head, True),
                      owners=tail if field == "owner" else None,
                      types=None if field == "owner" else tail)
    return hit, miss


def bisect(part):
    """Split one partition into two whose union is exactly the parent.

    Created date first, then owner, then item type. Date is first because it
    always works and needs nothing read from the portal. Owner is second because
    a day with more than 10,000 items is a bulk load and a bulk load has one
    owner. Type is last because it is the coarsest.

    Every level is a complement pair, so the two children cover the parent even
    when the owner or type list is wrong or stale. A census that split on a list
    of usernames would drop every item owned by somebody hired since the list
    was made, and would not notice, because the halves would still add up to a
    number.
    """
    if part.hi - part.lo >= DAY_MS:
        mid = part.lo + (part.hi - part.lo) // 2
        return part.child(hi=mid), part.child(lo=mid + 1)
    if part.owners:
        return _split_terms(part, "owner", part.owners)
    if part.types:
        return _split_terms(part, "type", part.types)
    raise CensusIncomplete(
        "this query still reports the %d result ceiling and there is nothing "
        "left to split it on: %s. It covers %d day(s), one owner and one item "
        "type, so the organization holds more than %d items that are identical "
        "on all three. No total can be proven for it."
        % (SEARCH_CEILING, partition_query(part),
           max(1, part.width_ms // DAY_MS), SEARCH_CEILING))


def reconcile(query, totals, collected):
    """Warnings about one partition's crawl. Empty when it reconciles.

    The totals are every value the server reported, one per page, not just the
    one on the last page. A total that moves mid-crawl means items were created
    or deleted while the census ran, and the honest response is to say so rather
    than to keep the newest number and call it the answer.
    """
    out = []
    if not totals:
        return out
    if len(set(totals)) > 1:
        out.append("the reported total moved from %d to %d while %s was being "
                   "read, so items were created or deleted mid-crawl"
                   % (totals[0], totals[-1], query))
    final = int(totals[-1])
    if not is_capped(final) and int(collected) != final:
        out.append("%s reported %d item(s) and returned %d"
                   % (query, final, collected))
    return out


def search_answer(page):
    """The (total, results) of one parsed search response, or ValueError."""
    if not isinstance(page, dict):
        raise ValueError("fetch returned a %s, which is not a search "
                         "response" % type(page).__name__)
    total = page.get("total")
    results = page.get("results")
    if (not isinstance(total, int) or isinstance(total, bool)
            or not isinstance(results, list)):
        # An empty object, a maintenance notice and a login page all parse
        # as JSON. Read as a total of 0 they used to reconcile, and the
        # census exited 0 having counted nothing. Keys only, as below.
        raise ValueError("the portal's search answer is not a search "
                         "response: it needs an integer total and a "
                         "results list (it has the keys: %s)"
                         % (", ".join(sorted("%s" % key for key in page))
                            or "none"))
    return total, results


def crawl_partition(fetch, query, num=SEARCH_MAX_NUM):
    """Page one query to its end. Returns (items by id, totals seen, pages).

    fetch is a callable taking (query, start, num) and returning one parsed
    search response. Everything this tool needs from a network lives behind it,
    which is why the paging and the bisect can be driven by canned pages and no
    socket at all.
    """
    size = clamp_num(num)
    items = {}
    totals = []
    pages = 0
    start = 1
    while start is not None:
        page = fetch(query, start, size)
        total, results = search_answer(page)
        pages += 1
        totals.append(total)
        for raw in results:
            iid = raw.get("id")
            if not iid:
                # Keys only. A secured-service row carries its url with a
                # service token in the query string, and this message is printed.
                raise ValueError("a search result has no id (it has the "
                                 "fields: %s)" % ", ".join(sorted(
                                     "%s" % key for key in raw)))
            # Union by id. Two partitions that overlap, because an item was
            # edited between them or because the server's created filter is
            # fuzzy at the boundary, contribute that item once.
            items[iid] = raw
        start = next_start(page, start, len(results))
    return items, totals, pages


class Census(object):
    """The finished inventory and the evidence that it is complete."""

    def __init__(self, items, partitions, warnings, bisections, pages):
        self.items = items
        self.partitions = list(partitions)
        self.warnings = list(warnings)
        self.bisections = bisections
        self.pages = pages

    @property
    def total(self):
        """Distinct item ids read. Never a number the server reported."""
        return len(self.items)

    @property
    def reconciled(self):
        return not self.warnings

    def __repr__(self):
        return "Census(total=%d, partitions=%d, warnings=%d)" % (
            self.total, len(self.partitions), len(self.warnings))


def census(fetch, base="", owners=(), types=BISECT_TYPES, now_ms=None,
           num=SEARCH_MAX_NUM, max_partitions=MAX_PARTITIONS, echo=None):
    """Count every item the base query matches, bisecting past the ceiling.

    Depth first, not breadth first. A partition that will never come under the
    ceiling has to be found by descending into it: taking the tree a level at a
    time would build thirty thousand partitions before the first one got narrow
    enough to refuse, and the refusal is the useful answer.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if max_partitions < 1:
        raise ValueError("--max-partitions must be at least 1")
    now_ms = int(now_ms)
    root = Partition(base, EPOCH_START_MS, now_ms,
                     owners=tuple(owners), types=tuple(types))
    # The tail goes in first so it is read last, after the root.
    tail = Partition(base, now_ms + 1, FAR_FUTURE_MS,
                     owners=tuple(owners), types=tuple(types))
    pending = [tail, root]
    late = 0
    items = {}
    done = []
    warnings = []
    bisections = 0
    pages = 0
    visited = 0
    halves = []

    while pending:
        part = pending.pop()
        visited += 1
        if visited > max_partitions:
            raise CensusIncomplete(
                "gave up after %d partitions, still at %s. Either the portal "
                "reports the ceiling for every query, or the organization needs "
                "a narrower --query." % (max_partitions, partition_query(part)))
        query = partition_query(part)
        got, totals, page_count = crawl_partition(fetch, query, num)
        pages += page_count
        total = totals[-1] if totals else 0

        if is_capped(total):
            # The rows just read are thrown away on purpose. They are at most
            # the first 10,000 of an unknown number, and keeping them would put
            # items into the inventory that no complete partition vouches for.
            # The two children cover this partition exactly, so nothing is lost.
            left, right = bisect(part)
            bisections += 1
            if echo:
                echo("  at the %d ceiling, splitting: %s"
                     % (SEARCH_CEILING, query))
            pending.append(right)
            pending.append(left)
            continue

        warnings.extend(reconcile(query, totals, len(got)))
        items.update(got)
        if part.lo > now_ms:
            late += len(got)
        done.append((query, total, len(got)))
        if part.owner_half:
            halves.append((query, total))
        if echo:
            echo("  %d item(s) from %s" % (len(got), query))

    # A date never changes, so a date split cannot lose an item. An owner can:
    # an item moved to the owner of the first half after that half was read, and
    # before the second half was, is in neither. Both halves still reconcile, so
    # only a second look at the first half's total shows it. This reads the
    # total only, one request per owner split, and is not counted in pages.
    # ponytail: a first half that itself sat on the ceiling was split and is
    # not rechecked at its own level, only its inner owner halves are.
    for query, first in halves:
        again = search_answer(fetch(query, 1, 1))[0]
        if again != first:
            warnings.append(
                "%s reported %d item(s) when it was read and %d at the end "
                "of the run, so an item changed owner or was created while "
                "the census ran, and one moved between the two halves of an "
                "owner split can be in neither" % (query, first, again))
    if late:
        warnings.append(
            "%d item(s) were created after this machine's clock read %d, "
            "either while the census ran or on a portal whose clock is ahead "
            "of this one. They are in the inventory, but the organization "
            "changed during the run" % (late, now_ms))
    return Census(items, done, warnings, bisections, pages)


def item_record(raw):
    """Keep the inventory fields of a search result and drop the rest.

    An inventory file sits on a share for months and gets mailed around. It
    holds what an inventory needs and nothing else: no token, no service url
    with a token in it, no description long enough to be worth a public records
    request.
    """
    return {
        "id": raw.get("id") or "",
        "title": raw.get("title") or "",
        "owner": raw.get("owner") or "",
        "type": raw.get("type") or "",
        "created": int(raw.get("created") or 0),
        "modified": int(raw.get("modified") or 0),
        "access": raw.get("access") or "private",
        "size": int(raw.get("size") or 0),
        "numViews": int(raw.get("numViews") or 0),
    }


def build_inventory(url, query, taken, result, authenticated, role=None):
    """Assemble the inventory document. No credential ever enters it.

    authenticated is False for an --anonymous run. That search sees only public
    items, so the file has to say so itself: "reconciled" is true for it, and a
    file is mailed around without the console note that came with the run. role
    is the signed-in account's role from portals/self, None when it was not read.
    """
    return {
        "itemcensus": 1,
        "url": url,
        "authenticated": bool(authenticated),
        "role": role,
        "query": query,
        "taken": taken,
        "total": result.total,
        "reconciled": result.reconciled,
        "bisections": result.bisections,
        "pages": result.pages,
        "warnings": list(result.warnings),
        "partitions": [{"query": q, "reported": t, "returned": n}
                       for q, t, n in result.partitions],
        # Sorted by id, so the items of two censuses of an unchanged org are in
        # the same order. The JSON still differs by "taken" and by the clock
        # reading inside the partition queries. Only the CSV is byte-stable.
        "items": [item_record(raw)
                  for _iid, raw in sorted(result.items.items())],
    }


def describe(result, anonymous=False, role=ADMIN_ROLE):
    """Render a census as the lines the CLI prints.

    anonymous is True when no token was used. The search then sees only public
    items, and "reconciled" would read as a statement about the whole org. role
    is the signed-in account's role, None when the portal did not say. Only
    org_admin is known to see every item, so any other role gets a note.
    """
    out = ["%d item(s), read in %d partition(s) over %d page(s), %d bisection(s)"
           % (result.total, len(result.partitions), result.pages,
              result.bisections)]
    if result.warnings:
        out.append("")
        out.append("RECONCILIATION WARNINGS")
        for warning in result.warnings:
            out.append("  - %s" % warning)
        out.append("")
        out.append("The total above is the number of distinct item ids read. "
                   "The server's own counts")
        out.append("did not agree with it, so treat it as a floor and run the "
                   "census again.")
    else:
        out.append("Every partition reconciled: the reported total matched the "
                   "rows returned,")
        out.append("on every page, and no partition sat on the %d ceiling."
                   % SEARCH_CEILING)
    if anonymous:
        out.append("")
        out.append("NOTE: no token was used, so only public items were visible. "
                   "This total is not")
        out.append("the organization's, and exit 0 does not mean every item "
                   "was seen.")
    elif role != ADMIN_ROLE:
        out.append("")
        out.append("NOTE: the signed-in account's role is %s, not %s, so items "
                   "it cannot see are"
                   % (role if role else "unreadable", ADMIN_ROLE))
        out.append("missing from this total. The run exits 1, because the "
                   "total is a floor.")
    return out


def exit_code(result, anonymous=False, role=ADMIN_ROLE):
    """0 for a census that reconciled, 1 for one whose total is a floor.

    A token whose role is not org_admin, or is unreadable, sees only part of
    the org, so its total is a floor even when every partition reconciled. An
    anonymous run asked for public items by passing --anonymous, so it is not
    held to the role.
    """
    if result.warnings:
        return 1
    return 1 if not anonymous and role != ADMIN_ROLE else 0


def is_http_url(url):
    """True when urllib will actually open this url.

    A url typed without its scheme is the common mistake, and urllib answers it
    by raising an exception that quotes the whole url back, query string and
    token included. Refusing the url up front is what stops a token reaching a
    scheduler log.
    """
    return bool(url) and url.lower().startswith(("http://", "https://"))


def has_userinfo(url):
    """True when the url may carry a password (https://user:pass@host).

    Any "@" counts. A password may itself hold "/", "?" or "#", so no parse of
    the url can say where the user name ends, and the safe reading of an
    ambiguous url is to refuse it before it is printed anywhere.
    """
    return "@" in url


def has_query_or_fragment(url):
    """True when the url holds "?" or "#", where a token or password hides.

    A portal root has neither. The url is printed, sent back in error text and
    written into the inventory as typed, so a token in its query string would
    reach a log and a file mailed around.
    """
    return "?" in url or "#" in url


def redact(text, *secrets):
    """Remove secrets from anything about to be printed or written.

    urllib repeats the request in some error messages and generateToken is a
    POST, so an unredacted traceback is a password in a log file.
    """
    out = "%s" % (text,)
    for secret in secrets:
        if secret:
            # Coerced because this runs inside an exception handler. A secret
            # that arrived as anything but a string used to raise TypeError
            # here, which threw away the redaction along with the error.
            raw = "%s" % (secret,)
            # urllib quotes a request path back with the query percent-encoded,
            # so a token holding + / or = survives a replace of the raw text.
            for form in (raw, urllib.parse.quote_plus(raw),
                         urllib.parse.quote(raw, safe="")):
                out = out.replace(form, REDACTED)
    return out


def printable(text, encoding):
    """Make one line safe for a console that cannot encode it.

    ArcGIS Pro runs on Windows, where stdout is cp1252 unless somebody changed
    it, and one item whose title held a character that code page has no room for
    aborted the whole report with UnicodeEncodeError. A replaced character loses
    a letter; the crash lost the census.
    """
    if not encoding:
        return text
    try:
        text.encode(encoding)
    except UnicodeEncodeError:
        return text.encode(encoding, "replace").decode(encoding, "replace")
    except LookupError:
        return text
    return text


def utcnow():
    """UTC now, without datetime.utcnow().

    utcnow() is deprecated from 3.12 and datetime.UTC does not exist before
    3.11, so timezone.utc is the spelling that works on ArcGIS Pro's Python and
    on a current python3 alike.
    """
    return datetime.datetime.now(datetime.timezone.utc)


# ---------------------------------------------------------------- portal i/o

def _opener(insecure):
    """Build a urllib opener, optionally without certificate verification.

    Enterprise portals behind an internal CA are the reason --insecure exists.
    It is off by default and refused together with a password, because posting
    credentials down an unverified connection is the failure it would cause.
    """
    if not insecure:
        return urllib.request.build_opener()
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))


def _call(url, path, params, insecure=False, post=False, secret=None):
    """One REST call returning parsed JSON, with the portal's own errors raised.

    The portal answers HTTP 200 with an error object in the body, so the status
    code proves nothing and the body has to be read every time.
    """
    params = dict(params)
    params["f"] = "json"
    endpoint = "%s/sharing/rest/%s" % (url.rstrip("/"), path.lstrip("/"))
    data = urllib.parse.urlencode(params).encode("utf-8")
    opener = _opener(insecure)
    try:
        if post:
            response = opener.open(endpoint, data, timeout=HTTP_TIMEOUT)
        else:
            response = opener.open("%s?%s" % (endpoint, data.decode("utf-8")),
                                   timeout=HTTP_TIMEOUT)
        body = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        # Every credential this request carried, not only the caller's secret.
        # urllib quotes the full url back for a scheme-less --url, and that url
        # carries the token, so one typo used to print a live token to stderr.
        raise RuntimeError(redact("%s: %s" % (endpoint, exc), secret,
                                  params.get("token"), params.get("password")))
    if not isinstance(body, dict):
        # Guarded here rather than in each caller. A captive portal login page,
        # a proxy error page or a load balancer's maintenance notice can all
        # parse as valid JSON without being a portal response, and every caller
        # below goes straight to body.get(), which is an AttributeError and a
        # traceback instead of "the portal did not answer".
        raise RuntimeError("%s: the portal answered with a JSON %s, not an "
                           "object, so this is not a portal response"
                           % (endpoint, type(body).__name__))
    if "error" in body:
        err = body["error"]
        if not isinstance(err, dict):
            err = {"message": err}
        raise RuntimeError(redact("%s: %s %s" % (endpoint, err.get("code"),
                                                 err.get("message")), secret,
                                  params.get("token"), params.get("password")))
    return body


def console_missing(is_windows=None, opener=None):
    """True on Windows when the process has no console to prompt on.

    Without a console msvcrt.getwch() returns U+FFFF instead of raising, and
    getpass.win_getpass loops on it for ever, so an unattended run hung instead
    of reaching the EOFError branch. CONIN$ names the console input buffer
    (Microsoft, "Console Handles"). Opening it failed in a detached child
    process, which is how a missing console shows.
    """
    if is_windows is None:
        is_windows = os.name == "nt"
    if not is_windows:
        return False
    try:
        os.close((opener or os.open)("CONIN$", os.O_RDONLY))
    except OSError:
        return True
    return False


def read_secret(username):
    """Get the password from the environment, or prompt for it.

    Never from argv. getpass keeps it off the screen, and it is passed on to
    redact() so that nothing downstream can print it back out.
    """
    secret = os.environ.get(SECRET_ENV)
    if secret:
        return secret
    if console_missing():
        raise EOFError("no console to prompt on")
    return getpass.getpass("password for %s (not echoed): " % username)


def generate_token(url, username, secret, insecure=False):
    """Exchange a username and password for a short-lived token."""
    body = _call(url, "generateToken", {
        "username": username,
        "password": secret,
        "client": "referer",
        "referer": url,
        "expiration": 60,
    }, insecure=insecure, post=True, secret=secret)
    token = body.get("token")
    if not token:
        raise RuntimeError("generateToken returned no token")
    return token


def portal_self(url, token, insecure=False):
    """The portals/self reply: the org id, and with a token the signed-in user."""
    params = {"token": token} if token else {}
    return _call(url, "portals/self", params, insecure=insecure)


def account_role(me):
    """The signed-in user's role from a portals/self reply, or None.

    None means the reply named no role the tool can trust: no user object (a
    token-less call), no role, or a role that is not a plain word. Esri lists
    org_admin, org_publisher and org_user, and a custom role reports the one it
    maps to.
    """
    user = me.get("user")
    role = user.get("role") if isinstance(user, dict) else None
    if (isinstance(role, str) and role.isascii()
            and role.replace("_", "").isalnum()):
        return role
    return None


def org_owners(url, token, insecure=False):
    """Every username in the org, for the owner bisect. Empty with no token.

    Only ever used to make a bisect finer, never to decide what the census
    covers, so an incomplete list costs partitions and not items. That is the
    whole reason each owner split is paired with its own complement. A portal
    that refuses the list raises RuntimeError here, and main() carries on
    without it.
    """
    if not token:
        return ()
    names = []
    start = 1
    while start is not None:
        body = _call(url, "portals/self/users",
                     {"token": token, "start": start, "num": SEARCH_MAX_NUM},
                     insecure=insecure, secret=token)
        rows = body.get("users") or []
        for row in rows:
            name = row.get("username")
            if name:
                names.append(name)
        start = next_start(body, start, len(rows))
    return tuple(names)


def portal_fetch(url, token, insecure=False):
    """Build the fetch callable the pure crawl drives.

    Results are sorted by created ascending because the bisect is a created-date
    bisect. A stable sort on the field being partitioned is what keeps an item
    from sliding between two pages of the same crawl while the census runs.
    """
    def fetch(query, start, num):
        params = {"q": query, "start": start, "num": num,
                  "sortField": "created", "sortOrder": "asc"}
        if token:
            params["token"] = token
        return _call(url, "search", params, insecure=insecure, secret=token)
    return fetch


def write_json(document, path):
    """Write the inventory as JSON and return the path."""
    _make_parent(path)
    with io.open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=1, sort_keys=True)
    return path


def cell(value):
    """A CSV cell that a spreadsheet will not run as a formula.

    A title, owner or id comes from the portal, and anyone who can name an item
    can name it =HYPERLINK(...). An inventory is the kind of file somebody opens
    in a spreadsheet, though nothing here opened one. Text that starts with = + - @ tab or CR gets
    one apostrophe in front. Numbers pass through untouched: Esri returns size
    as -1 in a search result, and that is a number, not a formula. Whoever reads this file
    back strips exactly one leading apostrophe from a cell whose next character
    is one of CSV_FORMULA_LEAD. This tool never reads its own CSV.
    """
    if isinstance(value, str) and value[:1] in CSV_FORMULA_LEAD:
        return "'" + value
    return value


def write_csv(document, path):
    """Write the inventory as CSV and return the path.

    newline="" is not decoration. Without it the csv module's own carriage
    return meets the one the text layer adds on Windows, and every other line of
    the file is blank, which Excel reads as an empty row between every item.
    """
    _make_parent(path)
    with io.open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_FIELDS)
        for record in document["items"]:
            writer.writerow([cell(record[field]) for field in CSV_FIELDS])
    return path


def _make_parent(path):
    """Create the directory the output file goes in, when it is missing."""
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)


# ------------------------------------------------------------------ self-test

def self_test(_nested=False):
    """Assertions over the census core. No portal, no internet, no credentials."""
    import re
    import shutil
    import tempfile

    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def refuses_census(fn, label):
        """A census that must refuse. Returns the refusal message."""
        try:
            fn()
        except CensusIncomplete as exc:
            check(True, label)
            return "%s" % exc
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (returned a number instead)" % label)
        return ""

    def fails(fn, label):
        """A portal call that must raise. Returns the redacted message."""
        try:
            fn()
        except CensusIncomplete as exc:
            check(False, "%s (refused the census instead: %s)" % (label, exc))
        except RuntimeError as exc:
            check(True, label)
            return "%s" % exc
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)
        return ""

    def refuses(argv, label):
        """argparse writes its usage text to stderr, which is swallowed here so
        that a passing self-test prints only PASS lines."""
        noise, sys.stderr = sys.stderr, io.StringIO()
        try:
            _parse(argv)
        except SystemExit as exc:
            check(exc.code == EXIT_USAGE,
                  label if exc.code == EXIT_USAGE
                  else "%s (exit %s, not %d)" % (label, exc.code, EXIT_USAGE))
        else:
            check(False, "%s (argparse accepted it)" % label)
        finally:
            sys.stderr = noise

    print("itemcensus self-test: no portal, no internet, no credentials")
    print("-" * 68)

    DAY = DAY_MS
    BASE = 1577836800000                  # 2020-01-01T00:00:00Z, epoch ms
    NOW = BASE + 800 * DAY
    PORTAL = "https://portal.example.com"

    # ---- the page size the server will honour
    check(clamp_num(100) == 100, "a page size of 100 is passed through")
    check(clamp_num(1000) == 100,
          "a page size of 1000 is clamped to 100 before it is sent, because "
          "the server clamps it silently  <-- pinned defect")
    check(clamp_num(101) == 100, "one over the cap clamps to the cap")
    check(clamp_num(1) == 1, "a page size of 1 is honoured")
    check(clamp_num(25) == 25, "a page size under the cap is left alone")
    raises(lambda: clamp_num(0), "a page size of zero raises")
    raises(lambda: clamp_num(-5), "a negative page size raises")
    raises(lambda: clamp_num("100"), "a page size that is a string raises")
    raises(lambda: clamp_num(True),
           "a page size of True raises, rather than paging one row at a time")

    # ---- the ceiling, which is the whole point of the tool
    check(is_capped(10000) is True,
          "exactly 10,000 is the CAP and never a total  <-- pinned defect")
    check(is_capped(9999) is False, "9,999 is under the cap and is a real total")
    check(is_capped(10001) is True, "anything above the cap is capped")
    check(is_capped(0) is False, "an empty result is not capped")
    check(is_complete(9999, 9999) is True,
          "a total under the cap that matches the rows read is complete")
    check(is_complete(10000, 10000) is False,
          "10,000 rows read against a reported 10,000 is NOT complete  "
          "<-- pinned defect")
    check(is_complete(500, 499) is False,
          "one row short of the reported total is not complete")
    check(is_complete(500, 501) is False,
          "one row over the reported total is not complete either")
    check(is_complete(0, 0) is True, "an empty org is completely counted")
    check(is_complete(50000, 50000) is False,
          "a total above the cap is not complete however many rows came back")

    # ---- where the next page begins
    check(next_start({"nextStart": 101}, 1, 100) == 101,
          "the server's nextStart is followed")
    check(next_start({"nextStart": -1}, 101, 100) is None,
          "a nextStart of -1 ends the crawl  <-- pinned defect")
    check(next_start({"nextStart": 0}, 101, 100) is None,
          "a nextStart of 0 ends the crawl too")
    check(next_start({}, 1, 100) == 101,
          "a response with no nextStart advances by the rows it returned")
    check(next_start({}, 1, 10) == 11,
          "a short page advances by the ten rows it got, not by the hundred it "
          "asked for  <-- pinned defect")
    check(next_start({}, 1, 0) is None,
          "a page with no rows ends the crawl")
    check(next_start({"nextStart": 501}, 401, 0) is None,
          "a start past the end ends the crawl even when the server still "
          "points forward  <-- pinned defect")
    check(next_start({"nextStart": 9901}, 9801, 100) == 9901,
          "the last page under the ceiling is still asked for")
    check(next_start({"nextStart": 10001}, 9901, 100) is None,
          "a next page starting past the 10,000 ceiling is not asked for, "
          "because search refuses that start  <-- pinned defect")
    check(next_start({"nextStart": SEARCH_CEILING}, 9801, 100) == SEARCH_CEILING,
          "a next page starting exactly on the ceiling is still asked for")
    raises(lambda: next_start({"nextStart": 50}, 100, 10),
           "a nextStart that goes backwards raises instead of looping for ever")
    raises(lambda: next_start({"nextStart": 100}, 100, 10),
           "a nextStart that does not move raises")
    raises(lambda: next_start({}, 0, 10), "a start of 0 raises, start is 1-based")
    raises(lambda: next_start({}, 1, -1), "a negative row count raises")

    # ---- query terms
    check(quote_term("jsmith") == "jsmith", "a plain username needs no quotes")
    check(quote_term("Feature Service") == '"Feature Service"',
          "a term with a space is quoted")
    check(quote_term("corp\\jsmith") == "corp\\jsmith",
          "a windows-style domain login is left alone")
    check(quote_term("a:b") == '"a:b"',
          "a term holding a colon is quoted, or the server reads it as a field")
    raises(lambda: quote_term(''), "an empty term raises")
    raises(lambda: quote_term('   '), "a whitespace-only term raises")
    raises(lambda: quote_term('bad"name'),
           "a term holding a double quote raises, because it would close the "
           "query early and silently select different items  <-- pinned defect")
    check(field_clause("owner", ["a", "b"]) == "owner:(a OR b)",
          "an owner clause ORs its terms")
    check(field_clause("owner", ["a"], True) == "-owner:(a)",
          "a negated clause carries the leading minus")
    check(field_clause("type", ["Web Map", "PDF"]) == 'type:("Web Map" OR PDF)',
          "a type clause quotes only the terms that need it")
    raises(lambda: field_clause("owner", []), "an empty clause raises")

    # ---- partitions and the query they render to
    root = Partition("orgid:ORG123", EPOCH_START_MS, NOW)
    check(partition_query(root)
          == "(orgid:ORG123) AND created:[0 TO %d]" % NOW,
          "the root partition is the base query bounded by a date range")
    check(partition_query(Partition("", 0, 10)) == "created:[0 TO 10]",
          "an empty base query renders to the date range alone")
    check(partition_query(Partition("a OR b", 0, 10))
          == "(a OR b) AND created:[0 TO 10]",
          "the base query is parenthesised, or the date range would AND onto "
          "its last term only  <-- pinned defect")
    child = root.child(clause="owner:(jsmith)")
    check(partition_query(child).endswith(" AND owner:(jsmith)"),
          "a clause is appended to the query")
    check(partition_query(child.child(clause="-type:(PDF)")).endswith(
        " AND owner:(jsmith) AND -type:(PDF)"),
        "clauses accumulate in the order they were added")
    check(root.width_ms == NOW + 1, "the date range is inclusive at both ends")
    check(Partition("", 0, 0).width_ms == 1,
          "a partition covering one millisecond is one millisecond wide")
    check(root.divisible() is True, "a range of 800 days can still be split")
    check(Partition("", BASE, BASE + DAY - 1).divisible() is False,
          "one day with no owners and no types cannot be split")
    check(Partition("", BASE, BASE + DAY - 1, owners=("a",)).divisible() is True,
          "one day with an owner left can still be split")
    check(Partition("", BASE, BASE + DAY - 1, types=("PDF",)).divisible() is True,
          "one day with a type left can still be split")
    raises(lambda: Partition("", 10, 5), "a backwards date range raises")
    raises(lambda: Partition("", -1, 5), "a negative created date raises")
    check(repr(Partition("", 0, 5)) == "Partition('created:[0 TO 5]')",
          "a partition reprs as the query it renders to")

    # ---- the bisect ladder
    left, right = bisect(Partition("q", 0, 2 * DAY))
    check(left.lo == 0 and left.hi == DAY,
          "a two day range splits at its midpoint")
    check(right.lo == DAY + 1 and right.hi == 2 * DAY,
          "the second half starts one millisecond after the first ends, so no "
          "item falls in both  <-- pinned defect")
    check(left.width_ms + right.width_ms == 2 * DAY + 1,
          "the two halves cover the parent exactly, with nothing left over")
    wide = Partition("q", 0, 1000 * DAY)
    steps = 0
    part = wide
    while part.hi - part.lo >= DAY:
        part = bisect(part)[0]
        steps += 1
    check(steps == 10 and part.hi - part.lo < DAY,
          "a 1000 day range halves down to under a day in ten steps")
    # The ladder is date, then owner, then type, and the order is load bearing.
    # Splitting on owner while the range is still years wide burns a level that
    # costs a portal round trip on a partition a free date split would have
    # emptied, and it hands both children the same date range for ever.
    ladder = Partition("q", 0, 10 * DAY, owners=("ann",), types=("Web Map",))
    first = bisect(ladder)[0]
    check(first.hi - first.lo < ladder.hi - ladder.lo,
          "a wide range carrying owners and types still splits on DATE first  "
          "<-- pinned defect")
    check(first.owners == ("ann",) and first.types == ("Web Map",),
          "and a date split spends neither list, so both survive to the levels "
          "below it")
    check("owner:" not in partition_query(first)
          and "type:" not in partition_query(first),
          "a date split adds no owner or type clause at all")
    narrow = Partition("q", BASE, BASE + DAY - 1, owners=("ann",),
                       types=("Web Map",))
    check("owner:(ann)" in partition_query(bisect(narrow)[0]),
          "only once the range is down to one day does the OWNER level run")
    typed = Partition("q", BASE, BASE + DAY - 1, types=("Web Map",))
    check("type:" in partition_query(bisect(typed)[0]),
          "and TYPE is last, reached only when no owner is left to split on")

    day = Partition("q", BASE, BASE + DAY - 1, owners=("ann", "bob", "cal"))
    left, right = bisect(day)
    check(partition_query(left).endswith("owner:(ann)"),
          "a day at the cap splits on the first owner")
    check(partition_query(right).endswith("-owner:(ann)"),
          "the other half is NOT the remaining owners but everyone else, so an "
          "owner missing from the list is still counted  <-- pinned defect")
    check(left.owners == () and right.owners == ("bob", "cal"),
          "the matched half has no owner left to split on, the complement "
          "keeps the rest")
    check(left.lo == day.lo and left.hi == day.hi,
          "an owner split does not touch the date range")
    one, rest = bisect(right)
    check(partition_query(one).endswith("-owner:(ann) AND owner:(bob)"),
          "the complement splits again on the next owner")
    check(partition_query(rest).endswith("-owner:(ann) AND -owner:(bob)"),
          "and its own complement narrows further")
    check(rest.owners == ("cal",), "one owner is still available to split on")
    nobody = bisect(rest)[1]
    check(nobody.owners == (),
          "after the last named owner the complement has no owner left, so the "
          "split terminates instead of emitting owner:(cal) for ever  "
          "<-- pinned defect")
    check(partition_query(nobody).endswith("-owner:(cal)"),
          "and that last complement is the items owned by nobody on the list")
    typed = Partition("q", BASE, BASE + DAY - 1, types=("PDF", "CSV"))
    left, right = bisect(typed)
    check(partition_query(left).endswith("type:(PDF)"),
          "with owners exhausted the bisect falls through to item type")
    check(partition_query(right).endswith("-type:(PDF)"),
          "the type split is a complement pair too")
    both = Partition("q", BASE, BASE + DAY - 1, owners=("ann",), types=("PDF",))
    check("owner" in partition_query(bisect(both)[0]),
          "owner is tried before type, because a bulk load has one owner")
    check(bisect(both)[0].types == ("PDF",),
          "an owner split carries the type candidates down to its children")
    check(bisect(both)[1].types == ("PDF",),
          "and so does its complement")

    # Clause growth. search is a GET, and every bisect level used to add a
    # clause that the next level made redundant, so the url grew until the
    # portal answered 414 instead of dividing the partition.
    deep = Partition("q", BASE, BASE + DAY - 1, types=BISECT_TYPES)
    for _step in range(4):
        deep = bisect(deep)[0]
    check(partition_query(deep).count("type:(") == 1,
          "four type splits leave ONE type clause, not four nested ones that "
          "grow the url until search answers 414  <-- pinned defect")
    check(deep.types == (),
          "and after four halvings of 21 types nothing is left to split on")
    check(partition_query(deep) == partition_query(Partition(
        "q", BASE, BASE + DAY - 1, clauses=('type:("Feature Service")',))),
        "so four levels down the query is the same length as one level down, "
        "which is what keeps it sendable")
    kept = Partition("q", BASE, BASE + DAY - 1, owners=("ann", "bob", "cal"))
    kept = bisect(bisect(kept)[1])[0]
    check(partition_query(kept).count("-owner:(") == 1
          and partition_query(kept).count(" owner:(") == 1,
          "an exclusion is kept when the positive clause beside it is replaced, "
          "because 'not ann' is not implied by 'is bob'  <-- pinned defect")
    refusal = refuses_census(
        lambda: bisect(Partition("q", BASE, BASE + DAY - 1)),
        "a one day partition with nothing left to split on REFUSES  "
        "<-- pinned defect")
    check("nothing left to split" in refusal,
          "the refusal says why it cannot go further")
    check("created:[%d TO %d]" % (BASE, BASE + DAY - 1) in refusal,
          "the refusal names the exact query that would not divide")
    check("1 day(s)" in refusal, "the refusal says how wide that partition was")

    # ---- reconciliation
    check(reconcile("q", [], 0) == [],
          "a crawl with no pages has nothing to reconcile")
    check(reconcile("q", [40, 40, 40], 40) == [],
          "a steady total matching the rows read reconciles silently")
    moved = reconcile("q", [40, 41, 41], 41)
    check(len(moved) == 1 and "moved from 40 to 41" in moved[0],
          "a total that moves mid-crawl is a warning  <-- pinned defect")
    check("mid-crawl" in moved[0], "the warning says the org changed underneath")
    short = reconcile("q", [40, 40], 38)
    check(len(short) == 1 and "reported 40 item(s) and returned 38" in short[0],
          "a crawl that returned fewer rows than the total warns")
    check(reconcile("q", [SEARCH_CEILING], 10000) == [],
          "a capped total raises no row-count warning, because it is bisected "
          "rather than trusted")
    check(len(reconcile("q", [40, 41], 38)) == 2,
          "a total that moved AND came back short reports both")

    # ---- a search endpoint answering from a list of rows
    class Catalog(object):
        """A stand-in for /sharing/rest/search, with the server's real limits.

        It caps num, it counts only to the ceiling, and it filters on the same
        clauses the bisect emits, so a partition that actually narrows the org
        is the only thing that gets it under the cap. A clause it does not
        understand is an error rather than a match-everything, because a fake
        that quietly ignores a filter proves nothing.
        """

        def __init__(self, rows, cap=SEARCH_MAX_NUM, reported=None,
                     nextstart=True, moving=None):
            self.rows = list(rows)
            self.cap = cap
            self.reported = reported
            self.nextstart = nextstart
            self.moving = list(moving) if moving else None
            self.calls = []
            self._cache = {}

        def select(self, query):
            if query in self._cache:
                return self._cache[query]
            rows = self.rows
            for clause in query.split(" AND "):
                clause = clause.strip()
                if clause.startswith("(") and clause.endswith(")"):
                    continue                  # the base query matches all rows
                found = re.match(r"^created:\[(\d+) TO (\d+)\]$", clause)
                if found:
                    lo, hi = int(found.group(1)), int(found.group(2))
                    rows = [r for r in rows if lo <= r["created"] <= hi]
                    continue
                found = re.match(r"^(-?)(owner|type):\((.*)\)$", clause)
                if found:
                    neg, field, body = found.groups()
                    terms = [t.strip().strip('"') for t in body.split(" OR ")]
                    rows = [r for r in rows
                            if (r.get(field) in terms) != bool(neg)]
                    continue
                raise ValueError("the catalog cannot answer %r" % clause)
            self._cache[query] = rows
            return rows

        def __call__(self, query, start, num):
            self.calls.append((query, start, num))
            rows = self.select(query)
            if self.moving:
                total = self.moving[min(len(self.calls) - 1,
                                        len(self.moving) - 1)]
            elif self.reported is not None:
                total = self.reported
            else:
                total = min(len(rows), SEARCH_CEILING)
            size = min(num, self.cap)         # the silent server-side cap
            page = rows[start - 1:start - 1 + size]
            body = {"total": total, "start": start, "num": len(page),
                    "results": page}
            if self.nextstart:
                reach = min(len(rows), SEARCH_CEILING)
                nxt = start + len(page)
                body["nextStart"] = -1 if (not page or nxt > reach) else nxt
            return body

    def rows(count, first=0, owner="jsmith", kind="Feature Service",
             spread=DAY, access="org"):
        return [{"id": "item%06d" % (first + n),
                 "title": "Layer %d" % (first + n),
                 "owner": owner if isinstance(owner, str) else owner[n % len(owner)],
                 "type": kind if isinstance(kind, str) else kind[n % len(kind)],
                 "access": access,
                 "created": BASE + (first + n) * spread,
                 "modified": BASE + (first + n) * spread + 5,
                 "size": 1024 + n,
                 "numViews": n}
                for n in range(count)]

    # The stand-in is only evidence if it refuses what it cannot answer. A fake
    # that treats an unrecognised clause as "match everything" would report a
    # passing census for a tool that emitted nonsense.
    raises(lambda: Catalog([]).select("owner:jsmith"),
           "the stand-in server refuses a query clause it does not understand, "
           "rather than matching everything  <-- pinned defect")
    raises(lambda: Catalog([]).select("created:[0 TO now]"),
           "and refuses a date range it cannot parse")

    # ---- paging one partition
    small = Catalog(rows(250))
    got, totals, pages = crawl_partition(small, "created:[0 TO %d]" % NOW)
    check(len(got) == 250,
          "paging reads all 250 items, not the 100 the server caps a page at  "
          "<-- pinned defect")
    check(pages == 3, "250 items come back in three pages")
    check(totals == [250, 250, 250],
          "the reported total is read on every page, not only the first  "
          "<-- pinned defect")
    check(got["item000249"]["title"] == "Layer 249",
          "the last item of the last page is kept, not lost")
    check(all(call[2] == 100 for call in small.calls),
          "every page asked for exactly 100, the clamped size")
    over = Catalog(rows(120))
    crawl_partition(over, "created:[0 TO %d]" % NOW, num=5000)
    check(all(call[2] == 100 for call in over.calls),
          "a caller asking for 5000 rows a page still sends 100  "
          "<-- pinned defect")
    dribble = Catalog(rows(250), cap=10)
    got, _t, pages = crawl_partition(dribble, "created:[0 TO %d]" % NOW)
    check(len(got) == 250 and pages == 25,
          "a server that quietly returns ten rows for a page of a hundred is "
          "still paged to the end  <-- pinned defect")
    check(dribble.calls[1][1] == 11,
          "the second page starts at row 11, after the ten rows that arrived")
    nostart = Catalog(rows(250), nextstart=False)
    got, _t, pages = crawl_partition(nostart, "created:[0 TO %d]" % NOW)
    check(len(got) == 250,
          "a portal that sends no nextStart at all is paged by row count")
    check(pages == 4,
          "and it costs one extra page to confirm the end, because the only "
          "thing that could have stopped it three pages in is the reported "
          "total, which is the number this tool refuses to trust  "
          "<-- pinned defect")
    empty = Catalog([])
    got, totals, pages = crawl_partition(empty, "created:[0 TO %d]" % NOW)
    check(got == {} and totals == [0] and pages == 1,
          "an empty result set is one page and no items")
    single = Catalog(rows(1))
    got, totals, pages = crawl_partition(single, "created:[0 TO %d]" % NOW)
    check(len(got) == 1 and pages == 1 and totals == [1],
          "an org of exactly one item is one page")
    shortpage = Catalog(rows(40))
    got, _t, pages = crawl_partition(shortpage, "created:[0 TO %d]" % NOW)
    check(len(got) == 40 and pages == 1,
          "a page whose results are fewer than num ends the crawl there")
    raises(lambda: crawl_partition(lambda q, s, n: ["not", "a", "page"], "q"),
           "a fetch returning something that is not a search response raises")
    raises(lambda: crawl_partition(
        lambda q, s, n: {"total": 1, "results": [{"title": "no id"}]}, "q"),
        "a search result with no id raises rather than being keyed under None")

    for what, junk in (
            ("an empty object", {}),
            ("an object with a results list and no total", {"results": []}),
            ("a maintenance notice", {"message": "maintenance"}),
            ("a total that is text", {"total": "5", "results": []}),
            ("a total that is True", {"total": True, "results": []}),
            ("results that are not a list", {"total": 0, "results": None})):
        raises(lambda junk=junk: crawl_partition(lambda q, s, n: junk, "q"),
               "%s is not a search response and is refused, never read as "
               "zero items  <-- pinned defect" % what)
    try:
        crawl_partition(lambda q, s, n: {}, "q")
    except ValueError as exc:
        check("not a search response" in "%s" % exc and "none" in "%s" % exc,
              "and the refusal says so, and that the object had no keys")
    try:
        crawl_partition(lambda q, s, n: {"message": "m", "token": "SECRET"},
                        "q")
    except ValueError as exc:
        check("message, token" in "%s" % exc and "SECRET" not in "%s" % exc,
              "and lists the keys it did have and never a value")

    overlapping = [
        {"total": 3, "nextStart": -1,
         "results": [dict(rows(1)[0], id="dupe"), dict(rows(1)[0], id="one")]},
    ]
    got, _t, _p = crawl_partition(lambda q, s, n: overlapping[0], "q")
    check(len(got) == 2, "two ids in one page are two items")

    # ---- a whole census
    plain = Catalog(rows(250))
    result = census(plain, base="orgid:ORG123", now_ms=NOW, types=())
    check(result.total == 250, "a 250 item org counts to 250")
    check(len(result.partitions) == 2 and result.bisections == 0,
          "an org under the ceiling needs one partition and no bisect, plus "
          "the tail partition that proves nothing was created after the "
          "clock reading")
    check(result.reconciled is True, "and it reconciles")
    check(result.warnings == [], "with no warnings")
    check(result.pages == 4, "three pages for the org and one for the tail")
    check(result.partitions[0][1] == 250 and result.partitions[0][2] == 250,
          "the partition records what the server reported and what it returned")
    check(repr(result).startswith("Census(total=250"),
          "a census reprs with its total")

    none_at_all = census(Catalog([]), base="orgid:ORG123", now_ms=NOW, types=())
    check(none_at_all.total == 0 and none_at_all.reconciled is True,
          "an empty org counts to zero and reconciles")
    check(len(none_at_all.partitions) == 2,
          "an empty org still needs its one partition and its tail")
    just_one = census(Catalog(rows(1)), base="orgid:ORG123", now_ms=NOW, types=())
    check(just_one.total == 1 and just_one.reconciled is True,
          "an org of exactly one item counts to one")

    # An item created after the clock reading the root partition stops at: made
    # while the census runs, or on a portal whose clock is ahead of this one.
    # Before the tail partition it was missing from the inventory, and the run
    # said every partition reconciled and exited 0.
    ahead = rows(5) + [dict(rows(1)[0], id="late1", created=NOW + 1000)]
    behind = Catalog(ahead)
    skewed = census(behind, base="orgid:ORG123", now_ms=NOW, types=())
    check(skewed.total == 6 and "late1" in skewed.items,
          "an item created after the clock reading is still counted  "
          "<-- pinned defect")
    check(skewed.reconciled is False and exit_code(skewed) == 1,
          "and the census says the org changed during the run, so it exits 1 "
          "instead of reporting a clean total  <-- pinned defect")
    check(any("1 item(s) were created after" in w for w in skewed.warnings),
          "and the warning counts the late items")
    check(any("created:[%d TO %d]" % (NOW + 1, FAR_FUTURE_MS) in call[0]
              for call in behind.calls),
          "the tail partition runs from the clock reading to the far future")
    check(census(Catalog(rows(5)), base="orgid:ORG123", now_ms=NOW,
                 types=()).reconciled is True,
          "an org with nothing after the clock reading still reconciles")

    # THE HEADLINE CASE: an org past the ceiling, which every other tool
    # reports as 10,000 and calls done.
    big_rows = rows(10400, spread=DAY // 16)
    big = Catalog(big_rows)
    check(big("created:[0 TO %d]" % NOW, 1, 100)["total"] == SEARCH_CEILING,
          "the stand-in server reports 10,000 for an org of 10,400, the way "
          "the real one does  <-- pinned defect")
    huge = census(big, base="orgid:ORG123", now_ms=NOW, types=())
    check(huge.total == 10400,
          "an org of 10,400 items counts to 10,400, not to the 10,000 the "
          "server reported  <-- pinned defect")
    check(huge.bisections >= 1, "getting there needed at least one bisect")
    check(len(huge.partitions) == huge.bisections + 2,
          "one more finished partition than bisections, which is what a binary "
          "split of the org produces, plus the tail")
    check(huge.reconciled is True,
          "every finished partition reconciled against the server's own count")
    check(all(p[1] < SEARCH_CEILING for p in huge.partitions),
          "not one finished partition sat on the ceiling  <-- pinned defect")
    check(sum(p[2] for p in huge.partitions) == 10400,
          "the partitions add up to the total, with no item counted twice")
    check(len(set(r["id"] for r in huge.items.values())) == 10400,
          "and all 10,400 ids are distinct")

    exactly = Catalog(rows(SEARCH_CEILING, spread=DAY // 16))
    at_cap = census(exactly, base="orgid:ORG123", now_ms=NOW, types=())
    check(at_cap.total == SEARCH_CEILING,
          "an org of exactly 10,000 items still counts to 10,000")
    check(at_cap.bisections >= 1,
          "but it was bisected first, because 10,000 could not be proven "
          "complete without splitting it  <-- pinned defect")

    # A day that will not come apart on dates, which is what the owner level is
    # for: 10,400 items loaded by four users on one afternoon.
    one_day = rows(10400, owner=("ann", "bob", "cal", "dee"), spread=0)
    for n, row in enumerate(one_day):
        row["created"] = BASE + (n % 1000)          # all inside one second
    crammed = census(Catalog(one_day), base="orgid:ORG123", now_ms=NOW,
                     owners=("ann", "bob", "cal", "dee"), types=())
    check(crammed.total == 10400,
          "10,400 items created in one second are still all counted, by "
          "splitting on owner  <-- pinned defect")
    check(any("owner:(" in p[0] for p in crammed.partitions),
          "the census fell through to an owner clause to get there")
    check(crammed.reconciled is True, "and the owner split reconciles")

    # An item moved to ann between the read of owner:(ann OR bob) and the read
    # of -owner:(ann OR bob) is in neither half. Each half reconciles on its
    # own, so the census used to count 10,399 and exit 0.
    racing = Catalog([dict(r) for r in one_day])
    moved = [r for r in racing.rows if r["owner"] == "cal"][0]
    transfer = []

    def transfer_fetch(query, start, num):
        if "-owner:(" in query and not transfer:
            moved["owner"] = "ann"
            racing._cache.clear()
            transfer.append(moved["id"])
        return racing(query, start, num)

    lost = census(transfer_fetch, base="orgid:ORG123", now_ms=NOW,
                  owners=("ann", "bob", "cal", "dee"), types=())
    check(transfer and moved["id"] not in lost.items,
          "the repro really loses the transferred item  <-- pinned defect")
    check(lost.reconciled is False and exit_code(lost) == 1,
          "an owner transfer between the two halves of a split is not a clean "
          "exit 0  <-- pinned defect")
    check(any("changed owner" in w and "owner:(ann OR bob)" in w
              for w in lost.warnings),
          "and the warning names the first half that grew")
    check(len(lost.warnings) == 1,
          "one warning per changed half, none for the halves that held")
    check(lost.pages == crammed.pages,
          "the recheck reads totals only, so the page count does not move")

    stale = census(Catalog(one_day), base="orgid:ORG123", now_ms=NOW,
                   owners=("ann", "bob"), types=("Feature Service",))
    check(stale.total == 10400,
          "an owner list missing half the org still counts every item, because "
          "the other half of each split is 'not these owners'  <-- pinned "
          "defect")

    typed_rows = rows(10400, kind=("PDF", "CSV"), spread=0)
    for n, row in enumerate(typed_rows):
        row["created"] = BASE
    by_type = census(Catalog(typed_rows), base="orgid:ORG123", now_ms=NOW,
                     owners=(), types=("PDF", "CSV"))
    check(by_type.total == 10400,
          "10,400 items with one owner and one created date are counted by "
          "splitting on item type")
    check(any("type:(" in p[0] for p in by_type.partitions),
          "the type clause is what got them apart")

    # THE REFUSAL: a portal that answers the cap however narrow the query.
    liar = Catalog(rows(50), reported=SEARCH_CEILING)
    message = refuses_census(
        lambda: census(liar, base="orgid:ORG123", now_ms=BASE + 30 * DAY,
                       owners=(), types=()),
        "a partition still at the cap after bisecting down to one day REFUSES "
        "rather than returning a number  <-- pinned defect")
    check("No total can be proven" in message,
          "the refusal says no total can be proven")
    check("created:[" in message, "the refusal names the partition it gave up on")
    check(len(liar.calls) > 1, "it did try to split before refusing")
    over_budget = refuses_census(
        lambda: census(Catalog(rows(50), reported=SEARCH_CEILING),
                       base="orgid:ORG123", now_ms=NOW, owners=(), types=(),
                       max_partitions=4),
        "a census that runs past --max-partitions refuses instead of bisecting "
        "for ever  <-- pinned defect")
    check("gave up after 4 partitions" in over_budget,
          "the budget refusal says how many partitions it visited")
    raises(lambda: census(Catalog([]), max_partitions=0),
           "a partition budget of zero raises")

    # A total that moves under the census, which is a warning and not a silent
    # overwrite of the number that was read first.
    # The fourth call is the empty tail partition, which reports no movement.
    moving = Catalog(rows(250), moving=[250, 251, 252, 0])
    drifted = census(moving, base="orgid:ORG123", now_ms=NOW, types=())
    check(drifted.total == 250,
          "the census reports the ids it actually read, not the total that "
          "moved  <-- pinned defect")
    check(drifted.reconciled is False, "a moving total does not reconcile")
    check(len(drifted.warnings) == 2,
          "both the move and the row-count mismatch it caused are reported")
    check(any("moved from 250 to 252" in w for w in drifted.warnings),
          "the warning names the first and the last total the server gave")
    check(exit_code(drifted) == 1, "an unreconciled census exits 1")
    check(exit_code(result) == 0, "a reconciled census exits 0")
    check(exit_code(result, role="org_admin") == 0
          and exit_code(result, role="org_user") == 1
          and exit_code(result, role=None) == 1
          and exit_code(drifted, role="org_admin") == 1,
          "a census by a role other than org_admin exits 1, an unreadable "
          "role too  <-- pinned defect")
    check(exit_code(result, anonymous=True, role=None) == 0
          and exit_code(drifted, anonymous=True, role=None) == 1,
          "an anonymous run is not held to the role, but still exits 1 on "
          "drift")
    # Exit 1 means the census did NOT reconcile. The docstring once read
    # "reconciled with warnings", which says the opposite of the document's own
    # "reconciled": false, and an operator reading the exit code would have
    # trusted a total the tool had just disowned.
    check(drifted.reconciled is False
          and build_inventory(PORTAL, "q", "t", drifted, True)["reconciled"] is False,
          "and exit 1 lines up with reconciled=false in the document, not with "
          "a census that reconciled  <-- pinned defect")
    check(describe(drifted)[0].startswith("%d item(s)" % drifted.total)
          and "RECONCILIATION WARNINGS" in describe(drifted)
          and "treat it as a floor" in " ".join(describe(drifted)),
          "and the report calls that total a floor rather than an answer")

    # Overlapping partitions, which the union by id has to absorb. The first
    # crawl reports the ceiling, so the census bisects, and then BOTH halves
    # hand back the same two items. An item edited between the two reads does
    # exactly this, because the halves are split on a date the edit moved.
    pair = [dict(rows(1)[0], id="a"), dict(rows(1)[0], id="b")]
    seen = {"n": 0}

    def overlapping_fetch(query, start, num):
        if "created:[%d TO" % (NOW + 1) in query:
            return {"total": 0, "results": [], "nextStart": -1}
        seen["n"] += 1
        if seen["n"] == 1:
            return {"total": SEARCH_CEILING, "results": [], "nextStart": -1}
        return {"total": 2, "nextStart": -1, "results": pair}
    union = census(overlapping_fetch, base="orgid:ORG123", now_ms=NOW, types=())
    check(union.total == 2,
          "two partitions that both return the same two items count two, not "
          "four  <-- pinned defect")
    check(len(union.partitions) == 3 and union.bisections == 1,
          "the overlap came from two real partitions after one bisect, and "
          "the tail")
    check(sum(p[2] for p in union.partitions) == 4,
          "the partitions did return four rows between them, which is what the "
          "union by id absorbed")

    # A capped partition's rows are thrown away rather than kept. Here the root
    # reports the ceiling and hands back an item that is gone by the time the
    # children are read, so no complete partition ever vouches for it. Keeping
    # what a capped partition returned would put that item in the inventory and
    # report a total one higher than anything the tool can prove.
    ghost = {"id": "ghost0", "title": "Deleted mid-crawl", "owner": "ann",
             "type": "Web Map", "created": BASE, "modified": BASE,
             "access": "org", "size": 0, "numViews": 0}
    settled = Catalog(rows(3))
    root_q = partition_query(Partition("orgid:ORG123", EPOCH_START_MS, NOW))

    def haunted(query, start, num):
        if query == root_q:
            return {"total": SEARCH_CEILING, "start": 1, "num": 1,
                    "nextStart": -1, "results": [ghost]}
        return settled(query, start, num)

    haunt = census(haunted, base="orgid:ORG123", now_ms=NOW, types=())
    check("ghost0" not in haunt.items,
          "an item seen ONLY in a partition that sat on the ceiling is not in "
          "the census, because nothing complete vouches for it  "
          "<-- pinned defect")
    check(haunt.total == 3 and haunt.bisections >= 1,
          "only the items the finished partitions proved are counted, and the "
          "capped root was really split")
    check(all("ghost0" not in q for q, _t, _n in haunt.partitions)
          and root_q not in [q for q, _t, _n in haunt.partitions],
          "and the capped root is not listed as a finished partition")

    echoed = []
    census(big, base="orgid:ORG123", now_ms=NOW, types=(), echo=echoed.append)
    check(any("splitting" in line for line in echoed),
          "the progress echo says when a partition is split")
    check(any("item(s) from" in line for line in echoed),
          "and how many items each finished partition gave")

    # ---- the inventory document
    record = item_record({"id": "abc", "title": "Parcels", "owner": "jsmith",
                          "type": "Feature Service", "created": 1, "modified": 2,
                          "access": "public", "size": 99, "numViews": 7,
                          "url": "https://x/FeatureServer?token=LIVE"})
    check(record["id"] == "abc" and record["access"] == "public",
          "an item record keeps the inventory fields")
    check(sorted(record) == sorted(CSV_FIELDS),
          "and holds exactly the CSV columns, no more")
    check("url" not in record,
          "the service url is dropped, because it can carry a token  "
          "<-- pinned defect")
    thin = item_record({"id": "abc"})
    check(thin["title"] == "" and thin["owner"] == "",
          "missing text fields become empty strings, not None")
    check(thin["created"] == 0 and thin["size"] == 0,
          "missing numbers become zero, not None")
    check(thin["access"] == "private",
          "an item with no access field is recorded private, the safe reading")
    check(item_record({"id": "a", "size": -1})["size"] == -1,
          "a size of -1 is kept as the number the portal sent")

    document = build_inventory(PORTAL, "orgid:ORG123", "2026-09-16T00:00:00Z",
                               result, True)
    check(document["total"] == 250 and document["reconciled"] is True,
          "the inventory document carries the total and the verdict")
    check(document["url"] == PORTAL and document["query"] == "orgid:ORG123",
          "and says which org and which query it came from")
    check(len(document["items"]) == 250, "and holds every item")
    # Read back from a portal that returned the rows BACKWARDS. Asserting that
    # the first id is below the last proves nothing when the crawl already read
    # them in order: it would still pass with the sort taken out.
    scrambled = census(Catalog(list(reversed(rows(5)))), base="orgid:ORG123",
                       now_ms=NOW, types=())
    check(list(scrambled.items) != sorted(scrambled.items),
          "the crawl really did read those ids backwards, or the next "
          "assertion would prove nothing  <-- pinned defect")
    jumbled = build_inventory(PORTAL, "q", "t", scrambled, True)
    check([it["id"] for it in jumbled["items"]]
          == sorted(it["id"] for it in jumbled["items"]),
          "items are written in id order even when the portal returned them "
          "backwards, so an unchanged org writes the same bytes twice  "
          "<-- pinned defect")
    check(document["partitions"][0]["reported"] == 250,
          "the partitions are listed with what each one reported")
    check(build_inventory(PORTAL, "q", "t", drifted, True)["warnings"],
          "an unreconciled census carries its warnings into the document")

    # ---- rendering
    lines = describe(result)
    check(lines[0].startswith("250 item(s), read in 2 partition(s)"),
          "the summary leads with the total and the partition count")
    check("0 bisection(s)" in lines[0],
          "the summary names how many bisections were needed")
    check(any("Every partition reconciled" in line for line in lines),
          "a clean census says so")
    lines = describe(drifted)
    check(any("RECONCILIATION WARNINGS" in line for line in lines),
          "an unreconciled census leads its warnings with a heading")
    check(any("treat it as a floor" in line for line in lines),
          "and says the total is a floor")
    check(describe(huge)[0].count("bisection") == 1,
          "a bisected census reports its bisections once")

    check(printable("plain", "cp1252") == "plain",
          "an ascii title passes through a cp1252 console")
    check(printable(chr(0x6c34), "cp1252") == "?",
          "a title a cp1252 console cannot encode is replaced, not crashed on  "
          "<-- pinned defect")
    check(printable(chr(0x6c34), "utf-8") == chr(0x6c34),
          "a console that can encode the title gets the real title")
    check(printable("x", None) == "x", "a stream with no encoding is left alone")
    check(printable("x", "no-such-codec") == "x",
          "an encoding python has never heard of is left alone, not raised on")

    check(is_http_url("https://x.maps.arcgis.com") is True,
          "an https url is openable")
    check(is_http_url("http://portal.local/portal") is True,
          "a plain http url is openable")
    check(is_http_url("portal.example.com") is False,
          "a url with no scheme is refused, because urllib quotes the whole "
          "url back into its error and that url carries the token  "
          "<-- pinned defect")
    check(is_http_url("") is False, "an empty url is refused")
    check(is_http_url(None) is False, "a missing url is refused")

    check(redact("token=SECRET1", "SECRET1") == "token=" + REDACTED,
          "a secret is removed before printing")
    check(redact("q?token=AB%2BCD%2F%3DX&f=json", "AB+CD/=X")
          == "q?token=" + REDACTED + "&f=json",
          "a token holding + / or = is removed from the percent-encoded form "
          "urllib quotes back in a bad-url error  <-- pinned defect")
    check(redact("a b%20c", "a b c") == "a b%20c",
          "a secret is only removed where it appears, not guessed at")
    check(redact("a=1", None, "") == "a=1",
          "a redact with nothing to remove leaves the text alone")
    check(redact("id 7 failed", 7) == "id %s failed" % REDACTED,
          "a secret that is not a string is still removed  <-- pinned defect")

    # ---- the portal, answered in process
    #
    # No credential is real. _opener is swapped for a
    # stand-in that answers the REST paths the portal answers, including the
    # failures that matter: a 200 carrying an error envelope, which is how
    # ArcGIS reports a dead token, and an exception out of urllib that quotes
    # the failing url back with the token still in it.

    class FakeResponse(object):
        def __init__(self, body):
            self.body = body

        def read(self):
            return json.dumps(self.body).encode("utf-8")

    class FakePortal(object):
        """A portal answering in process from a Catalog and a user list."""

        def __init__(self, catalog=None, users=(), org="ORG123",
                     token="TESTTOKEN", open_error=None, fail_search_after=None,
                     role="org_admin"):
            self.catalog = catalog
            self.users = list(users)
            self.org = org
            self.role = role
            self.token = token
            self.open_error = open_error
            self.fail_search_after = fail_search_after
            self.searches = 0
            self.targets = []
            self.posted = []
            self.insecure = None

        def __call__(self, insecure):          # stands in for _opener(insecure)
            self.insecure = insecure
            return self

        def open(self, target, data=None, timeout=None):
            if data is None:
                path, _sep, query = target.partition("?")
            else:
                path, query = target, data.decode("utf-8")
                self.posted.append(query)
            self.targets.append(target)
            if self.open_error is not None:
                raise self.open_error(target)
            return FakeResponse(self._body(path, dict(
                urllib.parse.parse_qsl(query))))

        def _body(self, path, params):
            if path.endswith("/generateToken"):
                if params.get("password") != "hunter2":
                    return {"error": {"code": 400,
                                      "message": "Invalid username or password."}}
                return {"token": self.token, "expires": 9999999999999}
            if path.endswith("/portals/self"):
                me = {"id": self.org} if self.org else {}
                if self.role and params.get("token"):
                    me["user"] = {"username": "ann", "role": self.role}
                return me
            if path.endswith("/portals/self/users"):
                start = int(params.get("start") or 1)
                num = min(int(params.get("num") or 10), SEARCH_MAX_NUM)
                page = [row if isinstance(row, dict) else {"username": row}
                        for row in self.users[start - 1:start - 1 + num]]
                nxt = start + len(page)
                return {"total": len(self.users), "start": start,
                        "num": len(page),
                        "nextStart": -1 if nxt > len(self.users) else nxt,
                        "users": page}
            if path.endswith("/search"):
                self.searches += 1
                if (self.fail_search_after is not None
                        and self.searches > self.fail_search_after):
                    return {"error": {"code": 498, "message": "Invalid token."}}
                return self.catalog(params.get("q") or "",
                                    int(params.get("start") or 1),
                                    int(params.get("num") or 10))
            return {"error": {"code": 400, "message": "unhandled " + path}}

    def serving(a_portal, fn):
        """Run fn with that portal answering every call, then put _opener back."""
        saved = globals()["_opener"]
        globals()["_opener"] = a_portal
        try:
            return fn()
        finally:
            globals()["_opener"] = saved

    class Console(io.StringIO):
        """A stdout that reports an encoding, the way a real console does."""
        encoding = "utf-8"

    class StrictConsole(Console):
        """A console that raises on a character its encoding cannot hold."""

        def write(self, text):
            text.encode(self.encoding)
            return Console.write(self, text)

    def captured(fn, encoding="utf-8", console_class=Console):
        """Run fn with stdout and stderr collected. Returns (result, text)."""
        console = console_class()
        console.encoding = encoding
        saved = (sys.stdout, sys.stderr)
        sys.stdout, sys.stderr = console, console
        try:
            outcome = fn()
        finally:
            sys.stdout, sys.stderr = saved
        return outcome, console.getvalue()

    portal = FakePortal(Catalog(rows(250)), users=["ann", "bob"])
    fetched = serving(portal, lambda: crawl_partition(
        portal_fetch(PORTAL, "TESTTOKEN"), "created:[0 TO %d]" % NOW))[0]
    check(len(fetched) == 250, "the real fetch callable pages a live portal")
    check(all("token=TESTTOKEN" in t for t in portal.targets),
          "the token is sent with every page")
    check(all("num=100" in t for t in portal.targets),
          "the page size is clamped before it is sent")
    check(all("f=json" in t for t in portal.targets),
          "every call asks for f=json, or the portal answers with an html page "
          "and json.loads gets a doctype  <-- pinned defect")
    check(all("sortField=created" in t for t in portal.targets),
          "results are sorted by created, the field the bisect partitions on  "
          "<-- pinned defect")
    check(portal.insecure is False,
          "the default opener verifies the certificate  <-- pinned defect")

    anon = FakePortal(Catalog(rows(5)))
    serving(anon, lambda: crawl_partition(portal_fetch(PORTAL, None),
                                          "created:[0 TO %d]" % NOW))
    check(not any("token=" in t for t in anon.targets),
          "an anonymous read sends no token parameter at all")

    live = FakePortal(Catalog(rows(10400, spread=DAY // 16)),
                      users=["ann", "bob"])
    counted = serving(live, lambda: census(
        portal_fetch(PORTAL, "TESTTOKEN"), base="orgid:ORG123", now_ms=NOW,
        types=()))
    check(counted.total == 10400,
          "a portal past the ceiling is counted through the real fetch layer, "
          "not stopped at 10,000  <-- pinned defect")
    check(live.searches > 100,
          "which took more pages than the ceiling alone would have allowed")

    unhandled = fails(
        lambda: serving(portal, lambda: _call(PORTAL, "content/users/x", {})),
        "the stand-in portal refuses a REST path it does not implement, so a "
        "call this tool should not be making cannot pass  <-- pinned defect")
    check("unhandled" in unhandled, "and says which path it would not answer")

    # A captive portal login page, a proxy error page and a maintenance notice
    # can all parse as valid JSON without being a portal response. Guarded in
    # _call, because every caller below it goes straight to body.get() and
    # would raise AttributeError instead.
    class NotAnObject(FakePortal):
        def _body(self, path, params):
            return ["maintenance", "in progress"]

    shaped = fails(lambda: serving(NotAnObject(),
                                   lambda: portal_self(PORTAL, None)),
                   "a portal answering with a JSON array is refused, not fed "
                   "to body.get() as a traceback  <-- pinned defect")
    check("not an" in shaped and "list" in shaped,
          "and the refusal says what came back instead")
    fails(lambda: serving(NotAnObject(), lambda: org_owners(PORTAL, "T")),
          "the same guard covers the user list, because it is one guard in "
          "_call and not one per caller")
    fails(lambda: serving(NotAnObject(),
                          lambda: crawl_partition(portal_fetch(PORTAL, "T"),
                                                  "created:[0 TO 1]")),
          "and the search crawl")

    check(serving(portal, lambda: portal_self(PORTAL, "TESTTOKEN"))["id"]
          == "ORG123",
          "the org id is read from portals/self")
    check(serving(FakePortal(org=None),
                  lambda: portal_self(PORTAL, None)).get("id") is None,
          "a portal that will not name its org gives no id, not a wrong one")
    blind = FakePortal(org=None)
    serving(blind, lambda: portal_self(PORTAL, None))
    check(not any("token=" in t for t in blind.targets),
          "portals/self is asked anonymously when there is no token")

    many_users = FakePortal(users=["user%03d" % n for n in range(250)])
    names = serving(many_users, lambda: org_owners(PORTAL, "TESTTOKEN"))
    check(len(names) == 250,
          "the org's user list is paged past the first hundred  "
          "<-- pinned defect")
    check(names[0] == "user000" and names[-1] == "user249",
          "the first and the last username both survive the paging")
    check(serving(portal, lambda: org_owners(PORTAL, None)) == (),
          "an anonymous run reads no user list and asks for none")
    quiet = FakePortal(users=[])
    check(serving(quiet, lambda: org_owners(PORTAL, "TESTTOKEN")) == (),
          "an org whose user list comes back empty gives an empty tuple")
    nameless = FakePortal(users=[])
    nameless.users = [{"nothing": "here"}]
    check(serving(nameless, lambda: org_owners(PORTAL, "TESTTOKEN")) == (),
          "a user row with no username is skipped, not added as None")

    # ---- the credential, which must never be printed, logged or stored
    os.environ[SECRET_ENV] = "from-the-environment"
    try:
        check(read_secret("gis_admin") == "from-the-environment",
              "the password is read from %s when it is set" % SECRET_ENV)
    finally:
        del os.environ[SECRET_ENV]
    asked = []
    real_getpass = getpass.getpass
    getpass.getpass = lambda prompt: asked.append(prompt) or "typed-in"
    try:
        check(read_secret("gis_admin") == "typed-in",
              "with no environment variable the password is prompted for")
    finally:
        getpass.getpass = real_getpass
    check(len(asked) == 1 and "not echoed" in asked[0],
          "the prompt says the password will not be echoed  <-- pinned defect")
    check("gis_admin" in asked[0], "and names the user it is signing in as")

    check(serving(portal, lambda: generate_token(
        PORTAL, "gis_admin", "hunter2")) == "TESTTOKEN",
        "a username and password are exchanged for a token")
    check(any("password=hunter2" in body for body in portal.posted),
          "the password goes in the POST body, which is where it belongs")
    check(not any("password" in t for t in portal.targets),
          "the password never appears in a url  <-- pinned defect")
    check(all("f=json" in body for body in portal.posted),
          "the POST body asks for f=json too, not only the GETs")
    refused_login = fails(lambda: serving(portal, lambda: generate_token(
        PORTAL, "gis_admin", "wrongpass")),
        "a rejected sign-in is raised, not returned as a token")
    check("Invalid username or password" in refused_login,
          "the portal's own reason survives into the error  <-- pinned defect")
    check("wrongpass" not in refused_login,
          "the rejected password is not quoted back in the error")
    fails(lambda: serving(FakePortal(token=None), lambda: generate_token(
        PORTAL, "gis_admin", "hunter2")),
        "a sign-in that returns no token raises rather than returning None")

    leaky = FakePortal(open_error=lambda target: ValueError(
        "unknown url type: %r" % target))
    quoted = fails(lambda: serving(
        leaky, lambda: portal_self(PORTAL, "TESTTOKEN")),
                   "an exception out of urllib is raised as a portal error")
    check("TESTTOKEN" not in quoted,
          "the token in the url urllib quoted back is redacted out of the "
          "error  <-- pinned defect")
    check(REDACTED in quoted, "and the redaction is visible in its place")
    posted_back = FakePortal(open_error=lambda target: urllib.error.HTTPError(
        target, 500, "Internal Server Error", {}, None))
    check("500" in fails(lambda: serving(posted_back, lambda: generate_token(
        PORTAL, "gis_admin", "hunter2")),
        "an HTTP error from a gateway is raised as a portal error"),
        "and the status code survives into the message")
    hidden = fails(lambda: serving(
        FakePortal(open_error=lambda t: ValueError("boom %s" % t)),
        lambda: generate_token(PORTAL, "gis_admin", "hunter2")),
        "a POST that blows up inside urllib is raised")
    check("hunter2" not in hidden,
          "and the password it carried is not in the message  <-- pinned defect")

    encoded = fails(lambda: serving(
        FakePortal(open_error=lambda t: ValueError("bad url %r" % t)),
        lambda: portal_self(PORTAL, "AB+CD/=X")),
        "a request urllib refuses to open is raised")
    check("AB%2B" not in encoded and "AB+CD" not in encoded
          and REDACTED in encoded,
          "and a token with + / = is not in the message in either spelling  "
          "<-- pinned defect")

    class StringError(FakePortal):
        def _body(self, path, params):
            return {"error": "maintenance window"}

    texty = fails(lambda: serving(StringError(),
                                  lambda: portal_self(PORTAL, None)),
                  "a portal whose error is a bare string is raised, not an "
                  "AttributeError")
    check("maintenance window" in texty,
          "and the string is reported  <-- pinned defect")

    expiring = FakePortal(Catalog(rows(250)), fail_search_after=1)
    dead = fails(lambda: serving(expiring, lambda: census(
        portal_fetch(PORTAL, "TESTTOKEN"), base="orgid:ORG123", now_ms=NOW,
        types=())), "a token that dies mid-paging fails the census")
    check("498" in dead and "TESTTOKEN" not in dead,
          "the portal's error code is reported and the dead token is not  "
          "<-- pinned defect")

    insecure_portal = FakePortal(org="ORG123")
    serving(insecure_portal, lambda: portal_self(PORTAL, None, insecure=True))
    check(insecure_portal.insecure is True,
          "--insecure reaches the opener when it is asked for")
    check(isinstance(_opener(False), urllib.request.OpenerDirector),
          "the real opener builds without a certificate override")
    check(isinstance(_opener(True), urllib.request.OpenerDirector),
          "and builds one with the override too")

    # ---- the command line, end to end
    workdir = tempfile.mkdtemp(prefix="itemcensus-selftest-")
    # An ambient token would turn every anonymous run below into a signed-in one.
    ambient_token = os.environ.pop(TOKEN_ENV, "")
    # The loopback stub below must not go through a proxy the machine sets for
    # the internet. With HTTP_PROXY set, urllib sent these requests to the
    # proxy and the run died on a missing file instead of a failed assertion.
    # The run now sets a dead proxy itself, so the bypass is exercised on every
    # machine, not only on one that has a proxy. Both spellings of no_proxy,
    # because the lower-case one wins on Linux.
    proxy_env = {"no_proxy": "*", "NO_PROXY": "*",
                 "HTTP_PROXY": "http://127.0.0.1:9",
                 "HTTPS_PROXY": "http://127.0.0.1:9"}
    ambient_proxy = {n: os.environ[n] for n in proxy_env if n in os.environ}
    os.environ.update(proxy_env)
    try:
        out_json = os.path.join(workdir, "inventory.json")
        out_csv = os.path.join(workdir, "nested", "inventory.csv")

        cli_portal = FakePortal(Catalog(rows(250)), users=["ann", "bob"])
        code, seen = captured(lambda: serving(cli_portal, lambda: main(
            ["--url", PORTAL, "--token", "TESTTOKEN"])))
        check(code == 0, "a clean census exits 0")
        check("250 item(s)" in seen, "and prints the total")
        check("orgid:ORG123" in seen,
              "and the query it built from the signed-in org")
        check("TESTTOKEN" not in seen,
              "the token never reaches stdout  <-- pinned defect")
        check(not os.path.exists(out_json),
              "a run without --out writes nothing")

        # A page the crawl cannot trust is the portal's fault, not this tool's,
        # so it is an exit code and a message. Before this it was an uncaught
        # ValueError and a traceback with the query in it.
        idless = FakePortal(lambda q, s, n: {"total": 1, "nextStart": -1,
                                             "results": [{"title": "no id",
                                                          "url": "https://h/s?token=SVCTOKEN"}]})
        code, seen = captured(lambda: serving(idless, lambda: main(
            ["--url", PORTAL, "--token", "TESTTOKEN", "--query", "orgid:X"])))
        check(code == 2, "a search result with no id exits 2, not a traceback  "
                         "<-- pinned defect")
        check("error:" in seen and "no id" in seen,
              "and says what the portal sent that it would not accept")
        check("SVCTOKEN" not in seen and "title" in seen,
              "without quoting the row, whose service url can carry a token  "
              "<-- pinned defect")

        code, seen = captured(lambda: serving(
            FakePortal(lambda q, s, n: {}),
            lambda: main(["--url", PORTAL, "--token", "TESTTOKEN",
                          "--query", "orgid:X"])))
        check(code == 2 and "not a search response" in seen
              and "Every partition reconciled" not in seen,
              "an answer that is not a search response exits 2 and does not "
              "claim an empty org reconciled  <-- pinned defect")

        class NoUserList(FakePortal):
            def _body(self, path, params):
                if path.endswith("/portals/self/users"):
                    return {"error": {"code": 403, "message": "no access"}}
                return FakePortal._body(self, path, params)

        code, seen = captured(lambda: serving(
            NoUserList(Catalog(rows(250))),
            lambda: main(["--url", PORTAL, "--token", "TESTTOKEN",
                          "--query", "orgid:X"])))
        check(code == 0 and "250 item(s)" in seen
              and "the user list could not be read" in seen
              and "403 no access" in seen and "TESTTOKEN" not in seen,
              "a portal that refuses the user list is counted anyway, with a "
              "note and no owner bisect  <-- pinned defect")

        ambient_env = os.environ.copy()
        os.environ.pop(SECRET_ENV, None)
        real_getpass = getpass.getpass

        def no_console(prompt):
            raise EOFError()

        getpass.getpass = no_console
        try:
            code, seen = captured(lambda: main(
                ["--url", PORTAL, "--username", "gis_admin"]))
        finally:
            getpass.getpass = real_getpass
            os.environ.clear()
            os.environ.update(ambient_env)
        check(code == 2 and SECRET_ENV in seen and "Traceback" not in seen,
              "a run with no password and no console exits 2 and names %s, "
              "not a traceback and exit 1  <-- pinned defect" % SECRET_ENV)

        # Windows with no console: getpass would loop for ever, so the check
        # runs first. The stand-in getpass records any call it gets.
        prompted = []

        real_missing = globals()["console_missing"]
        globals()["console_missing"] = lambda: True
        getpass.getpass = lambda prompt: prompted.append(prompt) or "x"
        os.environ.pop(SECRET_ENV, None)
        try:
            code, seen = captured(lambda: main(
                ["--url", PORTAL, "--username", "gis_admin"]))
        finally:
            globals()["console_missing"] = real_missing
            getpass.getpass = real_getpass
            os.environ.clear()
            os.environ.update(ambient_env)
        check(code == 2 and SECRET_ENV in seen and not prompted,
              "a Windows run with no console never prompts, and exits 2 "
              "naming %s  <-- pinned defect" % SECRET_ENV)
        pipe_read, pipe_write = os.pipe()
        os.close(pipe_write)

        def refuse(name, flags):
            raise OSError("no console")

        check(console_missing(False, refuse) is False
              and console_missing(True, refuse) is True
              and console_missing(True, lambda n, f: pipe_read) is False,
              "console_missing is False off Windows, True when CONIN$ will not "
              "open, False when it opens")
        check(isinstance(console_missing(), bool),
              "console_missing answers on this host without raising")

        code, seen = captured(lambda: serving(
            FakePortal(Catalog(rows(250)), users=["ann"]),
            lambda: main(["--url", PORTAL, "--query", "orgid:X",
                          "--anonymous"])))
        check(code == 0 and "250 item(s)" in seen
              and "no token was used" in seen
              and "does not mean every item was seen" in seen
              and "Every partition reconciled" in seen,
              "an anonymous run says it saw public items only, "
              "though it exits 0  <-- pinned defect")
        check("no token was used" not in captured(lambda: serving(
            FakePortal(Catalog(rows(10)), users=["ann"]),
            lambda: main(["--url", PORTAL, "--token", "TESTTOKEN",
                          "--query", "orgid:X"])))[1],
              "a run with a token carries no anonymous note")

        code, seen = captured(lambda: serving(
            FakePortal(Catalog(rows(250)), users=["ann"]),
            lambda: main(["--url", PORTAL, "--token", "TESTTOKEN",
                          "--out", out_json])))
        check(code == 0 and "Check only" in seen,
              "an --out without --apply says it wrote nothing")
        check(not os.path.exists(out_json),
              "and the file really is not there  <-- pinned defect")

        code, seen = captured(lambda: serving(
            FakePortal(Catalog(rows(250)), users=["ann"]),
            lambda: main(["--url", PORTAL, "--token", "TESTTOKEN",
                          "--out", out_json, "--apply"])))
        check(code == 0 and os.path.exists(out_json),
              "--apply writes the inventory")
        with io.open(out_json, "r", encoding="utf-8") as handle:
            written = json.load(handle)
        check(written["total"] == 250 and len(written["items"]) == 250,
              "the written inventory holds every item")
        check(written["reconciled"] is True,
              "and records that the census reconciled")
        with io.open(out_json, "r", encoding="utf-8") as handle:
            raw_text = handle.read()
        check("TESTTOKEN" not in raw_text,
              "no credential reaches the inventory file on disk  "
              "<-- pinned defect")
        check("hunter2" not in raw_text, "and no password either")

        code, seen = captured(lambda: serving(
            FakePortal(Catalog(rows(30)), users=["ann"]),
            lambda: main(["--url", PORTAL, "--token", "TESTTOKEN",
                          "--out", out_csv, "--format", "csv", "--apply"])))
        check(code == 0 and os.path.exists(out_csv),
              "--format csv writes a csv, creating the directory it needs")
        with io.open(out_csv, "r", encoding="utf-8", newline="") as handle:
            table = list(csv.reader(handle))
        check(table[0] == list(CSV_FIELDS),
              "the csv leads with the fixed column header")
        check(len(table) == 31, "and holds one row per item under it")
        check(table[1][0].startswith("item"),
              "the first data row is an item id")
        check(all(len(row) == len(CSV_FIELDS) for row in table),
              "every csv row has every column")
        with io.open(out_csv, "r", encoding="utf-8", newline="") as handle:
            csv_text = handle.read()
        check("\n\n" not in csv_text.replace("\r\n", "\n"),
              "the csv has no blank line between rows, which is what newline='' "
              "prevents on windows  <-- pinned defect")
        check("TESTTOKEN" not in csv_text, "and carries no token")

        # ---- a spreadsheet must not run an item's title as a formula
        #
        # Anyone who can name an item chooses a string that lands in a cell of
        # the inventory, and the inventory is opened in Excel. The guard is one
        # apostrophe in front of text that starts with = + - @ tab or CR.
        leads = [("equals", "=HYPERLINK(" + chr(34) + "http://x.invalid" + chr(34) + ")"),
                 ("plus", "+1+1"), ("minus", "-2+3"), ("at", "@SUM(1+1)"),
                 ("tab", chr(9) + "=1+1"), ("cr", chr(13) + "=1+1")]

        def unguard(text):
            """What a reader of this csv does: strip exactly the one prefix."""
            if text[:1] == "'" and text[1:2] in CSV_FORMULA_LEAD:
                return text[1:]
            return text

        def one_item_csv(**fields):
            record = item_record({"id": "item1", "title": "T", "owner": "o",
                                  "type": "Web Map", "access": "org"})
            record.update(fields)
            doc = {"items": [record]}
            path = write_csv(doc, os.path.join(workdir, "hostile.csv"))
            with io.open(path, "r", encoding="utf-8", newline="") as handle:
                text = handle.read()
            return list(csv.reader(io.StringIO(text, newline="")))[1], text

        for field in ("id", "title", "owner", "type", "access"):
            for name, bad in leads:
                row, _text = one_item_csv(**{field: bad})
                got = row[CSV_FIELDS.index(field)]
                check(got == "'" + bad,
                      "%s starting with %s is written with a leading "
                      "apostrophe  <-- pinned defect" % (field, name))
                check(unguard(got) == bad,
                      "and stripping that one apostrophe restores the %s "
                      "exactly" % field)

        row, text = one_item_csv(title=leads[0][1], size=-1, numViews=0)
        check(row[CSV_FIELDS.index("size")] == "-1",
              "a size of -1 stays the number -1, not the text '-1  "
              "<-- pinned defect")
        check(all(row[CSV_FIELDS.index(f)].lstrip("-").isdigit()
                  for f in ("created", "modified", "size", "numViews")),
              "every numeric column stays numeric")
        check(row[CSV_FIELDS.index("owner")] == "o"
              and row[CSV_FIELDS.index("type")] == "Web Map",
              "an ordinary cell is written as it is")
        check(cell("") == "" and cell(0) == 0 and cell(-1) == -1,
              "an empty cell is not quoted and a number is not turned into text")
        check(cell("a=b") == "a=b" and cell("x-y") == "x-y",
              "a trigger character past the first is left alone")
        check(cell(" =1") == " =1",
              "a leading space is not a trigger, so the guard is exactly the six "
              "characters and not a guess")
        check(cell("'=x") == "'=x" and unguard(cell("'=x")) == "=x",
              "known limit: a title that already starts with an apostrophe "
              "then a trigger is written as it is, so a reader that strips "
              "cannot tell it from a guarded one")
        row, text = one_item_csv(title=leads[5][1])
        check(len(row) == len(CSV_FIELDS)
              and chr(34) + "'" + chr(13) + "=1+1" + chr(34) in text,
              "a guarded cell that starts with CR is quoted whole, so it "
              "cannot split the row")

        hostile_doc = build_inventory(PORTAL, "q", "t", census(
            Catalog(rows(3)), base="orgid:ORG123", now_ms=NOW, types=()),
            True)
        hostile_doc["items"][0].update(title=leads[0][1], owner=leads[4][1])
        hostile_json = write_json(hostile_doc,
                                  os.path.join(workdir, "hostile.json"))
        with io.open(hostile_json, "r", encoding="utf-8") as handle:
            check(json.load(handle)["items"][0]["title"] == leads[0][1],
                  "json is not a spreadsheet format and keeps the title "
                  "unquoted")
        round_csv = write_csv(hostile_doc, os.path.join(workdir, "round.csv"))
        with io.open(round_csv, "r", encoding="utf-8", newline="") as handle:
            back = list(csv.DictReader(handle))
        check([unguard(r["title"]) for r in back]
              == [r["title"] for r in hostile_doc["items"]]
              and [unguard(r["owner"]) for r in back]
              == [r["owner"] for r in hostile_doc["items"]],
              "a whole inventory round trips: write, read, strip one "
              "apostrophe, and every title and owner is what the portal sent  "
              "<-- pinned defect")
        check(all(r["title"][:1] not in CSV_FORMULA_LEAD for r in back)
              and all(r["owner"][:1] not in CSV_FORMULA_LEAD for r in back),
              "and not one title or owner in the file starts with a formula "
              "character")

        code, seen = captured(lambda: serving(
            FakePortal(Catalog(rows(250), moving=[250, 251, 252]),
                       users=["ann"]),
            lambda: main(["--url", PORTAL, "--token", "TESTTOKEN"])))
        check(code == 1, "a census with reconciliation warnings exits 1")
        check("RECONCILIATION WARNINGS" in seen, "and prints them")

        code, seen = captured(lambda: serving(
            FakePortal(Catalog(rows(50), reported=SEARCH_CEILING),
                       users=["ann"]),
            lambda: main(["--url", PORTAL, "--token", "TESTTOKEN",
                          "--out", out_json, "--apply",
                          "--max-partitions", "6"])))
        check(code == 2,
              "a census that cannot be completed exits 2, and does not print a "
              "total  <-- pinned defect")
        check("no total" in seen.lower(),
              "the refusal explains that no total can be given")
        check(written["total"] == 250,
              "and the inventory from the previous run was not overwritten by "
              "a refused one  <-- pinned defect")

        code, seen = captured(lambda: serving(
            FakePortal(Catalog(rows(10400, spread=DAY // 16)), users=["ann"]),
            lambda: main(["--url", PORTAL, "--token", "TESTTOKEN"])))
        check(code == 0 and "10400 item(s)" in seen,
              "the command line counts an org past the ceiling  "
              "<-- pinned defect")
        check("bisection(s)" in seen and "0 bisection(s)" not in seen,
              "and says how many bisections it needed")

        code, seen = captured(lambda: serving(
            FakePortal(Catalog(rows(10)), users=["ann"]),
            lambda: main(["--url", PORTAL, "--token", "TESTTOKEN",
                          "--query", "type:PDF"])))
        check(code == 0 and "type:PDF" in seen,
              "--query replaces the org query and is reported back")

        sign_in = FakePortal(Catalog(rows(10)), users=["ann"])
        os.environ[SECRET_ENV] = "hunter2"
        try:
            code, seen = captured(lambda: serving(sign_in, lambda: main(
                ["--url", PORTAL, "--username", "gis_admin"])))
        finally:
            del os.environ[SECRET_ENV]
        check(code == 0, "--username signs in and runs the census")
        check(any("password=hunter2" in body for body in sign_in.posted),
              "the password from the environment reached generateToken")
        check("hunter2" not in seen,
              "and never reached stdout  <-- pinned defect")

        os.environ[SECRET_ENV] = "wrongpass"
        try:
            code, seen = captured(lambda: serving(
                FakePortal(Catalog(rows(10))),
                lambda: main(["--url", PORTAL, "--username", "gis_admin"])))
        finally:
            del os.environ[SECRET_ENV]
        check(code == 2, "a rejected sign-in exits 2")
        check("wrongpass" not in seen,
              "and the rejected password is not printed  <-- pinned defect")

        no_org = FakePortal(Catalog(rows(10)), org=None)
        code, seen = captured(lambda: serving(no_org, lambda: main(
            ["--url", PORTAL, "--token", "TESTTOKEN"])))
        check(code == 2 and "pass --query" in seen,
              "a portal that will not name its org asks for --query rather "
              "than censusing the wrong thing  <-- pinned defect")

        code, seen = captured(lambda: serving(
            FakePortal(open_error=lambda t: ValueError("unknown url type: %r" % t)),
            lambda: main(["--url", PORTAL, "--token", "TESTTOKEN"])))
        check(code == 2 and "TESTTOKEN" not in seen,
              "a portal call that blows up exits 2 without printing the token")

        code, seen = captured(lambda: serving(
            FakePortal(Catalog(rows(10)), users=["ann", "bob"]),
            lambda: main(["--url", PORTAL, "--token", "TESTTOKEN"])))
        check(code == 0 and "2 owner(s) available" in seen,
              "the owner list read for the bisect is reported, so an operator "
              "can see the census had one to fall back on")

        public_only = FakePortal(Catalog(rows(10)), users=["ann"])
        code, seen = captured(lambda: serving(public_only, lambda: main(
            ["--url", PORTAL, "--anonymous"])))
        check(code == 0 and "10 item(s)" in seen,
              "an anonymous census of the public items needs no credential "
              "at all")
        check(not any("token=" in target for target in public_only.targets),
              "and sends no token on any call  <-- pinned defect")
        check("owner(s) available" not in seen,
              "an anonymous run reads no user list, because the portal would "
              "refuse it, and the bisect falls back on item type")

        # ---- an anonymous run has to be asked for, and says so in its file
        bare = FakePortal(Catalog(rows(10)), users=["ann"])
        code, seen = captured(lambda: serving(bare, lambda: main(
            ["--url", PORTAL])))
        check(code == EXIT_USAGE and bare.searches == 0
              and "--anonymous" in seen and "10 item(s)" not in seen,
              "no credential and no --anonymous exits 64 before any search, "
              "because exit 0 would pass public items for the org  "
              "<-- pinned defect")
        for flags, what in ((["--token", "TESTTOKEN"], "--token"),
                            (["--username", "gis_admin"], "--username")):
            clash = FakePortal(Catalog(rows(10)), users=["ann"])
            code, seen = captured(lambda: serving(clash, lambda: main(
                ["--url", PORTAL, "--anonymous"] + flags)))
            check(code == EXIT_USAGE and clash.searches == 0
                  and "TESTTOKEN" not in seen,
                  "--anonymous together with %s is refused" % what)
        os.environ[TOKEN_ENV] = "ENVTOKEN"
        try:
            code, seen = captured(lambda: main(
                ["--url", PORTAL, "--anonymous"]))
        finally:
            del os.environ[TOKEN_ENV]
        check(code == EXIT_USAGE and "ENVTOKEN" not in seen,
              "and so is --anonymous with a token in the environment")
        anon_csv = os.path.join(workdir, "anon.csv")
        code, seen = captured(lambda: main(
            ["--url", PORTAL, "--anonymous", "--out", anon_csv, "--format",
             "csv", "--apply"]))
        check(code == EXIT_USAGE and not os.path.exists(anon_csv)
              and "authenticated" in seen,
              "an anonymous csv is refused, since it has no column to say "
              "public items only  <-- pinned defect")
        anon_json = os.path.join(workdir, "anon.json")
        code, seen = captured(lambda: serving(
            FakePortal(Catalog(rows(10)), users=["ann"]),
            lambda: main(["--url", PORTAL, "--anonymous", "--out", anon_json,
                          "--apply"])))
        with io.open(anon_json, "r", encoding="utf-8") as handle:
            anon_doc = json.load(handle)
        check(code == 0 and anon_doc["authenticated"] is False
              and anon_doc["reconciled"] is True and anon_doc["role"] is None,
              "an anonymous inventory file records authenticated false, "
              "though it reconciled  <-- pinned defect")
        check(written["authenticated"] is True
              and build_inventory(PORTAL, "q", "t", result, False)[
                  "authenticated"] is False,
              "and a signed-in one records true")

        # ---- a token that is not an administrator's sees part of the org
        check(account_role({"user": {"role": "org_admin"}}) == "org_admin"
              and account_role({"user": {"role": "org_user"}}) == "org_user",
              "account_role reads the role out of portals/self")
        check(account_role({}) is None
              and account_role({"user": "ann"}) is None
              and account_role({"user": {}}) is None
              and account_role({"user": {"role": 7}}) is None
              and account_role({"user": {"role": "org" + chr(27) + "[31m"}})
              is None
              and account_role({"user": {"role": "org_" + chr(0xe9)}}) is None,
              "and gives None for a reply with no role, or a role that is not "
              "a plain word, so a hostile portal cannot put escape codes on "
              "the console  <-- pinned defect")
        check("NOTE" not in " ".join(describe(result))
              and "NOTE" not in " ".join(describe(result, role="org_admin")),
              "an administrator's census carries no note")
        limited = " ".join(describe(result, role="org_user"))
        check("role is org_user" in limited and "not org_admin" in limited
              and "exits 1" in limited,
              "a census by another role says what it could not see  "
              "<-- pinned defect")
        check("role is unreadable" in " ".join(describe(result, role=None)),
              "and so does one whose role the portal did not say")
        check("role is" not in " ".join(describe(result, anonymous=True,
                                                 role=None)),
              "an anonymous run gets the anonymous note and no role note")
        check(build_inventory(PORTAL, "q", "t", result, True, "org_user")[
            "role"] == "org_user"
              and build_inventory(PORTAL, "q", "t", result, False)[
                  "role"] is None,
              "the inventory records the role, None when none was read")

        for role, wording in (("org_user", "role is org_user"),
                              (None, "role is unreadable")):
            lone_json = os.path.join(workdir, "role-%s.json" % role)
            lone = FakePortal(Catalog(rows(10)), users=["ann"], role=role)
            code, seen = captured(lambda: serving(lone, lambda: main(
                ["--url", PORTAL, "--token", "TESTTOKEN", "--query",
                 "orgid:X", "--out", lone_json, "--apply"])))
            with io.open(lone_json, "r", encoding="utf-8") as handle:
                lone_doc = json.load(handle)
            check(code == 1 and wording in seen
                  and "Every partition reconciled" in seen
                  and lone_doc["role"] == role,
                  "a run whose role is %s says so on screen and in the file, "
                  "with --query given  <-- pinned defect" % role)
            check(code == 1 and os.path.exists(lone_json),
                  "and exits 1 rather than 0, since its total is a floor, "
                  "though the file is written  <-- pinned defect")
            check(any(t.split("?")[0].endswith("/portals/self")
                      for t in lone.targets),
                  "portals/self is read for the role even with --query")
            lone_csv = os.path.join(workdir, "role-%s.csv" % role)
            lone = FakePortal(Catalog(rows(10)), users=["ann"], role=role)
            code, seen = captured(lambda: serving(lone, lambda: main(
                ["--url", PORTAL, "--token", "TESTTOKEN", "--query",
                 "orgid:X", "--out", lone_csv, "--format", "csv",
                 "--apply"])))
            check(code == EXIT_USAGE and not os.path.exists(lone_csv)
                  and lone.searches == 0 and "TESTTOKEN" not in seen
                  and "--format json" in seen,
                  "a csv from a run whose role is %s is refused before any "
                  "search  <-- pinned defect" % role)
        admin_json = os.path.join(workdir, "role-admin.json")
        code, seen = captured(lambda: serving(
            FakePortal(Catalog(rows(10)), users=["ann"]),
            lambda: main(["--url", PORTAL, "--token", "TESTTOKEN",
                          "--out", admin_json, "--apply"])))
        with io.open(admin_json, "r", encoding="utf-8") as handle:
            check(code == 0 and "NOTE" not in seen
                  and json.load(handle)["role"] == "org_admin",
                  "an administrator's inventory records org_admin and "
                  "prints no note")
        anon_json_role = FakePortal(Catalog(rows(10)), users=["ann"])
        code, seen = captured(lambda: serving(anon_json_role, lambda: main(
            ["--url", PORTAL, "--anonymous", "--query", "orgid:X"])))
        check(not any(t.split("?")[0].endswith("/portals/self")
                      for t in anon_json_role.targets),
              "an anonymous run with --query still reads no portals/self")

        # ---- a csv has no column for an unreconciled census either
        drift_csv = os.path.join(workdir, "drift.csv")
        code, seen = captured(lambda: serving(
            FakePortal(Catalog(rows(250), moving=[250, 251, 252]),
                       users=["ann"]),
            lambda: main(["--url", PORTAL, "--token", "TESTTOKEN",
                          "--out", drift_csv, "--format", "csv",
                          "--apply"])))
        check(code == 1 and not os.path.exists(drift_csv)
              and "RECONCILIATION WARNINGS" in seen
              and "No csv was written" in seen,
              "an unreconciled census writes no csv and still exits 1  "
              "<-- pinned defect")
        drift_json = os.path.join(workdir, "drift.json")
        code, seen = captured(lambda: serving(
            FakePortal(Catalog(rows(250), moving=[250, 251, 252]),
                       users=["ann"]),
            lambda: main(["--url", PORTAL, "--token", "TESTTOKEN",
                          "--out", drift_json, "--apply"])))
        with io.open(drift_json, "r", encoding="utf-8") as handle:
            check(code == 1 and json.load(handle)["reconciled"] is False,
                  "the same census as json is written, and records "
                  "reconciled false")

        # ---- a mistyped credential flag is not echoed back
        for argv, shown in (
                (["--url", PORTAL, "--username", "bob", "--password",
                  "SECRETWORD1"], "--password"),
                (["--url", PORTAL, "--password=SECRETWORD1"], "--password"),
                (["--url", PORTAL, "--tokn", "SECRETWORD1"], "--tokn"),
                (["--url", PORTAL, "--tok", "SECRETWORD1"], "--tok"),
                (["--url", PORTAL, "--urll",
                  "https://h.example?token=SECRETWORD1"], "--urll"),
                (["--url", PORTAL, "SECRETWORD1"], "none"),
                (["--url", PORTAL, "--Pass=SECRETWORD1"], "none"),
                (["--url", PORTAL, "--" + "x" * 40 + "=SECRETWORD1"], "none")):
            def run_argv(argv=argv):
                try:
                    return main(argv)
                except SystemExit as exc:
                    return exc.code
            code, seen = captured(run_argv)
            check(code == EXIT_USAGE and "SECRETWORD1" not in seen
                  and "unrecognized arguments" in seen
                  and "flags among them: %s" % shown in seen,
                  "an unknown word in argv exits 64 and its value is not "
                  "echoed: %s  <-- pinned defect" % shown)
        code, seen = captured(lambda: main(["--url", PORTAL, "--anonymous",
                                            "--apply"]))
        check(code == EXIT_USAGE and "unrecognized" not in seen,
              "known flags raise no unrecognized-argument error")

        # ---- a password typed into the url never reaches a log
        bad_urls = ("http://ann:Sekret-Pass-99@127.0.0.1:1",
                    "ann:Sekret-Pass-99@127.0.0.1:1",
                    "http://ann:Sekret/Pass-99@127.0.0.1:1",
                    "http://ann:Sekret?Pass#99@127.0.0.1:1")
        for bad_url in bad_urls:
            code, seen = captured(lambda: main(
                ["--url", bad_url, "--token", "TESTTOKEN"]))
            check(code == EXIT_USAGE and "Sekret" not in seen
                  and "Pass-99" not in seen and "99@" not in seen,
                  "a url with user info is refused without echoing it: %s"
                  % bad_url.split("Sekret")[0] + "...  <-- pinned defect")
        check(has_userinfo("https://u:p@host") is True
              and has_userinfo("https://host/portal") is False,
              "has_userinfo tells a url with user info from one without")

        # ---- a token typed into the url never reaches a log or the file
        leaky_urls = ("http://127.0.0.1:9/?token=LEAKTOKEN9",
                      "portal.example.com/sharing/rest?token=LEAKTOKEN9",
                      "https://portal.example.com/#token=LEAKTOKEN9",
                      "https://portal.example.com?password=LEAKTOKEN9")
        for leaky in leaky_urls:
            code, seen = captured(lambda: main(
                ["--url", leaky, "--anonymous", "--query", "orgid:ORG1"]))
            check(code == EXIT_USAGE and "LEAKTOKEN9" not in seen
                  and "reading" not in seen,
                  "a url with a query or fragment is refused without echoing "
                  "it: %s  <-- pinned defect" % leaky.split("LEAKTOKEN9")[0])
        check(has_query_or_fragment("https://host/portal") is False
              and has_query_or_fragment("https://host/?token=x") is True
              and has_query_or_fragment("https://host/#x") is True,
              "has_query_or_fragment tells a bare portal address from one "
              "with a query or fragment")
        code, seen = captured(lambda: main(
            ["--url", "portal.example.com", "--anonymous"]))
        check(code == EXIT_USAGE and "portal.example" not in seen
              and "https://" in seen,
              "the scheme refusal does not print the url back  "
              "<-- pinned defect")

        # ---- a token that sees nothing is not a census of an empty org
        nothing_out = os.path.join(workdir, "nothing.csv")
        blind_token = FakePortal(Catalog(rows(0)), users=["ann"])
        code, seen = captured(lambda: serving(blind_token, lambda: main(
            ["--url", PORTAL, "--token", "TESTTOKEN", "--out", nothing_out,
             "--format", "csv", "--apply"])))
        check(code == 2 and "found 0 items" in seen
              and "Every partition reconciled" not in seen
              and not os.path.exists(nothing_out),
              "a token that finds no item exits 2 and writes no inventory, "
              "because exit 0 would pass a blind token for an empty org  "
              "<-- pinned defect")

        # a token from the environment, so it never has to be on argv
        env_portal = FakePortal(Catalog(rows(10)), users=["ann"])
        os.environ[TOKEN_ENV] = "ENVTOKEN"
        try:
            code, seen = captured(lambda: serving(env_portal, lambda: main(
                ["--url", PORTAL])))
            env_over = FakePortal(Catalog(rows(10)), users=["ann"])
            captured(lambda: serving(env_over, lambda: main(
                ["--url", PORTAL, "--token", "FLAGTOKEN"])))
        finally:
            del os.environ[TOKEN_ENV]
        check(code == 0 and any("token=ENVTOKEN" in t
                                for t in env_portal.targets),
              "a token in %s is used when --token is not given  <-- pinned "
              "defect" % TOKEN_ENV)
        check("ENVTOKEN" not in seen, "and is not printed")
        check(any("token=FLAGTOKEN" in t for t in env_over.targets)
              and not any("ENVTOKEN" in t for t in env_over.targets),
              "--token wins over the environment")

        # an inventory that cannot be written. The census is complete, so this
        # is neither exit 1 (the total is a floor) nor a traceback.
        code, seen = captured(lambda: serving(
            FakePortal(Catalog(rows(10)), users=["ann"]),
            lambda: main(["--url", PORTAL, "--token", "TESTTOKEN",
                          "--out", workdir, "--apply"])))
        check(code == EXIT_WRITE_FAILED and "could not write" in seen
              and "Traceback" not in seen and "10 item(s)" in seen,
              "an --out that is a directory exits %d with a message, not a "
              "traceback and not exit 1  <-- pinned defect" % EXIT_WRITE_FAILED)
        unencodable = rows(1)
        unencodable[0]["title"] = chr(0xd800)
        code, seen = captured(lambda: serving(
            FakePortal(Catalog(unencodable), users=["ann"]),
            lambda: main(["--url", PORTAL, "--token", "TESTTOKEN",
                          "--out", os.path.join(workdir, "lone.csv"),
                          "--format", "csv", "--apply"])))
        check(code == EXIT_WRITE_FAILED and "could not write" in seen,
              "a title that cannot be encoded as UTF-8 exits %d, not a "
              "traceback  <-- pinned defect" % EXIT_WRITE_FAILED)

        # A console that cannot encode the --out path. The file is written in
        # full, so the run must say so and keep the census exit code.
        accent_out = os.path.join(workdir, "caf" + chr(0xe9) + ".json")
        code, seen = captured(lambda: serving(
            FakePortal(Catalog(rows(10)), users=["ann"]),
            lambda: main(["--url", PORTAL, "--token", "TESTTOKEN",
                          "--out", accent_out, "--apply"])),
            encoding="ascii", console_class=StrictConsole)
        check(code == 0 and os.path.exists(accent_out)
              and "could not write" not in seen and "wrote " in seen,
              "an --out path the console cannot encode reports the finished "
              "write and exits with the census code, not 73 and not a "
              "traceback  <-- pinned defect")

        # the usage errors that never reach the portal
        exits = []
        try:
            captured(lambda: main(["--url", PORTAL, "--bogus"]))
        except SystemExit as exc:
            exits.append(exc.code)
        check(exits == [EXIT_USAGE],
              "an unknown flag exits %d, the usage code, not the 2 that means "
              "the census could not be completed  <-- pinned defect"
              % EXIT_USAGE)
        code, seen = captured(lambda: main([]))
        check(code == 64 and "--url is required" in seen,
              "no --url at all is a usage error")
        code, seen = captured(lambda: main(["--url", "portal.example.com"]))
        check(code == 64 and "https://" in seen,
              "a scheme-less --url is refused before any request is built  "
              "<-- pinned defect")
        code, seen = captured(lambda: main(["--url", PORTAL, "--apply"]))
        check(code == 64 and "--apply needs --out" in seen,
              "--apply with nowhere to write is a usage error")
        code, seen = captured(lambda: main(
            ["--url", PORTAL, "--username", "gis_admin", "--insecure"]))
        check(code == 64 and "unverified connection" in seen,
              "--insecure with --username is refused, because that posts the "
              "password down an unverified connection  <-- pinned defect")
        code, seen = captured(lambda: main(["--url", PORTAL, "--num", "0"]))
        check(code == 64 and "--num" in seen, "a --num of zero is a usage error")
        code, seen = captured(lambda: main(
            ["--url", PORTAL, "--max-partitions", "0"]))
        check(code == 64 and "--max-partitions" in seen,
              "a --max-partitions of zero is a usage error")

        # ---- a hostile title all the way through the command line
        bad_rows = rows(6)
        for bad, (_name, text) in zip(bad_rows, leads):
            bad["title"] = text
        bad_rows[0]["owner"] = leads[1][1]
        bad_rows[1]["size"] = -1
        hostile_csv = os.path.join(workdir, "cli-hostile.csv")
        code, seen = captured(lambda: serving(
            FakePortal(Catalog(bad_rows), users=["ann"]),
            lambda: main(["--url", PORTAL, "--token", "TESTTOKEN",
                          "--out", hostile_csv, "--format", "csv",
                          "--apply"])))
        with io.open(hostile_csv, "r", encoding="utf-8", newline="") as handle:
            cli_rows = list(csv.DictReader(handle))
        check(code == 0 and len(cli_rows) == 6
              and sorted(unguard(r["title"]) for r in cli_rows)
              == sorted(t for _n, t in leads)
              and all(r["title"][:1] not in CSV_FORMULA_LEAD for r in cli_rows),
              "--format csv --apply guards every hostile title the portal "
              "returned, and a reader strips them back  <-- pinned defect")
        check(sorted(r["size"] for r in cli_rows)[0] == "-1",
              "and the size of -1 the portal reported is still the number")

        # ---- the same run over a real socket
        #
        # Everything above swaps _opener for an in-process stand-in, so urllib's
        # own request, status and body handling never ran. This one serves the
        # same stand-in from http.server on 127.0.0.1 and lets the real opener
        # talk to it: a GET search, a POST generateToken, an HTTP 500, and a
        # port nothing listens on.
        import http.server
        import threading

        stub = FakePortal(Catalog(bad_rows), users=["ann"])

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self, path, query):
                if "boom" in query:
                    self.send_error(500)
                    return
                body = json.dumps(stub._body(path, dict(
                    urllib.parse.parse_qsl(query)))).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path, _sep, query = self.path.partition("?")
                self._answer(path, query)

            def do_POST(self):
                size = int(self.headers["Content-Length"])
                self._answer(self.path, self.rfile.read(size).decode("utf-8"))

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        local = "http://127.0.0.1:%d" % server.server_address[1]
        socket_csv = os.path.join(workdir, "socket.csv")
        os.environ[SECRET_ENV] = "hunter2"
        try:
            code, seen = captured(lambda: main(
                ["--url", local, "--username", "gis_admin", "--out",
                 socket_csv, "--format", "csv", "--apply"]))
        finally:
            del os.environ[SECRET_ENV]
        with io.open(socket_csv, "r", encoding="utf-8", newline="") as handle:
            socket_rows = list(csv.DictReader(handle))
        check(code == 0 and "6 item(s)" in seen and len(socket_rows) == 6,
              "a census over a real socket signs in, counts and writes the csv")
        check(sorted(unguard(r["title"]) for r in socket_rows)
              == sorted(t for _n, t in leads)
              and all(r["title"][:1] not in CSV_FORMULA_LEAD
                      for r in socket_rows),
              "and the titles it got off the wire are guarded the same way  "
              "<-- pinned defect")
        with io.open(socket_csv, "r", encoding="utf-8") as handle:
            socket_text = handle.read()
        check("hunter2" not in seen + socket_text
              and "TESTTOKEN" not in seen + socket_text,
              "neither the password nor the token it was issued reached "
              "stdout or the file")
        check(stub.searches > 0, "the stub answered real search requests")
        code, seen = captured(lambda: main(
            ["--url", local, "--token", "TESTTOKEN", "--query", "boom"]))
        check(code == 2 and "TESTTOKEN" not in seen,
              "an HTTP 500 from a real server exits 2 without the token")
        server.shutdown()
        server.server_close()
        thread.join()
        code, seen = captured(lambda: main(
            ["--url", local, "--token", "TESTTOKEN"]))
        check(code == 2 and "TESTTOKEN" not in seen and "error:" in seen,
              "a port nothing listens on exits 2 without the token")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        os.environ.update({TOKEN_ENV: ambient_token} if ambient_token else {})
        for name in proxy_env:
            os.environ.pop(name, None)
        os.environ.update(ambient_proxy)

    # ---- argument handling
    args = _parse(["--url", PORTAL])
    check(args.apply is False, "--apply defaults to OFF")
    check(args.insecure is False, "--insecure defaults to OFF")
    check(args.self_test is False, "--self-test defaults to OFF")
    check(args.token is None and args.username is None,
          "no credential is assumed")
    check(args.out is None, "nothing is written by default")
    check(args.format == "json", "the default format is json")
    check(args.num == SEARCH_MAX_NUM, "--num defaults to the server's cap")
    check(args.max_partitions == MAX_PARTITIONS,
          "--max-partitions defaults to the configured budget")
    check(_parse(["--self-test"]).self_test is True, "--self-test parses")
    check(_parse(["--url", PORTAL, "--apply"]).apply is True, "--apply is read")
    refuses(["--url", PORTAL, "--ap"],
            "a unique prefix of --apply is refused, not read as --apply  <-- pinned defect")
    check(_parse(["--url", PORTAL, "--insecure"]).insecure is True,
          "--insecure is read")
    check(_parse(["--url", PORTAL, "--token", "T"]).token == "T",
          "--token is read")
    check(_parse(["--url", PORTAL, "--username", "u"]).username == "u",
          "--username is read")
    check(_parse(["--url", PORTAL, "--query", "q"]).query == "q",
          "--query is read")
    check(_parse(["--url", PORTAL, "--out", "f.json"]).out == "f.json",
          "--out is read")
    check(_parse(["--url", PORTAL, "--format", "csv"]).format == "csv",
          "--format is read")
    check(_parse(["--url", PORTAL, "--num", "25"]).num == 25, "--num is read")
    check(_parse(["--url", PORTAL, "--max-partitions", "9"]).max_partitions == 9,
          "--max-partitions is read")
    refuses(["--url", PORTAL, "--format", "xlsx"],
            "a format nothing can write is refused by argparse")
    refuses(["--url", PORTAL, "--num", "lots"], "a non-numeric --num is refused")
    refuses(["--url"], "a --url with no value is refused")
    check("password" not in _parse(["--url", PORTAL]).__dict__,
          "there is no --password attribute at all, because argv is readable "
          "by every process on the box  <-- pinned defect")
    refuses(["--url", PORTAL, "--password", "hunter2"],
            "a --password flag is refused, it does not exist")

    # ---- the harness itself, which has to be able to report red
    #
    # A self-test whose failure path is never exercised is not a control: it
    # reports green because nothing ever calls the other branch. The harness is
    # run here against six deliberate failures, with its output swallowed and
    # its tally put back, so that a green run has still proven it can go red.
    def probe():
        check(False, "a false check must be recorded as a failure")
        raises(lambda: None, "a function that raises nothing must fail")
        raises(lambda: 1 / 0, "a function that raises the wrong thing must fail")
        refuses(["--self-test"], "an argv argparse accepts must fail")
        refuses_census(lambda: None, "a census that returns must fail")
        refuses_census(lambda: 1 / 0,
                       "a census that raises the wrong thing must fail")
        return [fails(lambda: None, "a call that does not raise must fail"),
                fails(lambda: 1 / 0,
                      "a call that raises the wrong thing must fail"),
                fails(lambda: bisect(Partition("q", 0, 0)),
                      "a call that refuses the census instead of failing the "
                      "portal must fail")]

    kept_passed, kept_failed = passed[0], list(failed)
    returned, noise = captured(probe)
    probe_passed, probe_failed = passed[0], list(failed)
    passed[0], failed[:] = kept_passed, kept_failed
    check(len(probe_failed) - len(kept_failed) == 9,
          "the harness records a false check, a missing exception, two wrong "
          "exceptions, an argv argparse accepted, a census that did not "
          "refuse, a census that raised the wrong thing, a portal call that "
          "did not fail and one that refused instead as nine failures, so a "
          "broken tool turns this self-test red  <-- pinned defect")
    check(probe_passed == kept_passed,
          "not one of those nine was counted as a pass")
    check(noise.count("FAIL  ") == 9,
          "every recorded failure prints a FAIL line the operator can see")
    check(returned == ["", "", ""],
          "a portal call that did not fail the right way yields no message to "
          "assert on")

    # ---- the whole self-test has to be able to report red
    #
    # The probe above proves the harness records a failure. This proves the
    # summary a person reads does too: a tool with one broken function, run
    # through the real self_test(), must exit 1 and name what failed. The inner
    # run skips this block, or it would recurse for ever.
    if not _nested:
        saved_fn = globals()["is_http_url"]
        globals()["is_http_url"] = lambda url: True
        try:
            broken, broken_out = captured(lambda: self_test(_nested=True))
        finally:
            globals()["is_http_url"] = saved_fn
        check(broken == 1 and "FAILED: a url with no scheme is refused" in
              broken_out and re.search(r"\d+ assertions, [1-9]\d* failed",
                                       broken_out),
              "a tool with one broken function turns the whole self-test red: "
              "exit 1, and the summary names the assertion that failed")

        # ---- the script run as a script, and imported as a module
        import runpy
        saved_argv = sys.argv
        sys.argv = ["itemcensus.py"]
        try:
            ran_code = []
            try:
                captured(lambda: runpy.run_path(__file__, run_name="__main__"))
            except SystemExit as exc:
                ran_code.append(exc.code)
            imported = runpy.run_path(__file__, run_name="itemcensus_imported")
        finally:
            sys.argv = saved_argv
        check(ran_code == [64],
              "run as a script, it exits with main()'s code: 64 for no --url")
        check(callable(imported["main"]),
              "imported as a module, it defines main() and runs nothing")

    print("-" * 68)
    total = passed[0] + len(failed)
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for item in failed:
            print("  FAILED: %s" % item)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# ----------------------------------------------------------------------- cli

class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a usage error, which this tool reserves for 'the
    census could not be completed'. A mistyped flag in a scheduled job must not
    look like a portal failure."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, "%s: error: %s\n" % (self.prog, message))

    def parse_args(self, args=None, namespace=None):
        """Refuse an unknown word without printing it.

        argparse's own message quotes every unrecognized word. A mistyped
        --password hunter2 or --tokn SECRET would put the secret on stderr, and
        so in the scheduler log. Only a flag-shaped word is named; a value or a
        bare word is counted.
        """
        known, extras = self.parse_known_args(args, namespace)
        if extras:
            flags = [word.split("=", 1)[0] for word in extras
                     if re.match(r"--[a-z][a-z0-9-]{0,30}(=|$)", word)]
            self.error("unrecognized arguments: %d word(s) not shown, because "
                       "one may be a credential; flags among them: %s"
                       % (len(extras), " ".join(flags) if flags else "none"))
        return known


def _parse(argv):
    ap = _Parser(
        prog="itemcensus.py",
        description="Inventory every item in an ArcGIS organization, and "
                    "refuse to report a total the server cannot prove is "
                    "complete.",
        epilog="Read-only. No flag changes anything in the organization. The "
               "password is never a flag: export %s or answer the prompt."
               % SECRET_ENV,
        allow_abbrev=False,
    )
    ap.add_argument("--url",
                    help="portal url, e.g. https://portal.example.com")
    ap.add_argument("--query",
                    help="search query to census (default: orgid of the "
                         "signed-in org)")
    ap.add_argument("--token",
                    help="an existing portal token. Prefer %s: a value on "
                         "argv is readable by every process on the box and is "
                         "kept in shell history and audit logs." % TOKEN_ENV)
    ap.add_argument("--anonymous", action="store_true",
                    help="census without any credential. The search then sees "
                         "only public items, so the total is not the "
                         "organization's. Needed when no --token, %s or "
                         "--username is given, and refused with one."
                         % TOKEN_ENV)
    ap.add_argument("--username",
                    help="generate a token for this user. The password comes "
                         "from %s or an unechoed prompt, never from argv."
                         % SECRET_ENV)
    ap.add_argument("--out", help="file to write the inventory to")
    ap.add_argument("--format", choices=["json", "csv"], default="json",
                    help="inventory format (default json)")
    ap.add_argument("--num", type=int, default=SEARCH_MAX_NUM,
                    help="rows per page to ask for. Clamped to %d, which is "
                         "all the server will give (default %d)."
                         % (SEARCH_MAX_NUM, SEARCH_MAX_NUM))
    ap.add_argument("--max-partitions", dest="max_partitions", type=int,
                    default=MAX_PARTITIONS,
                    help="give up after this many partitions rather than "
                         "bisecting for ever (default %d)" % MAX_PARTITIONS)
    ap.add_argument("--insecure", action="store_true",
                    help="skip TLS certificate verification, for an Enterprise "
                         "portal behind an internal CA. Refused together with "
                         "--username.")
    ap.add_argument("--apply", action="store_true",
                    help="write the inventory file. Without this the org is "
                         "counted and reported, and nothing is written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def _authenticate(args):
    """Return a token, or None for an anonymous read."""
    token = args.token or os.environ.get(TOKEN_ENV)
    if token:
        return token
    if not args.username:
        return None
    secret = read_secret(args.username)
    return generate_token(args.url, args.username, secret, args.insecure)


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    if not args.url:
        print("error: --url is required. Use --self-test to verify the tool "
              "without a portal.", file=sys.stderr)
        return EXIT_USAGE
    if has_userinfo(args.url):
        print("error: --url must not hold a user name or password (an \"@\" "
              "was found). Nothing is printed back, because the password would "
              "reach the log. Use --username and %s, or --token."
              % SECRET_ENV, file=sys.stderr)
        return EXIT_USAGE
    if has_query_or_fragment(args.url):
        print("error: --url must be the portal address alone, with no \"?\" "
              "query string and no \"#\" fragment. Nothing is printed back, "
              "because a token or password there would reach the log. Use "
              "--token or %s." % TOKEN_ENV, file=sys.stderr)
        return EXIT_USAGE
    if not is_http_url(args.url):
        print("error: --url must start with https:// or http://. The url is "
              "not printed back, because urllib quotes a url it cannot open "
              "into its own error and a token may be in it.", file=sys.stderr)
        return EXIT_USAGE
    if args.apply and not args.out:
        print("error: --apply needs --out, the file to write the inventory to.",
              file=sys.stderr)
        return EXIT_USAGE
    if args.insecure and args.username:
        print("error: --insecure with --username would post your password down "
              "an unverified connection. Pass --token instead.", file=sys.stderr)
        return EXIT_USAGE
    if args.num < 1:
        print("error: --num must be at least 1.", file=sys.stderr)
        return EXIT_USAGE
    if args.max_partitions < 1:
        print("error: --max-partitions must be at least 1.", file=sys.stderr)
        return EXIT_USAGE
    has_credential = bool(args.token or os.environ.get(TOKEN_ENV)
                          or args.username)
    if args.anonymous and has_credential:
        print("error: --anonymous with a credential (--token, %s or "
              "--username) says two things. Drop one." % TOKEN_ENV,
              file=sys.stderr)
        return EXIT_USAGE
    if not args.anonymous and not has_credential:
        print("error: no credential was given. An anonymous census sees only "
              "public items and its total is not the organization's. Pass "
              "--token, export %s, or pass --username. To count public items "
              "on purpose, pass --anonymous." % TOKEN_ENV, file=sys.stderr)
        return EXIT_USAGE
    if args.anonymous and args.apply and args.format == "csv":
        print("error: a csv has no column to say the run was anonymous, so it "
              "would pass for a whole-organization inventory. Use --format "
              "json, which records \"authenticated\": false.",
              file=sys.stderr)
        return EXIT_USAGE

    try:
        token = _authenticate(args)
        me = {}
        if token is not None or not args.query:
            me = portal_self(args.url, token, args.insecure)
        role = account_role(me) if token is not None else None
        if (token is not None and role != ADMIN_ROLE and args.apply
                and args.format == "csv"):
            print("error: a csv has no column to say the signed-in account is "
                  "not an organization administrator (its role is %s), so it "
                  "would pass for a whole-organization inventory. Use --format "
                  "json, which records the role."
                  % (role if role else "unreadable"), file=sys.stderr)
            return EXIT_USAGE
        query = args.query
        if not query:
            oid = me.get("id")
            if not oid:
                raise RuntimeError("could not read the org id, pass --query")
            query = "orgid:%s" % oid
        print("reading %s" % args.url)
        print("query: %s" % query)
        # The user list only ever makes a bisect finer. An org that will not
        # hand it over costs partitions, never items, because the other half of
        # every owner split is "not these owners".
        try:
            owners = org_owners(args.url, token, args.insecure)
        except RuntimeError as exc:
            owners = ()
            print("note: the user list could not be read (%s), so the owner "
                  "bisect is off. Every item is still counted, and a day "
                  "that sits on the ceiling is refused instead of split."
                  % exc, file=sys.stderr)
        if owners:
            print("%d owner(s) available for the owner bisect" % len(owners))
        result = census(portal_fetch(args.url, token, args.insecure),
                        base=query, owners=owners, now_ms=None, num=args.num,
                        max_partitions=args.max_partitions,
                        echo=lambda line: print(line))
    except EOFError:
        # getpass found no console to ask on. A scheduled job gets here. It
        # was a traceback and exit 1, which means "did not reconcile".
        print("error: no password was given and there is no console to ask "
              "on. Export %s before an unattended run." % SECRET_ENV,
              file=sys.stderr)
        return 2
    except CensusIncomplete as exc:
        print("")
        print("error: the census could not be completed, so no total is given.",
              file=sys.stderr)
        print("  %s" % exc, file=sys.stderr)
        return 2
    except (RuntimeError, ValueError) as exc:
        # ValueError is how the pure crawl rejects a page it cannot trust: a
        # result with no id, a nextStart that does not advance. Those come from
        # the portal, not from a bug here, so they are an exit code and a
        # message rather than a traceback.
        print("error: %s" % exc, file=sys.stderr)
        return 2

    if token is not None and result.total == 0:
        # An empty org and a token that sees none of it look the same from here.
        # Exit 0 would pass the second for the first.
        print("")
        print("error: the census found 0 items with a token. Either the "
              "organization is empty or this token cannot see it (a wrong "
              "--query, or an account with no access). The tool cannot tell "
              "which, so no inventory is written.", file=sys.stderr)
        return 2

    print("")
    encoding = getattr(sys.stdout, "encoding", None)
    for line in describe(result, anonymous=token is None, role=role):
        print(printable(line, encoding))

    if args.out:
        document = build_inventory(args.url, query,
                                   utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                                   result, token is not None, role)
        if not args.apply:
            print("")
            print("Check only. No inventory was written. Re-run with --apply.")
        elif args.format == "csv" and result.warnings:
            print("")
            print("error: the census did not reconcile, and a csv has no column "
                  "to say so, so it would pass for a complete inventory. No csv "
                  "was written. Run again, or use --format json, which records "
                  "\"reconciled\": false.", file=sys.stderr)
        else:
            writer = write_csv if args.format == "csv" else write_json
            print("")
            try:
                written = writer(document, args.out)
            except (OSError, UnicodeError) as exc:
                # A locked file, a read-only folder, a directory named as the
                # file, a full disk, a title that is not encodable. The census
                # above is complete, so this is not exit 1 or 2.
                print(printable("error: could not write %s: %s. The census "
                                "above finished; fix the path and run again."
                                % (args.out, exc),
                                getattr(sys.stderr, "encoding", None)),
                      file=sys.stderr)
                return EXIT_WRITE_FAILED
            # Outside the try: a console that cannot encode the path must not
            # turn a finished write into "could not write" and exit 73.
            print(printable("wrote %s" % written, encoding))

    return exit_code(result, anonymous=token is None, role=role)


if __name__ == "__main__":
    sys.exit(main())
