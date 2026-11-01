# SPDX-License-Identifier: GPL-3.0-or-later
"""Python-Xlib/XTest input and capture for the test's owned game window."""

from Xlib import X, XK, display, error
from Xlib.ext import xtest
import pygame


class X11Driver:
    """Use an explicit isolated display and verify ownership before input."""

    def __init__(self, name):
        self.display = display.Display(name)
        self.root = self.display.screen().root
        self.window = None
        self.pid = None
        self.held = set()

    def _children(self, window):
        try:
            return window.query_tree().children
        except error.BadWindow:
            return []

    def _pid(self, window):
        atom = self.display.intern_atom("_NET_WM_PID", only_if_exists=True)
        if not atom:
            return None
        try:
            value = window.get_full_property(atom, X.AnyPropertyType)
            if value is not None and value.format == 32 and len(value.value) == 1:
                return int(value.value[0])
        except error.BadWindow:
            # SDL may replace a temporary window while starting the renderer.
            return None
        return None

    def find_owned_window(self, pid):
        queue = list(self._children(self.root))
        while queue:
            window = queue.pop(0)
            if self._pid(window) == pid:
                self.window, self.pid = window, pid
                return window.id
            queue.extend(self._children(window))
        return None

    def focus(self):
        assert self.window is not None and self._pid(self.window) == self.pid
        self.window.map()
        self.window.configure(stack_mode=X.Above)
        self.window.set_input_focus(X.RevertToParent, X.CurrentTime)
        self.display.sync()
        focused = self.display.get_input_focus().focus
        assert getattr(focused, "id", focused) == self.window.id

    def key(self, name, down):
        if down:
            self.focus()
        code = self.display.keysym_to_keycode(XK.string_to_keysym(name))
        assert code
        xtest.fake_input(self.display, X.KeyPress if down else X.KeyRelease, code)
        if down:
            self.held.add(name)
        else:
            self.held.discard(name)
        self.display.flush()

    def click(self, x, y):
        self.focus()
        position = self.root.translate_coords(self.window, x, y)
        xtest.fake_input(self.display, X.MotionNotify, x=position.x, y=position.y)
        xtest.fake_input(self.display, X.ButtonPress, 1)
        xtest.fake_input(self.display, X.ButtonRelease, 1)
        self.display.flush()

    def capture(self, path):
        assert self._pid(self.window) == self.pid
        geometry = self.window.get_geometry()
        image = self.window.get_image(
            0, 0, geometry.width, geometry.height, X.ZPixmap, 0xFFFFFFFF
        )
        assert (
            image.depth == 24
            and self.display.display.info.image_byte_order == X.LSBFirst
        )
        pixels = bytearray(image.data)
        assert len(pixels) == geometry.width * geometry.height * 4
        pixels[3::4] = b"\xff" * (geometry.width * geometry.height)
        surface = pygame.image.frombytes(
            bytes(pixels), (geometry.width, geometry.height), "BGRA"
        )
        pygame.image.save(surface, str(path))

    def close(self):
        for name in tuple(self.held):
            self.key(name, False)
        self.display.close()
