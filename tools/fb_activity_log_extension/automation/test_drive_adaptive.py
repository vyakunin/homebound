#!/usr/bin/env python3
"""Unit tests for adaptive descent logic in drive_via_cdp.py.

Pure-Python — no CDP, no Chrome, no FB. Exercises the cap-detection
heuristic and the scope-children generator. Catches regressions in the
two places that decide "should we descend year → month for this scope".

Run:
    uv run --no-project python3 tools/fb_activity_log_extension/automation/test_drive_adaptive.py
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from drive_via_cdp import (  # noqa: E402
    LIKELY_CAP_MAX,
    LIKELY_CAP_MIN,
    ProgressEntry,
    Scope,
    StopReason,
    initial_scopes,
    is_capped,
    next_pass_scopes,
)


def _entry(label: str, items: int, stop: StopReason) -> ProgressEntry:
    return ProgressEntry.from_payload(
        {"unit": label, "items": items, "rounds": 11, "stoppedBecause": stop.name.replace("_", "").lower(), "unitMs": 5000}
    )


class TestScope(unittest.TestCase):
    def test_year_label_no_month(self):
        self.assertEqual(Scope(year=2018).label, "2018")

    def test_month_label_padded(self):
        self.assertEqual(Scope(year=2018, month=3).label, "2018-03")

    def test_year_children_are_12_months_newest_first(self):
        children = Scope(year=2018).children()
        self.assertEqual(len(children), 12)
        self.assertEqual(children[0], Scope(year=2018, month=12))
        self.assertEqual(children[-1], Scope(year=2018, month=1))

    def test_month_children_empty_no_url_granularity_below_month(self):
        self.assertEqual(Scope(year=2018, month=3).children(), [])


class TestStopReasonParsing(unittest.TestCase):
    def test_canonical_strings_round_trip(self):
        cases = [
            ("scrollStable", StopReason.SCROLL_STABLE),
            ("stalled", StopReason.STALLED),
            ("capPosts", StopReason.CAP_POSTS),
            ("capComments", StopReason.CAP_COMMENTS),
            ("error", StopReason.ERROR),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(StopReason.from_str(raw), expected)

    def test_unknown_string_yields_invalid_not_exception(self):
        self.assertEqual(StopReason.from_str("totally-new-reason"), StopReason.INVALID)
        self.assertEqual(StopReason.from_str(None), StopReason.INVALID)
        self.assertEqual(StopReason.from_str(""), StopReason.INVALID)


class TestIsCapped(unittest.TestCase):
    def test_natural_stop_with_batch_sized_items_is_capped(self):
        # The May 28 observation: ~25 items, scroll-stable. Classic cap.
        self.assertTrue(is_capped(_entry("2018", 25, StopReason.SCROLL_STABLE)))
        self.assertTrue(is_capped(_entry("2018", 32, StopReason.SCROLL_STABLE)))
        self.assertTrue(is_capped(_entry("2018", 26, StopReason.STALLED)))

    def test_at_thresholds_is_capped(self):
        self.assertTrue(is_capped(_entry("2018", LIKELY_CAP_MIN, StopReason.SCROLL_STABLE)))
        self.assertTrue(is_capped(_entry("2018", LIKELY_CAP_MAX, StopReason.SCROLL_STABLE)))

    def test_too_few_items_not_capped_likely_sparse_scope(self):
        # Few items + natural stop = scope is genuinely sparse, no descent needed
        self.assertFalse(is_capped(_entry("2014-08", 5, StopReason.SCROLL_STABLE)))
        self.assertFalse(is_capped(_entry("2014-08", LIKELY_CAP_MIN - 1, StopReason.SCROLL_STABLE)))

    def test_too_many_items_not_capped_fb_paginated_fine(self):
        # The May 23 happy case: 223 items came back. No cap.
        self.assertFalse(is_capped(_entry("2018", 223, StopReason.SCROLL_STABLE)))
        self.assertFalse(is_capped(_entry("2018", LIKELY_CAP_MAX + 1, StopReason.SCROLL_STABLE)))

    def test_our_own_cap_stop_not_capped(self):
        # capPosts means OUR --max-items short-circuit fired, not FB's cap.
        # Don't descend — the scope might have hundreds of posts, but iter
        # mode chose not to load them.
        self.assertFalse(is_capped(_entry("2018", 10, StopReason.CAP_POSTS)))
        self.assertFalse(is_capped(_entry("2018", 25, StopReason.CAP_POSTS)))

    def test_error_not_capped(self):
        # An errored scope didn't actually harvest anything; descent won't help.
        self.assertFalse(is_capped(_entry("2018", 0, StopReason.ERROR)))


class TestNextPassScopes(unittest.TestCase):
    def test_capped_year_emits_12_month_children(self):
        progress = [_entry("2018", 25, StopReason.SCROLL_STABLE)]
        children = next_pass_scopes(progress, max_depth=2, current_depth=0)
        self.assertEqual(len(children), 12)
        self.assertEqual(children[0].year, 2018)
        self.assertEqual({c.month for c in children}, set(range(1, 13)))

    def test_uncapped_year_emits_nothing(self):
        progress = [_entry("2018", 200, StopReason.SCROLL_STABLE)]
        self.assertEqual(next_pass_scopes(progress, max_depth=2, current_depth=0), [])

    def test_max_depth_short_circuit(self):
        progress = [_entry("2018", 25, StopReason.SCROLL_STABLE)]
        self.assertEqual(next_pass_scopes(progress, max_depth=1, current_depth=0), [])

    def test_capped_month_skipped_no_url_granularity_below(self):
        # A capped month would need DOM filter manipulation; not supported via URL.
        progress = [_entry("2018-02", 30, StopReason.SCROLL_STABLE)]
        self.assertEqual(next_pass_scopes(progress, max_depth=3, current_depth=1), [])

    def test_mixed_results_only_capped_years_descend(self):
        progress = [
            _entry("2018", 25, StopReason.SCROLL_STABLE),     # capped → descend
            _entry("2019", 5, StopReason.SCROLL_STABLE),      # sparse → skip
            _entry("2020", 200, StopReason.SCROLL_STABLE),    # uncapped → skip
            _entry("2021", 30, StopReason.STALLED),           # capped → descend
        ]
        children = next_pass_scopes(progress, max_depth=2, current_depth=0)
        years_descended = {c.year for c in children}
        self.assertEqual(years_descended, {2018, 2021})
        self.assertEqual(len(children), 24)  # 12 months × 2 years


class TestInitialScopes(unittest.TestCase):
    def test_year_range_newest_first(self):
        from drive_via_cdp import DriverArgs, Mode, Phase

        args = DriverArgs(
            mode=Mode.FULL, phase=Phase.POSTS,
            from_year=2018, to_year=2020,
            month=None, with_media=False, max_items=0,
            adaptive=True, max_depth=2,
        )
        scopes = initial_scopes(args)
        self.assertEqual([s.year for s in scopes], [2020, 2019, 2018])
        self.assertTrue(all(s.month is None for s in scopes))

    def test_single_month_scope(self):
        from drive_via_cdp import DriverArgs, Mode, Phase

        args = DriverArgs(
            mode=Mode.ITER, phase=Phase.POSTS,
            from_year=2018, to_year=2018,
            month=3, with_media=False, max_items=0,
            adaptive=False, max_depth=2,
        )
        scopes = initial_scopes(args)
        self.assertEqual(scopes, [Scope(year=2018, month=3)])


class TestScopeJsUnit(unittest.TestCase):
    def test_year_only_unit_no_month_key(self):
        self.assertEqual(Scope(year=2018).js_unit(), {"year": 2018})

    def test_year_month_unit(self):
        self.assertEqual(Scope(year=2018, month=3).js_unit(), {"year": 2018, "month": 3})


if __name__ == "__main__":
    unittest.main()
