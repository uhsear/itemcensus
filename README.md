# itemcensus

Inventory every item in an ArcGIS organization, and refuse to report a total the server cannot
prove is complete. Read-only, never changes anything in the org.

You are migrating an organization, or costing one, or decommissioning one, and the first thing
anybody asks is how many items are in it. So you run a census. It says 10,000.

Esri's REST reference says it plainly about `/sharing/rest/search`: the count is accurate only
to 10,000, beyond that the server returns 10,000, and `num` is capped at 100 whatever you ask
for. Every inventory, backup and cleanup script in the wild inherits that ceiling and reports
success at it. Your census is wrong by an unknown amount, and the thing that makes it dangerous
is that a true total of 10,000 and a truncated one are identical on the screen. Nothing is
missing, nothing errors, nothing warns. You find out after the migration, when somebody goes
looking for a layer that was never in the list.

```
$ python itemcensus.py --self-test
itemcensus self-test: no portal, no internet, no credentials
--------------------------------------------------------------------
PASS  a page size of 1000 is clamped to 100 before it is sent, because the server clamps it silently  <-- pinned defect
PASS  exactly 10,000 is the CAP and never a total  <-- pinned defect
PASS  10,000 rows read against a reported 10,000 is NOT complete  <-- pinned defect
PASS  a nextStart of -1 ends the crawl  <-- pinned defect
PASS  a short page advances by the ten rows it got, not by the hundred it asked for  <-- pinned defect
PASS  a next page starting past the 10,000 ceiling is not asked for, because search refuses that start  <-- pinned defect
...
PASS  the base query is parenthesised, or the date range would AND onto its last term only  <-- pinned defect
PASS  the second half starts one millisecond after the first ends, so no item falls in both  <-- pinned defect
PASS  a wide range carrying owners and types still splits on DATE first  <-- pinned defect
PASS  the other half is NOT the remaining owners but everyone else, so an owner missing from the list is still counted  <-- pinned defect
PASS  after the last named owner the complement has no owner left, so the split terminates instead of emitting owner:(cal) for ever  <-- pinned defect
PASS  four type splits leave ONE type clause, not four nested ones that grow the url until search answers 414  <-- pinned defect
PASS  an exclusion is kept when the positive clause beside it is replaced, because 'not ann' is not implied by 'is bob'  <-- pinned defect
PASS  a one day partition with nothing left to split on REFUSES  <-- pinned defect
PASS  a total that moves mid-crawl is a warning  <-- pinned defect
...
PASS  paging reads all 250 items, not the 100 the server caps a page at  <-- pinned defect
PASS  the reported total is read on every page, not only the first  <-- pinned defect
PASS  a server that quietly returns ten rows for a page of a hundred is still paged to the end  <-- pinned defect
PASS  the stand-in server reports 10,000 for an org of 10,400, the way the real one does  <-- pinned defect
PASS  an org of 10,400 items counts to 10,400, not to the 10,000 the server reported  <-- pinned defect
PASS  not one finished partition sat on the ceiling  <-- pinned defect
PASS  but it was bisected first, because 10,000 could not be proven complete without splitting it  <-- pinned defect
PASS  10,400 items created in one second are still all counted, by splitting on owner  <-- pinned defect
PASS  an owner list missing half the org still counts every item, because the other half of each split is 'not these owners'  <-- pinned defect
PASS  a partition still at the cap after bisecting down to one day REFUSES rather than returning a number  <-- pinned defect
PASS  a census that runs past --max-partitions refuses instead of bisecting for ever  <-- pinned defect
PASS  the census reports the ids it actually read, not the total that moved  <-- pinned defect
PASS  and exit 1 lines up with reconciled=false in the document, not with a census that reconciled  <-- pinned defect
PASS  two partitions that both return the same two items count two, not four  <-- pinned defect
PASS  an item seen ONLY in a partition that sat on the ceiling is not in the census, because nothing complete vouches for it  <-- pinned defect
...
PASS  the service url is dropped, because it can carry a token  <-- pinned defect
PASS  items are written in id order even when the portal returned them backwards, so an unchanged org writes the same bytes twice  <-- pinned defect
PASS  a url with no scheme is refused, because urllib quotes the whole url back into its error and that url carries the token  <-- pinned defect
PASS  a portal past the ceiling is counted through the real fetch layer, not stopped at 10,000  <-- pinned defect
PASS  a portal answering with a JSON array is refused, not fed to body.get() as a traceback  <-- pinned defect
PASS  the token never reaches stdout  <-- pinned defect
PASS  a search result with no id exits 2, not a traceback  <-- pinned defect
PASS  no credential reaches the inventory file on disk  <-- pinned defect
PASS  the csv has no blank line between rows, which is what newline='' prevents on windows  <-- pinned defect
PASS  id starting with equals is written with a leading apostrophe  <-- pinned defect
PASS  title starting with equals is written with a leading apostrophe  <-- pinned defect
PASS  owner starting with equals is written with a leading apostrophe  <-- pinned defect
PASS  a size of -1 stays the number -1, not the text '-1  <-- pinned defect
PASS  a whole inventory round trips: write, read, strip one apostrophe, and every title and owner is what the portal sent  <-- pinned defect
PASS  --format csv --apply guards every hostile title the portal returned, and a reader strips them back  <-- pinned defect
PASS  and the titles it got off the wire are guarded the same way  <-- pinned defect
PASS  an HTTP 500 from a real server exits 2 without the token
PASS  a census that cannot be completed exits 2, and does not print a total  <-- pinned defect
PASS  the command line counts an org past the ceiling  <-- pinned defect
PASS  a unique prefix of --apply is refused, not read as --apply  <-- pinned defect
PASS  there is no --password attribute at all, because argv is readable by every process on the box  <-- pinned defect
...
PASS  the harness records a false check, a missing exception, two wrong exceptions, an argv argparse accepted, a census that did not refuse, a census that raised the wrong thing, a portal call that did not fail and one that refused instead as nine failures, so a broken tool turns this self-test red  <-- pinned defect
PASS  a tool with one broken function turns the whole self-test red: exit 1, and the summary names the assertion that failed
PASS  run as a script, it exits with main()'s code: 64 for no --url
--------------------------------------------------------------------
399 assertions, 0 failed
```

