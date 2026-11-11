# itemcensus

Inventory every item in an ArcGIS organization, and refuse to report a total the server cannot
prove is complete. Read-only, never changes anything in the org.

You are migrating an organization, or costing one, or decommissioning one, and the first thing
anybody asks is how many items are in it. So you run a census. It says 10,000.

Esri's REST reference says it plainly about `/sharing/rest/search`. The `total` "is counted
accurately up to 10,000; if the total number is greater than this value, 10,000 will be
returned", and `num` has a "maximum allowed value" of 100. Every inventory, backup and cleanup script in the wild inherits that ceiling and reports
success at it. Your census is wrong by an unknown amount, and the thing that makes it dangerous
is that a true total of 10,000 and a truncated one are identical on the screen. Nothing is
missing, nothing errors, nothing warns. You find out after the migration, when somebody goes
looking for a layer that was never in the list.

A real run prints 417 lines. Here are the first fourteen and the last nine, copied
unchanged from one run, with nothing elided inside either excerpt.

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
417 assertions, 0 failed
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
needs the loopback interface and nothing else.

## Quick start

```
python itemcensus.py --url https://county.maps.arcgis.com --username gis_admin
```

That counts the organization and prints the total and the partitions it needed. Nothing is
written until you ask for a file.

```
python itemcensus.py --url https://county.maps.arcgis.com --username gis_admin \
    --out inventory.csv --format csv --apply
```

## Usage

| Flag | Default | What it does |
|---|---|---|
| `--url` | none | Portal url. Required. Must start with `https://` or `http://`. |
| `--query` | the signed-in org | Search query to census. Everything else is ANDed onto it. |
| `--token` | none | An existing portal token. Prefer the `ITEMCENSUS_TOKEN` environment variable: a value on `argv` is readable by every process on the machine. |
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
| 1 | The census did **not** reconcile, so its total is a floor. |
| 2 | The census could not be completed. |
| 64 | Usage error. A mistyped or unknown flag, a `--num` that is not a number and a bad `--format` all exit 64. |
| 73 | The census finished and was printed, but the inventory file could not be written. |

The password is never a flag. `argv` is readable by every process on the machine, it lands in
shell history and in scheduler logs, and `--self-test` asserts that no `--password` attribute
exists at all.

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
search, reports 10,000 for any query that matches more (`python itemcensus.py --url
http://127.0.0.1:18941 --query orgid:ORG`). The stub is not in the repository. The numbers in the
date ranges depend on the clock when the run started:

```
reading http://127.0.0.1:18941
query: orgid:ORG
  at the 10000 ceiling, splitting: (orgid:ORG) AND created:[0 TO 1794407697075]
  0 item(s) from (orgid:ORG) AND created:[0 TO 897203848537]
  at the 10000 ceiling, splitting: (orgid:ORG) AND created:[897203848538 TO 1794407697075]
  0 item(s) from (orgid:ORG) AND created:[897203848538 TO 1345805772806]
  at the 10000 ceiling, splitting: (orgid:ORG) AND created:[1345805772807 TO 1794407697075]
  0 item(s) from (orgid:ORG) AND created:[1345805772807 TO 1570106734941]
  at the 10000 ceiling, splitting: (orgid:ORG) AND created:[1570106734942 TO 1794407697075]
  at the 10000 ceiling, splitting: (orgid:ORG) AND created:[1570106734942 TO 1682257216008]
  8953 item(s) from (orgid:ORG) AND created:[1570106734942 TO 1626181975475]
  1447 item(s) from (orgid:ORG) AND created:[1626181975476 TO 1682257216008]
  0 item(s) from (orgid:ORG) AND created:[1682257216009 TO 1794407697075]
  0 item(s) from (orgid:ORG) AND created:[1794407697076 TO 253402300799999]

10400 item(s), read in 7 partition(s) over 610 page(s), 5 bisection(s)
Every partition reconciled: the reported total matched the rows returned,
on every page, and no partition sat on the 10000 ceiling.
```

The `0 item(s)` partitions are the date ranges before the stub's first item and after its last,
and the final one is the tail from step 5. Every owner and item type split is exercised by the
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
`gis.content.search` takes a `max_items`, `advanced_search` pages for you, and both are far less
code than this. What they do not give you is a statement that what came back was everything.
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
the run exits 1, the warning names the first and last total the server gave, and the inventory
file records `"reconciled": false`.

## Output

`--format json` writes the partition list next to the items, so the file carries its own proof.
This is the real output of `python itemcensus.py --url http://127.0.0.1:18940 --query orgid:ORG
--out inv.json --apply` against a stub portal holding three synthetic items, one of them with a
hostile title:

```
reading http://127.0.0.1:18940
query: orgid:ORG
  3 item(s) from (orgid:ORG) AND created:[0 TO 1794407695547]
  0 item(s) from (orgid:ORG) AND created:[1794407695548 TO 253402300799999]

3 item(s), read in 2 partition(s) over 2 page(s), 0 bisection(s)
Every partition reconciled: the reported total matched the rows returned,
on every page, and no partition sat on the 10000 ceiling.

wrote inv.json
```

and the file it wrote:

```json
{
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
   "query": "(orgid:ORG) AND created:[0 TO 1794407695547]",
   "reported": 3,
   "returned": 3
  },
  {
   "query": "(orgid:ORG) AND created:[1794407695548 TO 253402300799999]",
   "reported": 0,
   "returned": 0
  }
 ],
 "query": "orgid:ORG",
 "reconciled": true,
 "taken": "2026-11-11T14:34:55Z",
 "total": 3,
 "url": "http://127.0.0.1:18940",
 "warnings": []
}
```

