"""Fixed resource diagnostics preserve unknown, parser failure and wire limits."""

from copy import deepcopy
import json
import unittest

from tools import reference_browser_diagnostics as diag
from tests.test_reference_browser_diagnostics import render_value


class BrowserResourceTimingTests(unittest.TestCase):
    def test_unknown_remains_none_and_response_fits_existing_wire_caps(self):
        value = render_value()
        self.assertEqual(value, diag._sanitize(value))
        value["resource_timing"]["state"] = "available"
        value["resource_timing"]["resource_count_capped"] = 11
        value["resource_timing"]["script_count_capped"] = 10
        for item in value["resource_timing"]["resources"].values():
            item.update(
                {
                    "dom_script_present": True,
                    "entry_count": 1,
                    "response_status": 200,
                    "transfer_size": 22000,
                    "encoded_body_size": 21000,
                    "decoded_body_size": 21000,
                    "response_start": True,
                    "response_end": True,
                }
            )
        value["actuator_constructor"] = False
        value["grid_constructor"] = False
        value["tile_constructor"] = False
        for name in ("html_actuator_js", "grid_js", "tile_js"):
            value["resource_timing"]["resources"][name].update(
                {
                    "response_status": 0,
                    "transfer_size": 0,
                    "encoded_body_size": 0,
                    "decoded_body_size": 0,
                    "response_start": False,
                    "response_end": True,
                }
            )
        self.assertEqual(value, diag._sanitize(value))
        self.assertLess(diag._field_count(value), diag.MAX_FIELDS)
        raw = json.dumps(
            {"id": 1, "result": {"result": {"type": "object", "value": value}}}
        )
        self.assertLess(len(raw.encode()), diag.MAX_MESSAGE_BYTES)
        self.assertLess(diag._field_count(json.loads(raw)), diag.MAX_FIELDS)

    def test_missing_is_distinct_from_unknown_and_duplicate_is_not_chosen(self):
        value = render_value()
        timing = value["resource_timing"]
        timing.update(state="available", resource_count_capped=10)
        for item in timing["resources"].values():
            item["entry_count"] = 0
        self.assertEqual(value, diag._sanitize(value))
        timing["resources"]["html_actuator_js"]["entry_count"] = 2
        self.assertEqual(value, diag._sanitize(value))
        timing["resources"]["html_actuator_js"]["response_status"] = 200
        with self.assertRaises(ValueError):
            diag._sanitize(value)
        unknown = render_value()
        unknown["resource_timing"]["resources"]["html_actuator_js"]["entry_count"] = 0
        with self.assertRaises(ValueError):
            diag._sanitize(unknown)

    def test_rejects_urls_free_text_out_of_range_and_inconsistent_caps(self):
        variants = []
        for field, bad in (
            ("response_status", "ERR_CONNECTION_RESET"),
            ("response_status", 600),
            ("transfer_size", float("nan")),
            ("decoded_body_size", 16777217),
            ("response_start", "true"),
        ):
            value = render_value()
            value["resource_timing"]["resources"]["grid_js"][field] = bad
            variants.append(value)
        value = render_value()
        value["resource_timing"]["resources"]["url?secret"] = {}
        variants.append(value)
        value = render_value()
        value["resource_timing"]["resources"]["grid_js"]["url"] = "private"
        variants.append(value)
        value = render_value()
        value["resource_timing"]["resource_count_capped"] = 129
        variants.append(value)
        value = render_value()
        value["resource_timing"].update(state="overflow", resource_count_capped=127)
        variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                diag._sanitize(deepcopy(value))

    def test_expression_only_reads_fixed_resources_and_never_records_network_events(
        self,
    ):
        for forbidden in (
            "Network.enable",
            "PerformanceObserver",
            "addEventListener",
            "clearResourceTimings",
            "setResourceTimingBufferSize",
            "getResponseBody",
            "console.",
            "localStorage.",
            "textContent",
        ):
            self.assertNotIn(forbidden, diag.RENDER_EXPRESSION)
        self.assertIn("entries.length>128", diag.RENDER_EXPRESSION)
        self.assertIn("Math.min(document.scripts.length, 32)", diag.RENDER_EXPRESSION)


if __name__ == "__main__":
    unittest.main()
