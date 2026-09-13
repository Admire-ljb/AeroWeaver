"""Own MPE2's process-global SDL lifetime without touching task dynamics."""

import threading
import pygame
from pettingzoo.utils.wrappers.base_parallel import BaseParallelWrapper

_SDL_LOCK = threading.RLock()
_LIVE_ENVS = 0


class ManagedMPEEnvironment(BaseParallelWrapper):
    def __init__(self, env):
        global _LIVE_ENVS
        super().__init__(env)
        self._closed = False
        _LIVE_ENVS += 1

    def close(self):
        global _LIVE_ENVS
        with _SDL_LOCK:
            if self._closed:
                return
            self._closed = True
            raw = self.env.unwrapped
            # Font destructors must run BEFORE pygame/freetype shutdown. Native
            # SimpleEnv.close retains the font, which may be garbage-collected
            # during a later episode after its FreeType library was destroyed.
            raw.game_font = None
            raw.screen = None
            self.env.close()
            _LIVE_ENVS -= 1
            if _LIVE_ENVS == 0:
                pygame.quit()


def create_managed_environment(factory, **kwargs):
    with _SDL_LOCK:
        return ManagedMPEEnvironment(factory(**kwargs))
