"""Bounded "latest value wins" slot. Never accumulates old frames."""
from __future__ import annotations

import threading
from typing import Generic, TypeVar

T = TypeVar("T")


class LatestSlot(Generic[T]):
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._item: T | None = None
        self._version = 0
        self.overwritten = 0   # items replaced before anyone consumed them

    def put(self, item: T) -> None:
        with self._cond:
            if self._item is not None and self._unconsumed:
                self.overwritten += 1
            self._item = item
            self._version += 1
            self._unconsumed = True
            self._cond.notify_all()

    _unconsumed = False

    def peek(self) -> tuple[T | None, int]:
        """Newest item and its version, without consuming (several readers may peek)."""
        with self._cond:
            return self._item, self._version

    def take(self) -> T | None:
        with self._cond:
            self._unconsumed = False
            return self._item

    def wait_newer(self, version: int, timeout: float | None = None) -> tuple[T | None, int]:
        """Block until an item newer than `version` arrives (or timeout); returns (item, version)."""
        with self._cond:
            self._cond.wait_for(lambda: self._version > version, timeout)
            self._unconsumed = False
            return self._item, self._version

    def clear(self) -> None:
        with self._cond:
            self._item = None
            self._unconsumed = False

    @property
    def version(self) -> int:
        return self._version
