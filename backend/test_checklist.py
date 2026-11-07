"""
The tick-off checklist.

Verdicts live in the browser here — this service has no database — so these
cover the part the server owns: turning a run's line items into the structure
the UI renders, with ids stable enough that a verdict survives a re-run.
"""

import unittest

from checklist import build


def rec(order_id, sku, outcome="printed", quantity=1, seq=0, name=None):
    return {"order_id": order_id, "order_name": name or f"#{order_id}",
            "sku": sku, "quantity": quantity, "outcome": outcome, "seq": seq}


RECORDS = [
    rec(3891, "POSTER1182A4", quantity=2, seq=0),
    rec(3891, "ANIME55A3", seq=1),
    rec(3891, "KPOPSTIC271", seq=2),
    rec(3891, "GIFTWRAP", outcome="skipped", seq=3),
    rec(3892, "HP11A3", outcome="missing", seq=4),
    rec(3892, "CUSTOMPP", outcome="failed", seq=5),
]


class ChecklistTests(unittest.TestCase):
    def test_grouped_by_order_then_print_size(self):
        data = build(RECORDS)
        self.assertEqual([p["label"] for p in data["parents"]], ["#3891", "#3892"])
        self.assertEqual([g["key"] for g in data["parents"][0]["groups"]],
                         ["A3", "A4", "stickers"])

    def test_unprintable_items_are_left_out(self):
        """Gift wrap is not something anyone can tick off a pile."""
        self.assertNotIn("GIFTWRAP", str(build(RECORDS)))
        self.assertEqual(build(RECORDS)["totals"]["items"], 5)

    def test_every_item_is_in_exactly_one_bucket(self):
        counts = {f["key"]: f["count"] for f in build(RECORDS)["filters"]}
        exclusive = sum(v for k, v in counts.items() if k != "all")
        self.assertEqual(exclusive, counts["all"])

    def test_a_verdict_outranks_a_flag(self):
        """Ticking a no-artwork item empties that worklist."""
        data = build(RECORDS)
        noartwork = next(i["id"] for p in data["parents"] for g in p["groups"]
                         for i in g["items"] if i["bucket"] == "noartwork")
        after = {f["key"]: f["count"]
                 for f in build(RECORDS, {noartwork: "done"})["filters"]}
        self.assertEqual(after["noartwork"], 0)
        self.assertEqual(after["done"], 1)

    def test_ids_survive_a_re_run(self):
        """The whole reason ticks persist between runs."""
        first = {i["id"] for p in build(RECORDS)["parents"]
                 for g in p["groups"] for i in g["items"]}
        again = {i["id"] for p in build(list(reversed(RECORDS)))["parents"]
                 for g in p["groups"] for i in g["items"]}
        self.assertEqual(first, again)

    def test_the_same_sku_twice_in_one_order_gets_two_ids(self):
        data = build([rec(1, "DUPA4", seq=0), rec(1, "DUPA4", seq=1)])
        items = data["parents"][0]["groups"][0]["items"]
        self.assertEqual(len({i["id"] for i in items}), 2)

    def test_orders_sort_numerically_not_as_strings(self):
        """"999" sorts after "1000" as a string, and looks fine until it isn't."""
        data = build([rec(999, "AA4"), rec(1000, "BA4")])
        self.assertEqual([p["id"] for p in data["parents"]], ["999", "1000"])

    def test_checked_counts_any_verdict_not_just_done(self):
        """The bar answers 'how much is left', so bad news must move it."""
        first = build(RECORDS)["parents"][0]["groups"][0]["items"][0]["id"]
        totals = build(RECORDS, {first: "missing"})["totals"]
        self.assertEqual((totals["checked"], totals["done"], totals["missing"]),
                         (1, 0, 1))

    def test_scope_is_derived_from_the_data(self):
        self.assertEqual(build(RECORDS)["scope"], "2 order(s), #3891-#3892")
        self.assertEqual(build([])["scope"], "nothing to check")

    def test_a_finished_order_is_marked_complete(self):
        ids = [i["id"] for p in build(RECORDS)["parents"][:1]
               for g in p["groups"] for i in g["items"]]
        parent = build(RECORDS, {i: "done" for i in ids})["parents"][0]
        self.assertIn("complete", parent["chips"])


if __name__ == "__main__":
    unittest.main()
