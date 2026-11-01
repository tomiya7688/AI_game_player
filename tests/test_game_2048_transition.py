import ast
from dataclasses import FrozenInstanceError, replace
from math import fsum
from pathlib import Path
import random
import unittest

from ai_game_player.game_2048_state import Game2048State
from ai_game_player.game_2048_transition import (
    DIRECTIONS,
    ReferenceGame2048TransitionModel,
)
from ai_game_player.simulation_provider import SimulationKey, SimulationProvider
from ai_game_player.transition_model import ActionTransition, ChanceOutcome


EMPTY = ((0, 0, 0, 0),) * 4


class Game2048TransitionTests(unittest.TestCase):
    def setUp(self):
        self.model = ReferenceGame2048TransitionModel()

    def test_state_copies_mutable_input_and_rejects_invalid_values(self):
        rows = [list(row) for row in EMPTY]
        state = Game2048State(rows)
        rows[0][0] = 8
        self.assertEqual(state.cells, EMPTY)
        with self.assertRaises(FrozenInstanceError):
            state.won = True
        for invalid in (None, (), ((0,) * 3,) * 4, ((0,) * 4,) * 5):
            with self.subTest(board=invalid), self.assertRaises(ValueError):
                Game2048State(invalid)
        for tile in (-2, 1, 3, 2.0, True, None, "2"):
            with self.subTest(tile=tile), self.assertRaises(ValueError):
                Game2048State(((tile, 0, 0, 0), *EMPTY[1:]))
        for kwargs in (
            {"score": True},
            {"score": -1},
            {"score": 1.0},
            {"won": 1},
            {"over": None},
            {"keep_playing": 0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Game2048State(EMPTY, **kwargs)

    def test_all_directions_merge_once_and_preserve_input(self):
        for direction in DIRECTIONS:
            vertical = direction in ("UP", "DOWN")
            board = (
                ((2, 0, 0, 0), (2, 0, 0, 0), (4, 0, 0, 0), (4, 0, 0, 0))
                if vertical
                else ((2, 2, 4, 4), *EMPTY[1:])
            )
            state = Game2048State(board, score=100)
            result = self.model.apply_slide(state, direction)
            expected = (4, 8, 0, 0) if direction in ("UP", "LEFT") else (0, 0, 4, 8)
            actual = (
                tuple(row[0] for row in result.state.cells)
                if vertical
                else result.state.cells[0]
            )
            with self.subTest(direction=direction):
                self.assertEqual(actual, expected)
                self.assertTrue(result.changed)
                self.assertEqual(result.reward_delta, 12)
                self.assertEqual(result.state.score, 112)
                self.assertEqual(state.cells, board)
                self.assertEqual(
                    sum(map(sum, result.state.cells)), sum(map(sum, board))
                )

    def test_adjacent_new_tile_cannot_merge_again_in_same_move(self):
        for row, expected, score in (
            ((2, 2, 4, 0), (4, 4, 0, 0), 4),
            ((2, 2, 2, 2), (4, 4, 0, 0), 8),
            ((2, 0, 2, 2), (4, 2, 0, 0), 4),
            ((4, 4, 8, 8), (8, 16, 0, 0), 24),
        ):
            result = self.model.apply_slide(Game2048State((row, *EMPTY[1:])), "LEFT")
            self.assertEqual(result.state.cells[0], expected)
            self.assertEqual(result.reward_delta, score)
            self.assertIsNone(result.state.score)

    def test_empty_noop_and_terminal_states_never_spawn(self):
        for state, direction in (
            (Game2048State(EMPTY), "UP"),
            (Game2048State(((2, 0, 0, 0), *EMPTY[1:])), "LEFT"),
            (Game2048State(((2, 0, 0, 0), *EMPTY[1:]), over=True), "RIGHT"),
            (Game2048State(((2048, 0, 0, 0), *EMPTY[1:]), won=True), "RIGHT"),
            (
                Game2048State(
                    ((2, 4, 2, 4), (4, 2, 4, 2), (2, 4, 2, 4), (4, 2, 4, 2)), over=True
                ),
                "LEFT",
            ),
        ):
            result = self.model.apply_slide(state, direction)
            self.assertFalse(result.changed)
            self.assertIs(result.state, state)
            outcomes = self.model.spawn_outcomes(result)
            self.assertEqual(len(outcomes), 1)
            self.assertIs(outcomes[0].state, state)
            self.assertEqual(outcomes[0].probability, 1)
            self.assertIs(self.model.sample_spawn(result, 5), state)
        with self.assertRaises(ValueError):
            self.model.apply_slide(Game2048State(EMPTY), "up")

    def test_spawn_mass_position_and_tile_distribution(self):
        rng = random.Random(479)
        for _ in range(80):
            state = Game2048State(
                tuple(
                    tuple(rng.choice((0, 0, 2, 4, 8)) for _ in range(4))
                    for _ in range(4)
                )
            )
            for direction in DIRECTIONS:
                slide = self.model.apply_slide(state, direction)
                outcomes = self.model.spawn_outcomes(slide)
                self.assertAlmostEqual(
                    fsum(outcome.probability for outcome in outcomes), 1
                )
                self.assertEqual(
                    len({outcome.state for outcome in outcomes}), len(outcomes)
                )
                if not slide.changed:
                    continue
                count_empty = sum(v == 0 for row in slide.state.cells for v in row)
                self.assertEqual(len(outcomes), 2 * count_empty)
                mass_by_value = {2: 0.0, 4: 0.0}
                for outcome in outcomes:
                    differences = [
                        (a, b)
                        for row_a, row_b in zip(slide.state.cells, outcome.state.cells)
                        for a, b in zip(row_a, row_b)
                        if a != b
                    ]
                    self.assertEqual(len(differences), 1)
                    before, value = differences[0]
                    self.assertEqual(before, 0)
                    mass_by_value[value] += outcome.probability
                self.assertAlmostEqual(mass_by_value[2], 0.9)
                self.assertAlmostEqual(mass_by_value[4], 0.1)

    def test_spawn_can_end_game_and_merging_neighbor_keeps_it_alive(self):
        state = Game2048State(
            ((0, 4, 8, 16), (4, 8, 16, 32), (8, 16, 32, 64), (16, 32, 64, 128))
        )
        outcomes = self.model.spawn_outcomes(ActionTransition(state, True, 0))
        self.assertTrue(outcomes[0].state.over)  # 2 has no equal neighbor
        self.assertFalse(outcomes[1].state.over)  # 4 can merge with its neighbor
        with self.assertRaises(ValueError):
            self.model.spawn_outcomes(ActionTransition(outcomes[0].state, True, 0))
        with self.assertRaises(ValueError):
            self.model.sample_spawn(ActionTransition(outcomes[0].state, True, 0), 1)

    def test_win_still_spawns_continue_and_restart_are_virtual(self):
        state = Game2048State(((1024, 1024, 0, 0), *EMPTY[1:]), score=100)
        slide = self.model.apply_slide(state, "LEFT")
        self.assertTrue(slide.state.won)
        self.assertTrue(slide.state.terminated)
        outcome = self.model.sample_spawn(slide, 9)
        self.assertEqual(sum(value != 0 for row in outcome.cells for value in row), 2)
        continued = self.model.continue_game(outcome)
        self.assertFalse(continued.terminated)
        self.assertTrue(self.model.apply_slide(continued, "RIGHT").changed)
        self.assertTrue(
            self.model.continue_game(replace(outcome, over=True)).terminated
        )
        # The pinned game sets won only when a merge creates exactly 2048.
        beyond = self.model.apply_slide(
            Game2048State(((2048, 2048, 0, 0), *EMPTY[1:])), "LEFT"
        )
        self.assertEqual(beyond.state.cells[0][0], 4096)
        self.assertFalse(beyond.state.won)
        restarted = self.model.restart(99)
        self.assertEqual(restarted, self.model.restart(99))
        self.assertEqual(restarted.score, 0)
        self.assertFalse(restarted.won or restarted.over or restarted.keep_playing)
        self.assertEqual(sum(v != 0 for row in restarted.cells for v in row), 2)

    def test_seed_reproducibility_distribution_and_global_rng_isolation(self):
        before = random.getstate()
        slide = self.model.apply_slide(
            Game2048State(((0, 2, 0, 0), *EMPTY[1:])), "LEFT"
        )
        self.assertEqual(
            self.model.sample_spawn(slide, 479), self.model.sample_spawn(slide, 479)
        )
        count_four = 0
        for seed in range(2000):
            state = self.model.sample_spawn(slide, seed)
            count_four += sum(map(sum, state.cells)) == 6
        self.assertTrue(150 < count_four < 250)
        self.assertEqual(random.getstate(), before)
        for seed in (None, True, "1", 1.5):
            with self.assertRaises(ValueError):
                self.model.sample_spawn(slide, seed)
            with self.assertRaises(ValueError):
                self.model.restart(seed)

    def test_optional_provider_unknown_game_and_alternative_model(self):
        # A structurally compatible non-board model verifies the contract is generic.
        class CounterModel:
            def apply_action(self, state, action_id):
                return ActionTransition(state + 1, True, 1)

            def chance_outcomes(self, after_action):
                return (ChanceOutcome(after_action.state, 1.0),)

        counter_key = SimulationKey("counter", int)
        board_key = SimulationKey("2048", Game2048State)
        provider = SimulationProvider()
        provider.register(counter_key, CounterModel())
        provider.register(board_key, self.model)
        self.assertEqual(
            provider.get_model(counter_key).apply_action(3, "advance").state, 4
        )
        self.assertEqual(
            provider.get_model(counter_key).chance_outcomes(
                ActionTransition(4, True, 1)
            ),
            (ChanceOutcome(4, 1.0),),
        )
        self.assertIsNone(provider.get_model(SimulationKey("minecraft", str)))
        self.assertIsNone(provider.get_model(SimulationKey("2048", int)))
        self.assertIsNone(SimulationProvider().get_model(board_key))
        with self.assertRaises(ValueError):
            provider.register(counter_key, CounterModel())
        state = Game2048State(((0, 2, 0, 0), *EMPTY[1:]))
        model = provider.get_model(board_key)
        result = model.apply_action(state, "2048-move-left")
        self.assertEqual(result, self.model.apply_slide(state, "LEFT"))
        self.assertEqual(
            model.chance_outcomes(after_action=result),
            self.model.spawn_outcomes(result),
        )
        with self.assertRaises(ValueError):
            model.apply_action(state, "unregistered-action")

    def test_simulation_modules_have_no_game_io_dependency(self):
        root = Path(__file__).resolve().parents[1] / "src/ai_game_player"
        allowed = {
            "__future__",
            "dataclasses",
            "random",
            "typing",
            "ai_game_player.game_2048_state",
            "ai_game_player.transition_model",
            "ai_game_player.simulation_provider",
        }
        for name in (
            "game_2048_state.py",
            "game_2048_transition.py",
            "transition_model.py",
            "simulation_provider.py",
        ):
            tree = ast.parse((root / name).read_text())
            imports = {
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            }
            imports.update(
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            )
            self.assertLessEqual(imports, allowed, name)


if __name__ == "__main__":
    unittest.main()