## Requirements

Python 3.9 or newer. Standard library only: `urllib`, `json`, `csv`, `os`, `ssl`, `io`,
`argparse`, `datetime`, `getpass`, `time`, and `re`, `tempfile`, `shutil`, `http.server`,
`threading` and `runpy` inside the self-test. It runs on ArcGIS Pro's Python and on a plain `python3`. `arcpy` is not used and the
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
| `--token` | none | An existing portal token. |
| `--username` | none | Sign in as this user. The password comes from `ITEMCENSUS_PASSWORD` or an unechoed prompt. |
| `--out` | none | File to write the inventory to. |
| `--format` | `json` | `json` or `csv`. |
| `--num` | `100` | Rows per page to ask for. Clamped to 100, which is all the server gives. |
| `--max-partitions` | `4096` | Give up after this many partitions rather than bisecting for ever. |
| `--insecure` | off | Skip TLS verification, for an Enterprise portal behind an internal CA. Refused together with `--username`. |
| `--apply` | off | Write the inventory file. Without it nothing is written. |
| `--self-test` | off | Run the offline assertions and exit. |

Exit codes: 0 a complete census, 1 the census did **not** reconcile and its total is a floor, 2 the
census could not be completed, 64 usage error.

The password is never a flag. `argv` is readable by every process on the machine, it lands in
shell history and in scheduler logs, and `--self-test` asserts that no `--password` attribute
exists at all.

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

The bisect goes created date, then owner, then item type:

