import hashlib
import json
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import TclError, ttk

from ai_game_player.player_character import (
    DEFAULT_PLAYER_PACK,
    PlayerAssetPack,
    PlayerState,
    load_asset_pack,
    load_default_player,
    nearest_scale,
)
from ai_game_player.ui.player_seat import PlayerSeat, load_scaled_photo
from ai_game_player.ui.theme import PALETTE, apply_misk_theme


class PlayerAssetPackTests(unittest.TestCase):
    def test_bundled_wise_misk_pack_is_the_development_default(self):
        pack = load_default_player()
        self.assertEqual(pack.asset_pack_id, "tomiya.wise-misk")
        self.assertEqual(pack.display_name, "賢者ミスク")
        self.assertEqual(pack.profiles, ("development",))
        self.assertEqual(pack.license_name, "Tomiya Character License v1.0.1")
        self.assertTrue(pack.has_image())
        self.assertEqual(set(pack.state_messages), set(PlayerState))

    def test_missing_message_falls_back_to_unknown_then_empty(self):
        pack = PlayerAssetPack(
            "p", "P", state_messages={PlayerState.UNKNOWN: "unknown"}
        )
        self.assertEqual(pack.message_for(PlayerState.ACT), "unknown")
        self.assertEqual(PlayerAssetPack("p", "P").message_for(PlayerState.ACT), "")

    def write_pack(self, root, **fields):
        manifest = {"schema_version": 1, "asset_pack_id": "x", "display_name": "X"}
        manifest.update(fields)
        path = Path(root) / "pack.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_missing_image_without_hash_falls_back_to_text(self):
        with tempfile.TemporaryDirectory() as directory:
            pack = load_asset_pack(self.write_pack(directory, player_image="no.png"))
        self.assertFalse(pack.has_image())

    def test_invalid_manifests_are_rejected(self):
        cases = {
            "no-schema": {"schema_version": None},
            "future-schema": {"schema_version": 2},
            "no-id": {"asset_pack_id": None},
            "blank-name": {"display_name": " "},
            "smooth-scaling": {"image_scaling": "bilinear"},
            "unknown-state": {"state_messages": {"dance": "!"}},
            "absolute-image": {"player_image": str(Path(__file__).resolve())},
            "empty-image": {"player_image": ""},
            "outside-pack": {"player_image": "../outside.png"},
            "null-license": {"license_name": None},
            "object-source": {"source": {"url": "x"}},
        }
        with tempfile.TemporaryDirectory() as directory:
            for name, fields in cases.items():
                with self.subTest(name):
                    with self.assertRaises(ValueError):
                        load_asset_pack(self.write_pack(directory, **fields))
            not_object = Path(directory) / "list.json"
            not_object.write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_asset_pack(not_object)

    def test_recorded_image_hash_must_match_an_existing_image(self):
        image = b"recorded image bytes"
        digest = hashlib.sha256(image).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "p.png").write_bytes(image)
            for recorded in (digest, digest.upper()):
                with self.subTest(recorded=recorded[:4]):
                    pack = load_asset_pack(
                        self.write_pack(
                            directory,
                            player_image="p.png",
                            player_image_sha256=recorded,
                        )
                    )
                    self.assertTrue(pack.has_image())
            for name, fields in {
                "mismatch": {"player_image": "p.png", "player_image_sha256": "0" * 64},
                "malformed": {"player_image": "p.png", "player_image_sha256": "abc"},
                "missing": {"player_image": "no.png", "player_image_sha256": digest},
            }.items():
                with self.subTest(name):
                    with self.assertRaises(ValueError):
                        load_asset_pack(self.write_pack(directory, **fields))

    def test_state_messages_are_read_only(self):
        pack = load_default_player()
        with self.assertRaises(TypeError):
            pack.state_messages[PlayerState.ACT] = "changed"

    def test_unreadable_default_pack_falls_back_to_generic_player(self):
        with tempfile.TemporaryDirectory() as directory:
            pack = load_default_player(Path(directory) / "missing.json")
        self.assertEqual(pack.asset_pack_id, "fallback.player")
        self.assertFalse(pack.has_image())

    def test_nearest_scale_supports_org_upscale_and_large_downscale(self):
        self.assertEqual(nearest_scale(64, 128), (2, 1))
        self.assertEqual(nearest_scale(64, 100), (1, 1))
        self.assertEqual(nearest_scale(1920, 128), (1, 15))
        self.assertEqual(nearest_scale(1920, 100), (1, 20))
        with self.assertRaises(ValueError):
            nearest_scale(0, 10)


class PlayerSeatTkTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except TclError as exc:
            self.skipTest(f"Tk desktop is unavailable: {exc}")
        self.root.withdraw()
        apply_misk_theme(self.root)

    def tearDown(self):
        self.root.destroy()

    def test_wise_misk_sits_with_nearest_scaled_image_and_reacts_to_state(self):
        seat = PlayerSeat(self.root, load_default_player(), animate=False)
        seat.frame.pack()
        self.assertEqual((seat._photo.width(), seat._photo.height()), (128, 128))
        self.assertEqual(seat.name_var.get(), "賢者ミスク")
        self.assertIn("Tomiya Character License", seat.license_var.get())
        for state in PlayerState:
            with self.subTest(state=state):
                self.assertTrue(seat.set_state(state))
                self.assertEqual(seat.state, state)
                self.assertEqual(
                    seat.message_var.get(), seat.occupant.message_for(state)
                )

    def test_occupant_switch_and_missing_image_use_text_fallback(self):
        seat = PlayerSeat(self.root, load_default_player(), animate=False)
        seat.set_state(PlayerState.ACT)
        self.assertTrue(seat.set_occupant(PlayerAssetPack("other", "Super Icon")))
        self.assertIsNone(seat._photo)
        self.assertEqual(seat.name_var.get(), "Super Icon")
        self.assertEqual(seat.state, PlayerState.ACT)
        self.assertEqual(seat.message_var.get(), "")
        self.assertEqual(seat.bubble.winfo_manager(), "")
        texts = [
            seat.canvas.itemcget(item, "text")
            for item in seat.canvas.find_withtag("occupant")
        ]
        self.assertEqual(texts, ["S"])
        self.assertTrue(seat.set_occupant(load_default_player()))
        self.assertEqual(
            seat.frame.pack_slaves()[-2:], [seat.bubble, seat.license_label]
        )

    def test_hide_and_show_restore_the_original_pack_position(self):
        above = ttk.Label(self.root, text="above")
        above.pack()
        seat = PlayerSeat(self.root, load_default_player(), animate=False)
        seat.frame.pack(fill=tk.X, pady=(12, 0))
        below = ttk.Label(self.root, text="below")
        below.pack()
        self.assertTrue(seat.set_visible(False))
        self.assertEqual(seat.frame.winfo_manager(), "")
        self.assertTrue(seat.set_visible(True))
        self.assertEqual(self.root.pack_slaves(), [above, seat.frame, below])
        self.assertEqual(tuple(seat.frame.pack_info()["pady"]), (12, 0))

    def test_stop_and_warning_return_the_floating_image_to_rest(self):
        seat = PlayerSeat(self.root, load_default_player(), animate=False)
        rest = seat.canvas.coords("occupant")
        seat._float()
        self.assertNotEqual(seat.canvas.coords("occupant"), rest)
        for state in (PlayerState.STOP, PlayerState.WARNING):
            with self.subTest(state=state):
                seat.set_state(state)
                seat._float()
                self.assertEqual(seat.canvas.coords("occupant"), rest)

    def test_destroyed_seat_reports_failure_without_raising(self):
        seat = PlayerSeat(self.root, load_default_player())
        self.assertIsNotNone(seat._float_job)
        seat.frame.destroy()
        self.root.update()
        self.assertIsNone(seat._float_job)
        self.assertFalse(seat.set_state(PlayerState.WARNING))
        self.assertFalse(seat.set_occupant(load_default_player()))
        self.assertFalse(seat.set_visible(True))

    def test_stop_button_style_is_red_only_while_enabled(self):
        style = ttk.Style(self.root)
        self.assertEqual(style.lookup("Stop.TButton", "background"), PALETTE["danger"])
        self.assertEqual(
            style.lookup("Stop.TButton", "background", ["disabled"]), PALETTE["raised"]
        )
        self.assertEqual(
            style.lookup("Stop.TButton", "foreground", ["disabled"]), PALETTE["muted"]
        )

    def test_unreadable_image_returns_none(self):
        with tempfile.TemporaryDirectory() as directory:
            broken = Path(directory) / "broken.png"
            broken.write_bytes(b"not a png")
            self.assertIsNone(load_scaled_photo(self.root, broken, 32))
        self.assertIsNotNone(
            load_scaled_photo(
                self.root, DEFAULT_PLAYER_PACK.parent / "wise_misk.png", 32
            )
        )


if __name__ == "__main__":
    unittest.main()
