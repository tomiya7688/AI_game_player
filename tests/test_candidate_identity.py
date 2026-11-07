import time
import unittest

from ai_game_player.candidate_identity import (
    MAX_CANDIDATES,
    REASON_AMBIGUOUS,
    REASON_CAPACITY,
    REASON_COLLISION,
    REASON_INVALID,
    REASON_OK,
    MergeContext,
)
from ai_game_player.candidate_merger import CandidateMerger
from ai_game_player.models import ActionCandidate, DetectedElement


def button(action_id, x, y, kind="click", label="OK", bbox=None):
    return ActionCandidate(
        action_id, kind, label, x, y, bbox=bbox or (x - 10, y - 10, 20, 20)
    )


class CandidateIdentityTest(unittest.TestCase):
    def test_same_id_at_different_positions_is_collision_and_yields_no_executable_candidate(
        self,
    ):
        report = CandidateMerger().merge_detailed(
            [button("b", 20, 20)], [button("b", 300, 300)]
        )
        self.assertEqual(report.candidates, ())
        self.assertEqual(len(report.collisions), 2)
        self.assertEqual(report.reason, REASON_COLLISION)

    def test_same_id_with_different_kind_is_collision(self):
        report = CandidateMerger().merge_detailed(
            [button("b", 20, 20)], [button("b", 20, 20, kind="key")]
        )
        self.assertEqual(report.candidates, ())
        self.assertEqual(report.reason, REASON_COLLISION)

    def test_different_ids_same_bbox_and_label_merge_and_keep_origin_ids(self):
        report = CandidateMerger().merge_detailed(
            [button("a", 50, 50)], [button("ocr-0", 50, 50)]
        )
        self.assertEqual(len(report.candidates), 1)
        self.assertEqual(report.sources, (("automation", "ocr"),))
        self.assertEqual(report.origin_ids, (("a", "ocr-0"),))
        self.assertEqual(report.reason, REASON_OK)

    def test_same_text_at_different_positions_stays_independent(self):
        report = CandidateMerger().merge_detailed(
            [button("a", 20, 20)], [button("ocr-0", 400, 400)]
        )
        self.assertEqual(len(report.candidates), 2)

    def test_same_id_and_same_target_still_merges(self):
        report = CandidateMerger().merge_detailed(
            [button("a", 50, 50)], [button("a", 52, 50)]
        )
        self.assertEqual(len(report.candidates), 1)
        self.assertEqual(report.origin_ids, (("a",),))

    def test_generation_mismatch_rejects_incoming_candidate(self):
        context = MergeContext(generations={"automation": 1, "ocr": 2})
        report = CandidateMerger().merge_detailed(
            [button("a", 50, 50)], [button("ocr-0", 50, 50)], context=context
        )
        self.assertEqual([c.action_id for c in report.candidates], ["a"])
        self.assertEqual(report.rejected[0][1], REASON_AMBIGUOUS)
        self.assertEqual(report.reason, REASON_AMBIGUOUS)

    def test_coordinate_space_mismatch_is_not_merged(self):
        context = MergeContext(
            coordinate_spaces={"automation": "window", "ocr": "screen"}
        )
        report = CandidateMerger().merge_detailed(
            [button("a", 50, 50)], [button("ocr-0", 50, 50)], context=context
        )
        self.assertEqual(len(report.candidates), 1)
        self.assertEqual(len(report.rejected), 1)

    def test_label_conflict_with_overlapping_boxes_is_not_merged(self):
        start = DetectedElement("s", "text", (0, 0, 100, 40), "ocr", 0.8, "START")
        quit_ = DetectedElement("q", "text", (0, 0, 100, 40), "ocr", 0.8, "QUIT")
        self.assertEqual(
            len(CandidateMerger().merge_detailed([], [start, quit_]).candidates), 2
        )

    def test_candidates_without_coordinates_are_kept_separately(self):
        keys = [ActionCandidate("w", "key", "W"), ActionCandidate("s", "key", "S")]
        self.assertEqual(len(CandidateMerger().merge_detailed(keys, []).candidates), 2)

    def test_nan_coordinates_are_rejected(self):
        bad = [
            ActionCandidate("nan", "click", "N", float("nan"), 1),
        ]
        report = CandidateMerger().merge_detailed(bad, [])
        self.assertEqual(report.candidates, ())
        self.assertEqual({reason for _c, reason in report.rejected}, {REASON_INVALID})
        self.assertEqual(report.reason, REASON_INVALID)

    def test_collision_uses_merged_origin_ids_and_keeps_them_in_report(self):
        report = CandidateMerger().merge_detailed(
            [button("a", 20, 20), button("x", 300, 300)], [button("x", 20, 20)]
        )
        self.assertEqual(report.candidates, ())
        self.assertEqual(report.reason, REASON_COLLISION)
        self.assertIn(("a", "x"), report.collision_origin_ids)
        self.assertEqual(len(report.collision_sources), 2)

    def test_huge_integer_coordinate_is_rejected_not_raised(self):
        huge = ActionCandidate("h", "click", "H", 10**400, 1)
        report = CandidateMerger().merge_detailed([huge], [])
        self.assertEqual(report.candidates, ())
        self.assertEqual(report.reason, REASON_INVALID)

    def test_over_capacity_is_held_as_unknown(self):
        many = [button(f"c{i}", i * 30, 0) for i in range(MAX_CANDIDATES + 1)]
        report = CandidateMerger().merge_detailed(many, [])
        self.assertEqual(report.candidates, ())
        self.assertEqual(report.reason, REASON_CAPACITY)

    def test_comparisons_stay_bounded_at_capacity(self):
        many = [button(f"c{i}", i * 30, 0) for i in range(MAX_CANDIDATES)]
        started = time.perf_counter()
        report = CandidateMerger().merge_detailed(many[:64], many[64:])
        self.assertLessEqual(
            report.comparisons, MAX_CANDIDATES * (MAX_CANDIDATES - 1) // 2
        )
        self.assertLess(time.perf_counter() - started, 1.0)

    def test_merge_with_sources_hides_collisions_from_legacy_callers(self):
        merged = CandidateMerger().merge_with_sources(
            [button("b", 20, 20)], [button("b", 300, 300)]
        )
        self.assertEqual(merged, [])