```
(orgid:ORG) AND created:[0 TO 1789580538209]                    10,000  ->  split
  (orgid:ORG) AND created:[0 TO 894790269104]                        0  ->  done
  (orgid:ORG) AND created:[894790269105 TO 1789580538209]       10,000  ->  split
    (orgid:ORG) AND created:[894790269105 TO 1342185403657]          0  ->  done
    (orgid:ORG) AND created:[1342185403658 TO 1789580538209]    10,000  ->  split
      ... AND created:[1342185403658 TO 1565882970933]               0  ->  done
      ... AND created:[1565882970934 TO 1789580538209]          10,000  ->  split
        ... AND created:[1565882970934 TO 1677731754571]         9,250  ->  done
        ... AND created:[1677731754572 TO 1789580538209]         1,150  ->  done

10400 item(s), read in 5 partition(s) over 508 page(s), 4 bisection(s)
```

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
code than this. Neither one can tell you whether what came back was everything: `advanced_search`
hands back the same capped `total` the REST endpoint gave it, and `max_items=-1` stops at the
ceiling without saying so. The gap is not paging. It is proof.

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

`--format json` writes the partition list next to the items, so the file carries its own proof:

```json
{
 "itemcensus": 1,
 "url": "https://county.maps.arcgis.com",
 "query": "orgid:ORG",
 "taken": "2026-09-16T17:42:34Z",
 "total": 10400,
 "reconciled": true,
 "bisections": 4,
 "pages": 508,
 "warnings": [],
 "partitions": [
  {"query": "(orgid:ORG) AND created:[0 TO 894790269104]", "reported": 0, "returned": 0},
  {"query": "(orgid:ORG) AND created:[1565882970934 TO 1677731754571]", "reported": 9250, "returned": 9250}
 ],
 "items": [{"access": "public", "created": 1577836800000, "id": "item000000",
            "modified": 1577836800007, "numViews": 0, "owner": "ann", "size": 4096,
            "title": "Parcels 0", "type": "Feature Service"}]
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

```
id,title,owner,type,created,modified,access,size,numViews
item000001,"'=HYPERLINK(""http://x.invalid"")",jsmith,Web Map,1577836800000,1577836800005,org,-1,0
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
- Guard anything beyond those six leading characters. OWASP's page also lists a line feed and the
  full-width forms of `=`, `+`, `-` and `@`. This tool follows the six the portfolio uses, so a
  title that starts with a full-width equals sign is written as it came.
- Tell a guarded cell from a title that really starts with an apostrophe. A title written `'=x`
  is not changed, so a reader that strips one apostrophe turns it into `=x`. A self-test
  assertion marked "known limit" pins that.
- Promise the quote prefix survives a spreadsheet. The assertions are on the bytes in the file.
  Nothing here opened Excel. OWASP warns the quote techniques are not reliable in Excel after
  saving and re-opening a CSV, so treat the file as an inventory to read, and as input to
  a script, not as a workbook to resave.
- It cannot see items it is not allowed to see. An anonymous census counts public items, and a
  census run as a non-administrator counts what that user can search. The number is honest about
  the query, not about the organization.
- More than 10,000 items created by one user, of one type, in one day cannot be counted by this
  method, because the search API offers nothing narrower to split on. That is the case it
  refuses, and the refusal names the exact query it gave up on.

## Verification

Measured on 2026-11-11, all against the same file.

| Host | Python | Assertions |
|---|---|---|
| Windows | 3.13 | 399, 0 failed |
| Windows | 3.12 | 399, 0 failed |
| Windows | 3.9.25 | 399, 0 failed |
| Linux (Ubuntu) | 3.12.3 | 399, 0 failed |

The count is the same on every host. Branch coverage of `itemcensus.py` is 100 percent
(`coverage run --branch itemcensus.py --self-test`, 1188 statements, 198 branches). The last
branches are reached by running the whole self-test once with one function broken, to prove the
summary goes red, and by running the file with `runpy` once as a script and once as a module.
`pyflakes` is clean and `python -W error` compiles it. Coverage ran on Windows only, because
`coverage` is not installed on the Linux host.

## References

- Esri, [Search](https://developers.arcgis.com/rest/users-groups-and-items/search/): `num`
  maximum 100, `start` is the 1-based first result, `total` is counted accurately up to 10,000,
  `nextStart`, and the top 10,000 results are the ones available through pagination.
  `sortField` accepts `created`, `sortOrder` defaults to ascending.
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
