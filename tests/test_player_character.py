import json
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import TclError

from ai_game_player.player_character import (
    DEFAULT_PLAYER_PACK,
    PlayerAssetPack,
    PlayerState,
    load_asset_pack,
    load_default_player,
    nearest_scale,
)
from ai_game_player.ui.player_seat import PlayerSeat, load_scaled_photo
from ai_game_player.ui.theme import apply_misk_theme


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

    def test_missing_image_is_reported_without_error(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "pack.json"
            manifest.write_text(
                json.dumps(
                    {
                        "asset_pack_id": "x",
                        "display_name": "X",
                        "player_image": "no.png",
                    }
                ),
                encoding="utf-8",
            )
            pack = load_asset_pack(manifest)
        self.assertFalse(pack.has_image())

    def test_invalid_manifests_are_rejected(self):
        cases = {
            "not-object": [],
            "no-id": {"display_name": "X"},
            "blank-name": {"asset_pack_id": "x", "display_name": " "},
            "smooth-scaling": {
                "asset_pack_id": "x",
                "display_name": "X",
                "image_scaling": "bilinear",
            },
            "unknown-state": {
                "asset_pack_id": "x",
                "display_name": "X",
                "state_messages": {"dance": "!"},
            },
            "absolute-image": {
                "asset_pack_id": "x",
                "display_name": "X",
                "player_image": str(Path(__file__).resolve()),
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "pack.json"
            for name, payload in cases.items():
                with self.subTest(name):
                    manifest.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_asset_pack(manifest)

    def test_image_hash_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "p.png").write_bytes(b"not the recorded image")
            (root / "pack.json").write_text(
                json.dumps(
                    {
                        "asset_pack_id": "x",
                        "display_name": "X",
                        "player_image": "p.png",
                        "player_image_sha256": "0" * 64,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_asset_pack(root / "pack.json")

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

    def test_show_hide_and_destroyed_seat_do_not_raise(self):
        seat = PlayerSeat(self.root, load_default_player())
        seat.set_visible(True)
        self.assertEqual(seat.frame.winfo_manager(), "pack")
        seat.set_visible(False)
        self.assertEqual(seat.frame.winfo_manager(), "")
        self.assertIsNotNone(seat._float_job)
        seat.frame.destroy()
        self.assertIsNone(seat._float_job)
        self.assertFalse(seat.set_state(PlayerState.WARNING))

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
