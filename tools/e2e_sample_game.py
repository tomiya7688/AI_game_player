import argparse
import json
import os
import tkinter as tk
from pathlib import Path


def write_state(path: Path, step: int, total_steps: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"step": step, "total_steps": total_steps, "completed": step >= total_steps}, indent=2),
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
            return
        positions = ((60, 90), (220, 90), (60, 210), (220, 210))
        x, y = positions[step % len(positions)]
        button = (x, y, x + 220, y + 80)
        canvas.create_rectangle(*button, fill="#f5f5f5", outline="#f5f5f5")
        canvas.create_text(x + 110, y + 40, text=f"ADVANCE {step + 1}/{args.steps}", fill="#101010", font=("Segoe UI", 16, "bold"))

    def advance(event) -> None:
        nonlocal step
        if step >= args.steps:
            return
        left, top, right, bottom = button
        if left <= event.x <= right and top <= event.y <= bottom:
            step += 1
            write_state(args.state_file, step, args.steps)
            render()

    canvas.bind("<Button-1>", advance)
    write_state(args.state_file, step, args.steps)
    render()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
