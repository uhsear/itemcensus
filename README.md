# itemcensus

Inventory every item in an ArcGIS organization, and refuse to report a total the server cannot
prove is complete. Read-only, never changes anything in the org.

You are migrating an organization, or costing one, or decommissioning one, and the first thing
anybody asks is how many items are in it. So you run a census. It says 10,000.

Esri's REST reference says it plainly about `/sharing/rest/search`. The `total` "is counted
accurately up to 10,000; if the total number is greater than this value, 10,000 will be
returned", and `num` has a "maximum allowed value" of 100. A script that pages `search` and trusts the total inherits that ceiling and reports
success at it. Your census is wrong by an unknown amount, and the thing that makes it dangerous
is that a true total of 10,000 and a truncated one are identical on the screen. Nothing is
missing, nothing errors, nothing warns. You find out after the migration, when somebody goes
looking for a layer that was never in the list.

A real run prints 487 lines, 483 of them assertions. Here are the first fourteen and the last
nine, copied unchanged from one run, with nothing elided inside either excerpt.

```
$ python itemcensus.py --self-test
itemcensus self-test: no portal, no internet, no credentials
--------------------------------------------------------------------
PASS  a page size of 100 is passed through
PASS  a page size of 1000 is clamped to 100 before it is sent, because the server clamps it silently  <-- pinned defect
PASS  one over the cap clamps to the cap
PASS  a page size of 1 is honoured
PASS  a page size under the cap is left alone
PASS  a page size of zero raises
PASS  a negative page size raises
PASS  a page size that is a string raises
PASS  a page size of True raises, rather than paging one row at a time
PASS  exactly 10,000 is the CAP and never a total  <-- pinned defect
PASS  9,999 is under the cap and is a real total
PASS  anything above the cap is capped
```

```
PASS  the harness records a false check, a missing exception, two wrong exceptions, an argv argparse accepted, a census that did not refuse, a census that raised the wrong thing, a portal call that did not fail and one that refused instead as nine failures, so a broken tool turns this self-test red  <-- pinned defect
PASS  not one of those nine was counted as a pass
PASS  every recorded failure prints a FAIL line the operator can see
PASS  a portal call that did not fail the right way yields no message to assert on
PASS  a tool with one broken function turns the whole self-test red: exit 1, and the summary names the assertion that failed
PASS  run as a script, it exits with main()'s code: 64 for no --url
PASS  imported as a module, it defines main() and runs nothing
--------------------------------------------------------------------
483 assertions, 0 failed
```

## Requirements

Python 3.9 or newer. Standard library only: `urllib.request`, `urllib.parse`,
`urllib.error`, `json`, `csv`, `os`, `sys`, `ssl`, `io`, `argparse`, `datetime`, `getpass`,
`time`, and `re`, `tempfile`, `shutil`, `http.server`, `threading` and `runpy` inside the
self-test. It runs on ArcGIS Pro's Python and on a plain `python3`. `arcpy` is not used and the
`arcgis` package is not needed.

```
git clone https://github.com/uhsear/itemcensus.git
python itemcensus.py --self-test
```

`--self-test` needs no portal, no internet and no credentials, so you can check the tool before
you point it at an organization. It covers the portal calls as well as the decisions: paging,
sign-in, the 10,000 ceiling, all three bisect levels, a token that dies mid-crawl, an HTTP error,
a 200 response carrying an ArcGIS error envelope, and the command line end to end, against a
stand-in portal that answers inside the process. One section also starts a stub portal on
`127.0.0.1` with `http.server` and runs the command line against it over a real socket: a
`generateToken` POST, search GETs, an HTTP 500 and a port nothing listens on. That section
needs the loopback interface and nothing else. It sets `no_proxy` to `*` and points `HTTP_PROXY`
and `HTTPS_PROXY` at a dead port for the length of the run, so a proxy in your environment
cannot capture the loopback requests, and restores them afterwards.

## Quick start

```
python itemcensus.py --url https://portal.example.com --username gis_admin
```

That counts the organization and prints the total and the partitions it needed. Nothing is
written until you ask for a file. A run with no credential at all is refused with exit 64
unless you also pass `--anonymous`, because an anonymous census sees only public items and its
total is not the organization's.

```
python itemcensus.py --url https://portal.example.com --username gis_admin \
    --out inventory.csv --format csv --apply
```

## Usage

| Flag | Default | What it does |
|---|---|---|
| `--url` | none | Portal url. Required. Must start with `https://` or `http://`. A url that holds an `@`, a `?` or a `#` is refused with exit 64, and no url is printed back, because the part after one of those may be a password or a token. The url is printed and written into the inventory as typed, so pass the token with `--token` or `ITEMCENSUS_TOKEN`. |
| `--query` | the signed-in org | Search query to census. Everything else is ANDed onto it. |
| `--token` | none | An existing portal token. Prefer the `ITEMCENSUS_TOKEN` environment variable: a value on `argv` is readable by every process on the machine. |
| `--anonymous` | off | Census with no credential, on purpose. The search sees only public items. Required when there is no `--token`, `ITEMCENSUS_TOKEN` or `--username`, and refused together with any of them. With `--apply` it needs `--format json`, which records `"authenticated": false`. |
| `--username` | none | Sign in as this user. The password comes from `ITEMCENSUS_PASSWORD` or an unechoed prompt. |
| `--out` | none | File to write the inventory to. |
| `--format` | `json` | `json` or `csv`. |
| `--num` | `100` | Rows per page to ask for. Clamped to 100, which is all the server gives. |
| `--max-partitions` | `4096` | Give up after this many partitions rather than bisecting for ever. |
| `--insecure` | off | Skip TLS verification, for an Enterprise portal behind an internal CA. Refused together with `--username`. |
| `--apply` | off | Write the inventory file. Without it nothing is written. |
| `--self-test` | off | Run the offline assertions and exit. |

Exit codes:

| Code | Meaning |
|---|---|
| 0 | A complete census. |
| 1 | The total is a floor. Either the census did **not** reconcile, or the token's role is not `org_admin` (or the portal named none), so items it cannot see are missing. With `--format csv --apply` no csv is written for either. A role exit still writes the JSON file, which records `"role"`. An `--anonymous` run is the opted-in exception and exits 0. |
| 2 | The census could not be completed. A token that finds 0 items also exits 2 and writes no inventory, because the tool cannot tell an empty organization from a token that sees none of it. |
| 64 | Usage error. A mistyped or unknown flag, a `--num` that is not a number, a bad `--format`, a `--url` with an `@`, `?` or `#` in it, no credential and no `--anonymous`, and `--anonymous` with a credential all exit 64. So does `--format csv --apply` when the signed-in role is not `org_admin`. |
| 73 | The census finished and was printed, but the inventory file could not be written. |

The password is never a flag. `argv` is readable by every process on the machine, it lands in
shell history and in scheduler logs, and `--self-test` asserts that no `--password` attribute
exists at all. An unknown word on the command line, such as a mistyped `--password hunter2` or
`--tokn VALUE`, exits 64. The message counts the unknown words and names only the ones shaped like
a flag, cut at any `=`. It never prints a value, because argparse's own message would put a
credential into the scheduler log.

A token is different only in that `--token` exists. It has the same exposure as a password flag:
the value is in the process list, in shell history and in process-creation audit logs. Export
`ITEMCENSUS_TOKEN` instead. `--token` wins when both are set.

## What it actually does

Read the whole organization with one query and you get the first 10,000 items and a total of
10,000. So the organization is read as a tree of partitions instead.

1. Page one partition with `start` and `num`, `num` clamped to 100 before the request goes out,
   following the server's `nextStart` and stopping on `-1`.
2. Read the reported total on **every** page, not only the first, and compare it with the rows
   that actually came back.
3. If that total is 10,000 or more, the partition proves nothing. Throw away what it returned and
   **bisect** it.
4. Otherwise the partition is proven: the total is under the ceiling and the rows match it. Union
   its items into the inventory by item id.
5. The root partition ends at this machine's clock reading. A final tail partition runs from one
   millisecond after that reading to the year 9999, so an item created while the census ran, or
   on a portal whose clock is ahead of this one, is counted. Any item found there makes the run
   exit 1 and say so, because the organization changed under it.

The bisect goes created date, then owner, then item type. This is a real run of the tool
against a stub portal on `127.0.0.1` that holds 10,400 synthetic items and, like the real
search, reports 10,000 for any query that matches more (`python itemcensus.py --anonymous --url
http://127.0.0.1:18941 --query orgid:ORG`). The stub is not in the repository. The numbers in the
date ranges depend on the clock when the run started:

```
reading http://127.0.0.1:18941
query: orgid:ORG
  at the 10000 ceiling, splitting: (orgid:ORG) AND created:[0 TO 1794449158396]
  0 item(s) from (orgid:ORG) AND created:[0 TO 897224579198]
  at the 10000 ceiling, splitting: (orgid:ORG) AND created:[897224579199 TO 1794449158396]
  0 item(s) from (orgid:ORG) AND created:[897224579199 TO 1345836868797]
  at the 10000 ceiling, splitting: (orgid:ORG) AND created:[1345836868798 TO 1794449158396]
  0 item(s) from (orgid:ORG) AND created:[1345836868798 TO 1570143013597]
  at the 10000 ceiling, splitting: (orgid:ORG) AND created:[1570143013598 TO 1794449158396]
  at the 10000 ceiling, splitting: (orgid:ORG) AND created:[1570143013598 TO 1682296085997]
  at the 10000 ceiling, splitting: (orgid:ORG) AND created:[1570143013598 TO 1626219549797]
  5652 item(s) from (orgid:ORG) AND created:[1570143013598 TO 1598181281697]
  4748 item(s) from (orgid:ORG) AND created:[1598181281698 TO 1626219549797]
  0 item(s) from (orgid:ORG) AND created:[1626219549798 TO 1682296085997]
  0 item(s) from (orgid:ORG) AND created:[1682296085998 TO 1794449158396]
  0 item(s) from (orgid:ORG) AND created:[1794449158397 TO 253402300799999]

10400 item(s), read in 8 partition(s) over 711 page(s), 6 bisection(s)
Every partition reconciled: the reported total matched the rows returned,
on every page, and no partition sat on the 10000 ceiling.

NOTE: no token was used, so only public items were visible. This total is not
the organization's, and exit 0 does not mean every item was seen.
```

The two lines after the summary are what every `--anonymous` run prints. The `0 item(s)`
partitions are the date ranges before the stub's first item and after its last, and the final
one is the tail from step 5. Every owner and item type split is exercised by the
self-test, not by this run: the stub is anonymous, so no owner list is read.

Every split is a **complement pair**. Splitting owners into "these four" and "those four" is only
exhaustive while the user list is complete, and a user list is never complete for long. Splitting
into `owner:(ann)` and `-owner:(ann)` is exhaustive by construction, so an owner hired after the
list was read is still counted. The same holds for item type, which is why the type list in the
source does not have to be exhaustive. The date halves meet at one millisecond, not at one
shared boundary, so no item lands in both.

Results are unioned by item id rather than added up, so a partition that overlaps another one
cannot inflate the total. That matters because an item edited during the crawl can be returned
by both halves of a split it straddles.

## Why the obvious version is wrong

The ArcGIS API for Python is the obvious tool and it is good at what it does.
`gis.content.search` takes a `max_items`, and `advanced_search` takes a `max_items` documented as
up to 10,000, a `start` and a `return_count` (both are in the ArcGIS API for Python reference
listed under References). Both are far less code than this. What they do not give you is a statement that what came back was everything.
That is the whole of what this tool adds, and it is not paging. It is proof: it compares the
server's own count with the rows read, partition by partition, and refuses when they cannot
agree. This README makes no claim about how those two methods behave past 10,000 items, because
that was not tested here.

The handwritten version has its own three bugs, and the self-test pins all of them:

```python
num = 1000                                   # the server returns 100 and says nothing
start = 1
while start < total:
    page = search(q, start=start, num=num)
    total = page["total"]                    # 10,000, whatever the org holds
    items.extend(page["results"])
    start += num                             # skips rows 101 to 1000 of every page
```

`num` is clamped server-side, so advancing by your own `num` walks past nine tenths of the
organization. `total` is capped, so the loop exits at 10,000 believing it finished. And the loop
condition reads a total that the first page set, so a total that moved underneath it is silently
overwritten rather than reported.

The subtle one is the refusal. When a partition is still at the ceiling after bisecting down to a
single day, a single owner and a single item type, there is nothing honest left to do, and the
tempting move is to return what was read:

```python
if total >= 10000:
    log.warning("partition truncated, reporting what we have")   # wrong
    return items
```