`--format csv` writes those nine columns and nothing else. Items are sorted by id in both, so two
censuses of an unchanged organization produce the same bytes and `diff` is useful.

### CSV and spreadsheet formulas

A title, an owner and an id come from the portal, and anyone who can name an item chooses what
lands in the file. The inventory is opened in Excel by the person who asked how many items
there are. So in `--format csv`, a text cell that starts with `=`, `+`, `-`, `@`, a tab or a
carriage return gets one apostrophe in front of it. OWASP's CSV Injection page lists those
leading characters and recommends the single quote prefix. Every text column is guarded: `id`,
`title`, `owner`, `type` and `access`. The four number columns (`created`, `modified`, `size`,
`numViews`) are never touched, so the `-1` the portal sends for an unknown size stays the number
`-1`. JSON is not a spreadsheet format and is written as the portal sent it.

The real `--format csv` output from the same stub (`inv.csv`). The hostile title in the second row
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

- No item details, no data, no thumbnails. This counts and lists. Downloading is `itemvault`'s
  job and it is a different problem.
- No deletes, no re-shares, no transfers. There is no flag that changes the organization.
- No `total` you did not ask for. The number printed is the count of distinct item ids actually
  read, never a number the server reported.
- No usage or cost figures. `size` and `numViews` are copied from the search result as they were
  reported, including the `-1` the portal uses for an unknown size.
- No group membership. An item's groups need a call per group, which is `sharewatch`'s shape,
  not this one's.
- It cannot see items it is not allowed to see. An anonymous census counts public items, and a
  census run as a non-administrator counts what that user can search. The number is honest about
  the query, not about the organization.
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
  file as an inventory to read, and as input to a script, not as a workbook to resave.
- The tail partition ends at 9999-12-31 (`253402300799999` ms). That value is accepted by the
  stub portals used here. It was not run against a real ArcGIS portal, so if a portal rejects
  it the census exits 2 with the portal's message rather than returning a total.
- The numeric fields (`created`, `modified`, `size`, `numViews`) are assumed to be numbers. A
  portal that sends text there, for example `"created": "abc"`, stops a run that has `--out`
  with a Python traceback and exit 1, which this README does not define as "did not reconcile".
- A search result with no `id` aborts the census with exit 2 and a message that lists the
  fields the row had, not the row. A `fetch` that returns something other than a search
  response is reported by type only.
- A token travels in the query string of each search request, which is where Esri's search
  endpoint expects it. Anything that logs request urls on the portal or on a proxy will see it.
- `--url http://...` together with `--username` posts the password over cleartext HTTP. Only
  `--insecure` is refused with `--username`. Use an `https://` url when you sign in.
- Do not put credentials in `--url`. It is printed as `reading <url>` and saved as the `url`
  field of the inventory exactly as given.
- The `--query` and the progress lines are not passed through the console-encoding guard. A
  query with a character the console code page cannot encode, such as a Polish l-stroke on
  cp1252, exits 2 with an encoding error. Set `PYTHONIOENCODING=utf-8`.
- The inventory is written by opening the file for writing, so it is truncated first. A crash or
  a full disk part-way through leaves a truncated file. Write to a new file name for each run.
  A write that fails exits 73 with a message. A title or owner that is not encodable as UTF-8,
  such as a lone surrogate, also exits 73, and the CSV can be left part-written.
- `--insecure` is covered only by a stand-in that records the flag, because the stub portal is
  plain `http://127.0.0.1`. The password prompt (`getpass`) is never run from a terminal.
- The self-test binds a loopback `http.server` on `127.0.0.1`. It fails on a host that has no
  loopback interface or forbids local sockets.
- The examples and the self-test fixtures use the placeholder url `https://county.maps.arcgis.com`
  and the user `gis_admin`. They are invented and name no real organization.
- A portal error whose `error` value is a bare string is reported with that string. Portal
  behavior not listed here was not checked against a real portal: every network path ran
  against a local stub.

## Verification

Measured on 2026-11-11, all against the same file.

| Host | Python | Assertions |
|---|---|---|
| Windows | 3.13 | 417, 0 failed |
| Windows | 3.12 | 417, 0 failed |
| Windows | 3.10.20 | 417, 0 failed |
| Windows | 3.9.25 | 417, 0 failed |
| Windows, ArcGIS Pro `arcgispro-py3` clone | 3.13.7 | 417, 0 failed |
| Linux (Ubuntu) | 3.12.3 | 417, 0 failed |

The count is the same on every host, and the Linux output is byte for byte the Windows output
once the carriage returns are stripped. Branch coverage of `itemcensus.py` is 100 percent
(`coverage run --branch itemcensus.py --self-test`, 1256 statements, 208 branches). The last
branches are reached by running the whole self-test once with one function broken, to prove the
summary goes red, and by running the file with `runpy` once as a script and once as a module.
`pyflakes` is clean and `python -W error` compiles it. Coverage ran on Windows only, because
`coverage` is not installed on the Linux host.

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
- OWASP, [CSV Injection](https://community.owasp.org/attacks/CSV_Injection): the leading
  characters and the single quote prefix.
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
- [sightline](https://github.com/uhsear/sightline) - what a viewer can actually see among the items you just counted
- [whobreaks](https://github.com/uhsear/whobreaks) - what references an item before you delete it
- [ghostsvc](https://github.com/uhsear/ghostsvc) - services the portal does not know it publishes
