from ai_game_player.screen_capture import ScreenFrame


class PerceptualHasher:
    """Produces a compact dHash and compares visual similarity without dependencies."""

    _sample_columns = 9
    _sample_rows = 8

    def hash(self, frame: ScreenFrame) -> str:
        samples = [
            self._brightness(frame, column, row)
            for row in range(self._sample_rows)
            for column in range(self._sample_columns)
        ]
        value = 0
        for row in range(self._sample_rows):
            offset = row * self._sample_columns
            for column in range(self._sample_columns - 1):
                value = (value << 1) | int(samples[offset + column] > samples[offset + column + 1])
        return f"{value:016x}"

    def similarity(self, first_hash: str, second_hash: str) -> float:
        first = self._hash_value(first_hash)
        second = self._hash_value(second_hash)
        return 1.0 - ((first ^ second).bit_count() / 64)

    def _brightness(self, frame: ScreenFrame, column: int, row: int) -> int:
        x = min(frame.width - 1, column * frame.width // self._sample_columns)
        y = min(frame.height - 1, row * frame.height // self._sample_rows)
        offset = (y * frame.width + x) * 4
        blue, green, red = frame.bgra[offset : offset + 3]
        return (red * 299 + green * 587 + blue * 114) // 1000

    def _hash_value(self, value: str) -> int:
        if len(value) != 16:
            raise ValueError("perceptual hash must contain 16 hexadecimal characters")
        try:
            return int(value, 16)
        except ValueError as exc:
            raise ValueError("perceptual hash must contain 16 hexadecimal characters") from exc