That is the original bug with a log line in front of it. The caller still gets a number, still
prints it, and the warning scrolls off. This tool raises instead. A caller that wanted a number
and got an exception knows something; a caller that wanted a number and got 10,000 knows nothing.

A total that moves mid-crawl is different, and gets different treatment. Items really are created
and deleted while a census runs, and that is not a reason to refuse. It is a reason to say so:
the run exits 1, the warning names the first and last total the server gave, and a JSON inventory
file records `"reconciled": false`. A csv has no such column, so `--format csv --apply` writes no
file for that census and says so on stderr. The exit code stays 1.

## Output

`--format json` writes the partition list next to the items, so the file carries its own proof.
It also records `"authenticated"`: `false` for an `--anonymous` run, so a file that was mailed
away from its console still says the census saw public items only.
This is the real output of `python itemcensus.py --anonymous --url http://127.0.0.1:18940
--query orgid:ORG --out inv.json --apply` against a stub portal holding three synthetic items, one of them with a
hostile title:

```
reading http://127.0.0.1:18940
query: orgid:ORG
  3 item(s) from (orgid:ORG) AND created:[0 TO 1794449197051]
  0 item(s) from (orgid:ORG) AND created:[1794449197052 TO 253402300799999]

3 item(s), read in 2 partition(s) over 2 page(s), 0 bisection(s)
Every partition reconciled: the reported total matched the rows returned,
on every page, and no partition sat on the 10000 ceiling.

NOTE: no token was used, so only public items were visible. This total is not
the organization's, and exit 0 does not mean every item was seen.

wrote inv.json
```

and the file it wrote:

```json
{
 "authenticated": false,
 "bisections": 0,
 "itemcensus": 1,
 "items": [
  {
   "access": "org",
   "created": 1577836800000,
   "id": "item000000",
   "modified": 1577836800005,
   "numViews": 0,
   "owner": "bob",
   "size": 1024,
   "title": "Parcels 0",
   "type": "Feature Service"
  },
  {
   "access": "org",
   "created": 1577840400000,
   "id": "item000001",
   "modified": 1577840400005,
   "numViews": 1,
   "owner": "ann",
   "size": -1,
   "title": "=HYPERLINK(\"http://x.invalid\")",
   "type": "Feature Service"
  },
  {
   "access": "org",
   "created": 1577844000000,
   "id": "item000002",
   "modified": 1577844000005,
   "numViews": 2,
   "owner": "bob",
   "size": 1026,
   "title": "Parcels 2",
   "type": "Feature Service"
  }
 ],
 "pages": 2,
 "partitions": [
  {
   "query": "(orgid:ORG) AND created:[0 TO 1794449197051]",
   "reported": 3,
   "returned": 3
  },
  {
   "query": "(orgid:ORG) AND created:[1794449197052 TO 253402300799999]",
   "reported": 0,
   "returned": 0
  }
 ],
 "query": "orgid:ORG",
 "reconciled": true,
 "role": null,
 "taken": "2026-11-12T02:06:37Z",
 "total": 3,
 "url": "http://127.0.0.1:18940",
 "warnings": []
}
```

`--format csv` writes those nine columns and nothing else. It has no column for
`authenticated`, so `--anonymous --apply --format csv` is refused with exit 64. For the same reason
it is refused with exit 64 when the signed-in role is not `org_admin`, and it is not written for a
census that did not reconcile. `role` is `null` in the sample above because that run was
anonymous. Items are sorted by id in both. The
CSV of two censuses of an unchanged organization is the same bytes, so `diff` is useful on it.
The JSON is not: it carries `taken` and the clock reading inside each partition query, so two
JSON files differ on those lines even when the items match.

### CSV and spreadsheet formulas

A title, an owner and an id come from the portal, and anyone who can name an item chooses what
lands in the file. An inventory is the kind of file somebody opens in a spreadsheet. So in
`--format csv`, a text cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return gets
one apostrophe in front of it. OWASP's CSV Injection page lists those leading characters and
lists the single quote prefix as one of several mitigations. Every text column is guarded: `id`,
`title`, `owner`, `type` and `access`. The four number columns (`created`, `modified`, `size`,
`numViews`) are never touched, so a `size` of `-1` stays the number `-1`. The `size` values in
the samples above come from the stub, which invents them. Esri's Search page says "size is always
returned as -1" in a search result, so a real portal gives `-1` in every row of that column. It is
a fixed value there, not a marker for an unknown size, and the tool cannot measure item size. JSON is not a spreadsheet format, so it carries the same nine fields unguarded. It is not the portal's answer verbatim: it drops every other field (the item `url`, the description), casts the number fields to integers, and writes `private` for a missing `access`.

