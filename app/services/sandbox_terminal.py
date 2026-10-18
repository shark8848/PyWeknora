"""Provider-neutral interactive terminal (PTY) handles."""

from __future__ import annotations


class SandboxTerminalClosed(Exception):
    """The remote shell ended; ``exit_code`` is -1 when unavailable."""

    def __init__(self, exit_code: int = -1) -> None:
        super().__init__(f"terminal exited with code {exit_code}")
        self.exit_code = int(exit_code)


class SandboxTerminal:
    """Blocking byte-stream handle shared by Docker and envd backends."""

    backend = ""
    pid = 0

    @property
    def reattachable(self) -> bool:
        return self.pid > 0

    def read(self, size: int = 32768) -> bytes:
        raise NotImplementedError

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def resize(self, cols: int, rows: int) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError
