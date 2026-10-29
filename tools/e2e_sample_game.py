import argparse
import json
import os
import tkinter as tk
from pathlib import Path

ADVANCE_BUTTON_WIDTH = 220
ADVANCE_BUTTON_HEIGHT = 80


def write_state(
    path: Path,
    step: int,
    total_steps: int,
    active_control: dict[str, object] | None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "step": step,
                "total_steps": total_steps,
                "completed": step >= total_steps,
                "active_control": active_control,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic GUI target for Kadoka Windows E2E validation")
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--steps", type=int, default=24)
    parser.add_argument("--title", default="Kadoka E2E Sample Game")
    args = parser.parse_args()
    if args.steps < 1:
        raise ValueError("steps must be positive")

    root = tk.Tk()
    root.title(args.title)
    root.geometry("520x360+80+80")
    root.resizable(False, False)
    canvas = tk.Canvas(root, width=520, height=360, bg="#181818", highlightthickness=0)
    canvas.pack(fill=tk.BOTH, expand=True)

    step = 0
    button = (140, 130, 380, 220)

    def render() -> None:
        nonlocal button
        canvas.delete("all")
        completed = step >= args.steps
        if completed:
            button = (120, 125, 400, 225)
            canvas.create_rectangle(*button, fill="#f5f5f5", outline="#f5f5f5")
            canvas.create_text(260, 175, text="COMPLETE", fill="#101010", font=("Segoe UI", 24, "bold"))
            write_state(args.state_file, step, args.steps, None)
            return
        positions = ((60, 90), (220, 90), (60, 210), (220, 210))
        x, y = positions[step % len(positions)]
        button = (x, y, x + ADVANCE_BUTTON_WIDTH, y + ADVANCE_BUTTON_HEIGHT)
        label = f"ADVANCE {step + 1}/{args.steps}"
        canvas.create_rectangle(*button, fill="#f5f5f5", outline="#f5f5f5")
        canvas.create_text(
            x + ADVANCE_BUTTON_WIDTH // 2,
            y + ADVANCE_BUTTON_HEIGHT // 2,
            text=label,
            fill="#101010",
            font=("Segoe UI", 16, "bold"),
        )
        write_state(
            args.state_file,
            step,
            args.steps,
            {
                "action_id": f"advance-{step + 1}",
                "label": label,
                "bbox_size": [ADVANCE_BUTTON_WIDTH, ADVANCE_BUTTON_HEIGHT],
            },
        )

    def advance(event) -> None:
        nonlocal step
        if step >= args.steps:
            return
        left, top, right, bottom = button
        if left <= event.x <= right and top <= event.y <= bottom:
            step += 1
            render()

    canvas.bind("<Button-1>", advance)
    render()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