The real `--format csv` output from the same stub, run with `--token` (`inv.csv`). The hostile title in the second row
is the one written as `=HYPERLINK(...)` in the JSON above:

```
id,title,owner,type,created,modified,access,size,numViews
item000000,Parcels 0,bob,Feature Service,1577836800000,1577836800005,org,1024,0
item000001,"'=HYPERLINK(""http://x.invalid"")",ann,Feature Service,1577840400000,1577840400005,org,-1,1
item000002,Parcels 2,bob,Feature Service,1577844000000,1577844000005,org,1026,2
```

This tool never reads its own CSV back. Whatever does read it must strip exactly one leading
apostrophe from a cell whose next character is one of those six. The self-test pins that round
trip for every text column and every leading character, tab and carriage return included.

The service url is dropped on the way in. A real `search` response carries one, and on a secured
service it carries a token in its query string, which would otherwise sit in an inventory file
for years. `--self-test` asserts that no credential reaches stdout or the file.

## What it will not do

- No item details, no data, no thumbnails. This counts and lists. Restoring or snapshotting an item's
  configuration, which `itemvault` is for, is a different problem.
- No deletes, no re-shares, no transfers. There is no flag that changes the organization.
- No `total` you did not ask for. The number printed is the count of distinct item ids actually
  read, never a number the server reported.
- No usage or cost figures. `size` and `numViews` are copied from the search result as they were
  reported. Esri documents `size` as always `-1` in a search result, so that column carries no
  information from a real portal.
- No group membership. An item's groups need a call per group, which is `sharewatch`'s shape,
  not this one's.
- It cannot see items it is not allowed to see. An anonymous census counts public items, and a
  census run as a non-administrator is limited to what that user can search. The tool reads
  `user.role` from `portals/self` whenever a token is present. When the role is not `org_admin`,
  or the reply names none, the run prints a NOTE, exits 1, records `"role"` in the JSON file and
  refuses `--format csv --apply` with exit 64. Esri says a custom role with administrative
  privileges reports `org_admin`. This was run against a local stub only, never a real portal.
  The number is honest about the query, not about the organization.
- More than 10,000 items created by one user, of one type, in one day cannot be counted by this
  method, because the search API offers nothing narrower to split on. That is the case it
  refuses, and the refusal names the exact query it gave up on.

## Limits

- The CSV guard covers the six leading characters `=`, `+`, `-`, `@`, tab and carriage return. It
  does not touch a leading line feed, which OWASP's CSV Injection page also lists.
- The CSV guard does not touch the full-width forms of `=`, `+`, `-` and `@`. OWASP says some
  locales, Japanese for example, read them as formulas, so a title that starts with a full-width
  equals sign is written as it came.
- A title that already starts with an apostrophe followed by a trigger character, such as `'=x`,
  is written unchanged. A reader that strips one apostrophe cannot tell it from a guarded cell
  and turns it into `=x`. A self-test assertion marked "known limit" pins that.
- Nothing here opened Excel or any other spreadsheet. The assertions check the bytes in the file.
  OWASP warns the quote prefix is unreliable after Excel saves and reopens a CSV, so treat the
  file as an inventory to read, and as input to a script, not as a workbook to resave. The
  OWASP page lists the quote prefix as one option among several and does not call any of them
  reliable once Excel has resaved the file.
- The CSV is written as UTF-8 without a byte order mark. Excel opened by double-click may show a
  non-ASCII title garbled. Import the file as UTF-8 instead.
- The tail partition ends at 9999-12-31 (`253402300799999` ms). That value is accepted by the
  stub portals used here. It was not run against a real ArcGIS portal, so if a portal rejects
  it the census exits 2 with the portal's message rather than returning a total.