if __name__ == "__main__":
    unittest.main()


def test_issue_853_disjoint_rects_are_not_merged_by_proximity() -> None:
    from ai_game_player.candidate_merger import CandidateMerger
    from ai_game_player.models import ActionCandidate

    first = ActionCandidate("a", "click", "OK", 10, 10, 0.9, False, (0, 0, 10, 10))
    second = ActionCandidate("b", "click", "OK", 20, 10, 0.9, False, (15, 0, 10, 10))
    report = CandidateMerger(proximity=20).merge_detailed([first, second], [])
    assert len(report.candidates) == 2


def test_issue_853_incoming_is_checked_against_all_group_members() -> None:
    from ai_game_player.candidate_identity import MergeContext, REASON_AMBIGUOUS
    from ai_game_player.candidate_merger import CandidateMerger
    from ai_game_player.models import ActionCandidate

    rep = ActionCandidate("a", "click", "", 10, 10, 0.9, False, (0, 0, 10, 10))
    same = ActionCandidate("b", "click", "", 10, 10, 0.5, False, (0, 0, 10, 10))
    other_gen = ActionCandidate("c", "click", "", 10, 10, 0.5, False, (0, 0, 10, 10))
    ctx = MergeContext(generations={"automation": 1, "ocr": 1, "image": 2})
    report = CandidateMerger().merge_detailed([rep, same], [], [other_gen], ctx)
    assert report.reason == REASON_AMBIGUOUS
