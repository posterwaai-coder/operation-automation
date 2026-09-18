"""
The "Include ₹0 creator orders" run control.

UGC/creator gift orders arrive in Shopify at a total of exactly ₹0. Turning
the control off must drop the whole order from the consignment, and the API
must actually hand the choice to the pipeline — the UI sending the flag is not
enough on its own.
"""

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend import api
from backend.sku_rules import collect_unfulfilled_skus, is_creator_order


class CreatorOrderTests(unittest.TestCase):
    def test_zero_current_total_is_creator_order(self):
        order = SimpleNamespace(current_total_price="0.00", total_price="499.00")
        self.assertTrue(is_creator_order(order))

    def test_total_price_is_used_for_older_payloads(self):
        self.assertTrue(is_creator_order(SimpleNamespace(total_price=0)))

    def test_paid_and_unknown_orders_are_not_creator_orders(self):
        self.assertFalse(is_creator_order(SimpleNamespace(current_total_price="1.00")))
        self.assertFalse(is_creator_order(SimpleNamespace()))
        self.assertFalse(is_creator_order(SimpleNamespace(total_price="not-a-number")))

    def test_toggle_filters_the_complete_zero_value_order(self):
        paid_item = SimpleNamespace(sku="PAIDA4", quantity=1, properties=[])
        creator_items = [
            SimpleNamespace(sku="CREATORA3", quantity=2, properties=[]),
            SimpleNamespace(sku="CREATORSTIC", quantity=1, properties=[]),
        ]
        orders = [
            SimpleNamespace(id=1, fulfillment_status=None,
                            current_total_price="499.00", line_items=[paid_item]),
            SimpleNamespace(id=2, fulfillment_status=None,
                            current_total_price="0.00", line_items=creator_items),
            SimpleNamespace(id=3, fulfillment_status="fulfilled",
                            current_total_price="0.00", line_items=creator_items),
        ]

        included, skipped_orders, skipped_skus = collect_unfulfilled_skus(orders, True)
        self.assertEqual([row[1] for row in included], ["PAIDA4", "CREATORA3", "CREATORSTIC"])
        self.assertEqual((skipped_orders, skipped_skus), (0, 0))

        excluded, skipped_orders, skipped_skus = collect_unfulfilled_skus(orders, False)
        self.assertEqual([row[1] for row in excluded], ["PAIDA4"])
        self.assertEqual((skipped_orders, skipped_skus), (1, 2))


class ApiWiringTests(unittest.TestCase):
    class ImmediateThread:
        def __init__(self, target, daemon=True):
            self.target = target

        def start(self):
            self.target()

    def test_setting_defaults_on_and_persists_off(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(api, "CONFIG_FILE", os.path.join(temp_dir, "settings.json")):
            client = api.app.test_client()
            self.assertTrue(client.get("/api/settings").get_json()["include_creator_orders"])
            client.post("/api/settings", json={"include_creator_orders": False})
            self.assertFalse(client.get("/api/settings").get_json()["include_creator_orders"])

    def test_run_passes_the_toggle_to_the_pipeline(self):
        captured = {}

        def run_script(**kwargs):
            captured.update(kwargs)

        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(api, "CONFIG_FILE", os.path.join(temp_dir, "settings.json")), \
                patch.object(api.threading, "Thread", self.ImmediateThread), \
                patch.dict(sys.modules, {"main": SimpleNamespace(run_script=run_script)}):
            api._run_state["running"] = False
            response = api.app.test_client().post("/api/run", json={
                "source_folder_id": "drive-id",
                "recipient_email": "ops@example.com",
                "include_creator_orders": False,
            })

        self.assertEqual(response.status_code, 200)
        self.assertIs(captured["include_creator_orders"], False)


if __name__ == "__main__":
    unittest.main()