- The numeric fields (`created`, `modified`, `size`, `numViews`) are assumed to be numbers. A
  portal that sends text there, for example `"created": "abc"`, stops a run that has `--out`
  with a Python traceback and exit 1, which this README does not define as "did not reconcile".
  The check-only run stops too, because the inventory is built before `--apply` is looked at.
  Other wrong JSON shapes inside an otherwise valid object, such as a users row that is not an
  object, end the same way. A `total` of `1e999` is rejected as "not a search response" and
  exits 2.
- A search answer with no integer `total` or no `results` list, such as `{}` or a maintenance
  notice, aborts the census with exit 2 and the keys it had. It is never read as an empty
  organization.
- A search result with no `id` aborts the census with exit 2 and a message that lists the
  fields the row had, not the row. A `fetch` that returns something other than a search
  response is reported by type only.
- The census reads `portals/self/users` whenever a token is present, even when no owner split
  turns out to be needed. That is one more request. If the portal refuses the list, the census
  prints a note and runs without the owner split, so a day that sits on the ceiling is refused
  instead of split. The list is not de-duplicated and is paged until `nextStart` ends or passes
  10,000, so a portal that ignores `start` there makes the owner list very long.
- An item created but not yet indexed by the portal's search is in neither the reported total
  nor the rows. Exit 0 means the search index agreed with itself, not that every item was
  indexed.
- Exit 1 for a role other than `org_admin` is a verdict on the account, not on the search. The
  JSON file from such a run still says `"reconciled": true`, because every partition agreed with
  itself. Read `"role"` beside it. A scheduled job that wants only that user's view must expect
  exit 1.
- The leading-minus form (`-owner:(...)`, `-type:(...)`) that every complement split relies on
  ran only against a synthetic query evaluator. Esri's search reference page shows `-` as an
  infix operator and the leading form was not run against a real portal. The same holds for
  `sortField=created` and the end-of-results `nextStart` of `-1`.
- A write failure after a census that did not reconcile exits 73, not 1. Read the printed
  warnings, not only the exit code.
- With `--username` and no `ITEMCENSUS_PASSWORD` the tool prompts for the password. On Windows
  the prompt reads the console, not stdin, so a run that has a console waits even with stdin
  redirected. An unattended job must export the variable. A run with no console at all (a
  process started with `DETACHED_PROCESS`, as a scheduler can) exits 2 and names the variable.
  That was run for real with a detached child process. The self-test asserts it only with a
  stand-in for the console check, so it does not test the detached launch itself. The tool checks for a console
  first, because the Windows prompt reads the console device and a process with none gets no answer. This
  was observed, not taken from a documentation page.
- The tool uses the proxy named in the environment, as urllib's default opener does, and
  follows HTTP redirects. No assertion covers either. A proxy that logs request urls sees the
  token. The tool does not copy the token into a redirect on its own, but a `Location` that
  carries the same query string hands the token to the target.
- An `--out` path the console cannot encode is written in full. The `wrote` line shows `?`
  for the characters it cannot print, and the exit code is the census code. The self-test
  asserts this against a console that raises on an unencodable character.
- An `--anonymous` run prints a note that only public items were visible and still exits 0,
  because the search agreed with itself and you asked for it. Exit 0 there does not mean the
  whole organization was seen. The JSON file says so with `"authenticated": false`.
- A token that can see nothing, such as a new non-administrator account or a mistyped `orgid`,
  gives a census of zero items. The tool cannot tell that from an empty organization, so it
  exits 2, prints why and writes no inventory. A truly empty organization is refused the same
  way. The refusal applies only with a credential: an `--anonymous` census of zero items is
  still printed, with its note.
- A token sent to an `http://` url travels in clear text in the query string. Use `https://`.
- A text cell that starts with a space and then a trigger character (` =1`) is not guarded.
  The CSV guard also assumes the text fields arrive as JSON strings, so a number sent as a
  title is written as it is. Real portals send strings.
- A missing or null `size`, `numViews`, `created` or `modified` is written as `0`, and a float
  `size` is cut to an integer. A missing `access` is written as `private`. "as they were reported" holds for the numbers a portal sends.
- An item whose `created` is negative falls outside every partition and is dropped without a
  warning. The partitions start at 0 and no real item is older.
- An owner or type name is quoted only when it has a space, colon or parenthesis. A backslash
  (a domain login) or a wildcard reaches the search query unescaped, and Esri's escaping rules
  for those were not checked. The self-test pins the unescaped form against a synthetic
  evaluator.
