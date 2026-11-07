"""
Building the tick-off checklist from a run's line items.

Somebody stands at the packing table with the printed pile and confirms every
ordered item is actually there. One pure function walks the data and produces
the whole structure — parents, groups, items, buckets and every count — and
everything else reads from that one walk. Counting in two places is how a
header and a filter end up disagreeing, and once two numbers disagree nobody
trusts any of them.

The item id is the contract. A verdict is stored against it, so if an id
changes the verdict is orphaned and silently lost. Ids are built from
business facts — order, print size, SKU, and an occurrence number for the
case where one order holds the same SKU twice — never from array position,
so re-running to fetch missing artwork leaves every existing tick in place.
"""

from collections import Counter, defaultdict

from sku_rules import route_sku

# A verdict always beats a flag. That is what turns each flag filter into a
# worklist rather than a label: ticking a no-artwork item moves it out of that
# bucket, so the count falls to zero as the work gets done. If flags won, the
# count would never move and you could not tell what was left.
BUCKET_DONE = "done"
BUCKET_MISSING = "missing"
BUCKET_NOARTWORK = "noartwork"
BUCKET_FAILED = "failed"
BUCKET_OPEN = "open"

VERDICT_BUCKETS = {"done": BUCKET_DONE, "missing": BUCKET_MISSING}

FILTERS = (
    ("open", "To check", BUCKET_OPEN),
    ("noartwork", "No artwork", BUCKET_NOARTWORK),
    ("failed", "Failed", BUCKET_FAILED),
    ("missing", "Marked missing", BUCKET_MISSING),
    ("done", "Done", BUCKET_DONE),
    ("all", "Everything", None),
)

# The order the warehouse works through sizes in. Anything unrecognised goes
# to the end under "other" rather than being dropped on the floor.
GROUP_ORDER = ("A3", "A4", "A5", "PP", "stickers", "other")

OUTCOME_FLAGS = {"missing": BUCKET_NOARTWORK, "failed": BUCKET_FAILED}


def group_for(sku):
    """
    Which print-size section a SKU belongs in.

    Reuses the pipeline's own routing, so the checklist groups things exactly
    the way the output folders do.
    """
    routed = route_sku(sku or "")
    if routed is None:
        return "other", "Other"
    _stem, folder, _is_sticker = routed
    return folder, ("Stickers" if folder == "stickers" else folder)


def _numeric(order_id):
    """
    Sort ids numerically where possible.

    They arrive as strings, and a plain string sort puts "999" after "1000".
    It happens to look right across a narrow range and then quietly is not.
    """
    try:
        return (0, int(order_id))
    except (TypeError, ValueError):
        return (1, str(order_id))


def build(records, verdicts=None):
    """
    The single walk.

    ``records`` are dicts with order_id, order_name, sku, quantity, outcome
    and reason. ``verdicts`` maps item id to "done" or "missing" — the server
    has none of its own, so this is usually empty and the browser applies its
    own. Returns everything the UI renders from, counts included, so no
    caller has to total anything itself.
    """
    verdicts = verdicts or {}
    by_parent = defaultdict(list)
    names = {}

    for record in records:
        # Items with nothing to print — gift wrap, shipping protection — are
        # not physical things anyone can tick off a pile, so they are left
        # out rather than sitting there as permanent noise.
        if record.get("outcome") == "skipped":
            continue
        order_id = str(record.get("order_id"))
        by_parent[order_id].append(record)
        if record.get("order_name"):
            names[order_id] = record["order_name"]

    parents = []
    for order_id in sorted(by_parent, key=_numeric):
        grouped = defaultdict(list)
        # An occurrence number per (group, SKU), so one order holding the same
        # SKU twice yields two ids rather than one tick covering both.
        seen = Counter()

        for record in sorted(by_parent[order_id],
                             key=lambda r: (r.get("sku") or "", r.get("seq", 0))):
            key, _label = group_for(record.get("sku"))
            sku = record.get("sku") or "unknown"
            quantity = record.get("quantity") or 1
            seen[(key, sku)] += 1
            item_id = "%s|%s|%s|%02d" % (order_id, key, sku, seen[(key, sku)])
            verdict = verdicts.get(item_id, "")
            grouped[key].append({
                "id": item_id,
                "label": sku if quantity == 1 else "%s  x%d" % (sku, quantity),
                "sku": sku,
                "quantity": quantity,
                "flag": OUTCOME_FLAGS.get(record.get("outcome"), ""),
                "reason": record.get("reason"),
                "verdict": verdict,
                "bucket": (VERDICT_BUCKETS.get(verdict)
                           or OUTCOME_FLAGS.get(record.get("outcome"))
                           or BUCKET_OPEN),
            })

        groups = [{"key": key,
                   "label": "Stickers" if key == "stickers" else
                            ("Other" if key == "other" else key),
                   "total": len(grouped[key]),
                   "items": grouped[key]}
                  for key in GROUP_ORDER if grouped.get(key)]

        items = [item for group in groups for item in group["items"]]
        counts = Counter(item["bucket"] for item in items)
        chips = []
        if counts[BUCKET_NOARTWORK]:
            chips.append("%d no artwork" % counts[BUCKET_NOARTWORK])
        if counts[BUCKET_FAILED]:
            chips.append("%d failed" % counts[BUCKET_FAILED])
        if counts[BUCKET_MISSING]:
            chips.append("%d missing" % counts[BUCKET_MISSING])
        if items and counts[BUCKET_DONE] == len(items):
            chips.append("complete")

        parents.append({
            "id": order_id,
            "label": names.get(order_id, "Order %s" % order_id),
            "subtitle": "%d item(s)" % len(items),
            "total": len(items),
            "done": counts[BUCKET_DONE],
            "chips": chips,
            "groups": groups,
        })

    every = [item for parent in parents
             for group in parent["groups"] for item in group["items"]]
    buckets = Counter(item["bucket"] for item in every)

    return {
        "scope": _scope(parents),
        "parents": parents,
        "totals": {
            "parents": len(parents),
            "items": len(every),
            # "Checked" is any verdict, good or bad: the bar answers "how much
            # of the job is left". Done over total would stall it every time
            # bad news came in.
            "checked": buckets[BUCKET_DONE] + buckets[BUCKET_MISSING],
            "done": buckets[BUCKET_DONE],
            "missing": buckets[BUCKET_MISSING],
        },
        "filters": [
            {"key": key, "label": label,
             "count": len(every) if bucket is None else buckets[bucket]}
            for key, label, bucket in FILTERS
        ],
    }


def _scope(parents):
    """
    Derived, never written by hand.

    A typed scope line goes stale the first time the data changes and then
    lies about what is on screen.
    """
    if not parents:
        return "nothing to check"
    labels = [parent["label"] for parent in parents]
    if len(parents) == 1:
        return "1 order, %s" % labels[0]
    return "%d order(s), %s-%s" % (len(parents), labels[0], labels[-1])