- The first owner split puts half of the owner list into one query. No test measures query
  length, so a proxy or web server with a short query-string limit would answer with an HTTP
  error and the run would exit 2, not print a total.
- A token travels in the query string of each search request, which is where Esri's search
  endpoint expects it. Anything that logs request urls on the portal or on a proxy will see it.
- `--url http://...` together with `--username` posts the password over cleartext HTTP. Only
  `--insecure` is refused with `--username`. Use an `https://` url when you sign in.
- A `--url` that holds an `@`, a `?` or a `#` is refused before anything is printed, and the
  refusal does not echo it. The test is the character alone, so a portal url that really
  contains one cannot be used. A token in a `--url` is not tested by name: only these characters
  are.
- A numeric field that arrives as text `int()` cannot read, such as `"abc"` or the decimal
  string `"12.0"`, ends the run with a traceback and exit 1, the same as the `"abc"` case above.
- The `--query` and the progress lines are not passed through the console-encoding guard. A
  query with a character the console code page cannot encode, such as a Polish l-stroke on
  cp1252, exits 2 with an encoding error. Set `PYTHONIOENCODING=utf-8`.
- The inventory is written by opening the file for writing, so it is truncated first. A crash or
  a full disk part-way through leaves a truncated file. Write to a new file name for each run.
  A write that fails exits 73 with a message. In `--format csv`, a title or owner that is not
  encodable as UTF-8, such as a lone surrogate, also exits 73, and the file can be left
  part-written. The JSON writer escapes such a character and does not fail.
- `--apply` replaces the file named by `--out` without asking.
- Tokens are requested with `client=referer`, and later requests send no `Referer` header.
  Whether a real portal accepts that was not tested.
- The sentence that a real search result carries a service url, and on a secured service a token
  in its query string, is a statement about the response shape. This README does not cite Esri
  for it, and it was not checked against a real portal.
- When a day sits on the ceiling and no owner list was read, the refusal says the day holds
  one owner and one item type. The tool did not establish that, so read it as "no finer split
  was available". The exit code 2 and the missing total are right.
- The stub that produced the 10,400-item figures above is not in the repository, so a reader
  cannot reproduce those partition and page counts exactly.
- `--insecure` is covered only by a stand-in that records the flag, because the stub portal is
  plain `http://127.0.0.1`. The password prompt (`getpass`) is never run from a terminal.
- The self-test binds a loopback `http.server` on `127.0.0.1`. It fails on a host that has no
  loopback interface or forbids local sockets.
- The examples and the self-test fixtures use the placeholder url `https://portal.example.com`
  and the user `gis_admin`. They are invented and name no real organization.
- A portal error whose `error` value is a bare string is reported with that string. Portal
  behavior not listed here was not checked against a real portal: every network path ran
  against a local stub.

- A value that argparse rejects for `--num` or `--format` is printed back in the usage error, so
  never put a credential in those flags.
- A token from `--token` or `ITEMCENSUS_TOKEN` wins over `--username`, and `--username` is then
  ignored without a note.
- Redaction replaces the secret wherever its text appears, so a very short test token garbles
  the error message it is removed from.
- An item with no `created` or `modified` value is recorded as `0`, which reads as a 1970 date,
  and one with no `access` is recorded as private, both without a warning. Real search results
  carry these fields.
- A partition that sits on the ceiling is read through all 100 pages before it is split, so an
  organization of 10,400 items costs about 700 requests. That is a cost, not a count error.
- Owner names that hold a hyphen, a period or one of the words AND, OR, NOT and TO are not
  quoted in the owner bisect, which relies on the search parser reading them as one term. That
  was not checked against Esri's parser. It matters only when one owner has more than 10,000
  items in one day.
- When the `portals/self/users` reply has no `users` key, the owner list is empty and the owner
  bisect is off without a note. That costs partitions, not items. A refused list prints a note.
- A `--token` value that starts with `-`, which a URL-safe token can, is read by argparse as a
  flag and exits 64. Pass `--token=VALUE` or use `ITEMCENSUS_TOKEN`.
- Text from the portal, such as an error message or an owner name in the progress lines, is
  printed as received, so a hostile portal can put control or escape characters on the console.
  The role in the NOTE is the exception: it is printed only when it is a plain word.
- The Esri pages cited here cover `search`, `search-reference`, `portals/self` and `user`. The
  tool also relies on `generateToken`, on `portals/self/users` with `start` and `num`, and on
  `client=referer`. Those are not cited, and none was run against a real portal.
- OWASP's CSV Injection page now lists a tab prefix inside a quoted field as the mitigation that
  resists Excel, and says the single quote prefix is unreliable after Excel saves the file
  again. This tool uses the single quote prefix, which is the mitigation the self-test pins.

## Verification

Measured on 2026-11-12, all against the same file.

| Host | Python | Assertions |
|---|---|---|
| Windows | 3.13.2 | 483, 0 failed |
| Windows | 3.12.10 | 483, 0 failed |
| Windows | 3.10.20 | 483, 0 failed |
| Windows | 3.9.25 | 483, 0 failed |
| Windows, ArcGIS Pro conda environments (two of them) | 3.11.8 and 3.13.7 | 483, 0 failed |
| Linux (Ubuntu) | 3.12.3 | 483, 0 failed |

The count is the same on every host, and the Linux output is byte for byte the Windows output
once the carriage returns are stripped. Branch coverage of `itemcensus.py` is 100 percent
(`coverage run --branch itemcensus.py --self-test`, 1490 statements, 262 branches). The last
branches are reached by running the whole self-test once with one function broken, to prove the
summary goes red, and by running the file with `runpy` once as a script and once as a module.
`pyflakes` is clean and `python -W error` compiles it. Coverage ran on Windows only, because
`coverage` is not installed on the Linux host. The Linux host has only Python 3.12.3, so
earlier Linux runs on 3.9, 3.10 and 3.11 were not repeated for this count.

## References

- Esri, [Search](https://developers.arcgis.com/rest/users-groups-and-items/search/): `num`
  maximum 100, `start` is the 1-based first result, `total` is counted accurately up to 10,000,
  `nextStart` is "the next entry index if the current result set doesn't contain all results", and
  "the top 10,000 query results are available to retrieve through pagination". `sortField`
  accepts `created`, `sortOrder` defaults to ascending. The page does not say what `nextStart`
  is at the end. The tool ends the crawl on `-1`. When `nextStart` is missing it advances by the rows
  that came back and stops on a page with no rows. The self-test pins both.
- Esri, [Search reference](https://developers.arcgis.com/rest/users-groups-and-items/search-reference/):
  `created: [start TO end]` in epoch milliseconds with inclusive brackets, `orgid:`, `owner:`,
  and the `-` prohibit operator that every complement split relies on.
- Esri, [Portal Self](https://developers.arcgis.com/rest/users-groups-and-items/portal-self/):
  the `user` object "is returned only when a token is passed in", and its `role` is `org_admin`,
  `org_publisher` or `org_user`. Esri, [User](https://developers.arcgis.com/rest/users-groups-and-items/user/):
  each role value also covers a custom role with the matching privileges, and `roleId` holds the
  id of a custom one.
- OWASP, [CSV Injection](https://community.owasp.org/attacks/CSV_Injection): the leading
  characters, and the single quote prefix as one of several mitigations.
- Esri, [ArcGIS API for Python, arcgis.gis](https://developers.arcgis.com/python/latest/api-reference/arcgis.gis.toc.html):
  the `max_items`, `start` and `return_count` parameters of `ContentManager.search` and
  `advanced_search`, read on 2026-11-11.
- Microsoft, [Consoles](https://learn.microsoft.com/en-us/windows/console/console-handles):
  `CONIN$` opens the console input buffer even when stdin is redirected. The page does not say
  what happens with no console. Opening it failed in a detached child process here, and that
  is how the tool tells a no-console launch from one that can prompt.
- Python, [csv](https://docs.python.org/3/library/csv.html): open the file with `newline=''`,
  and the writer's default line terminator is `\r\n`.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [sharewatch](https://github.com/uhsear/sharewatch) - once you can enumerate the org, diff its sharing posture against yesterday
- [sightline](https://github.com/uhsear/sightline) - audit web maps for layers that the people viewing them cannot see
- [whobreaks](https://github.com/uhsear/whobreaks) - what references an item before you delete it
- [ghostsvc](https://github.com/uhsear/ghostsvc) - services the portal does not know it publishes
