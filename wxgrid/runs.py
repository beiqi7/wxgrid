"""Model-run identity."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class Run:
    """One model cycle: UTC date + cycle hour (00/06/12/18)."""

    date: dt.date
    hour: int

    def __post_init__(self) -> None:
        if self.hour not in (0, 6, 12, 18):
            raise ValueError(f"cycle hour must be 00/06/12/18, got {self.hour}")

    @property
    def stamp(self) -> str:
        """``YYYYMMDDHH``."""
        return f"{self.date:%Y%m%d}{self.hour:02d}"

    @classmethod
    def from_stamp(cls, stamp: str) -> "Run":
        """Parse ``YYYYMMDDHH`` (the ``--run`` CLI form) into a :class:`Run`."""
        s = str(stamp).strip()
        if len(s) != 10 or not s.isdigit():
            raise ValueError(f"run stamp must be YYYYMMDDHH, got {stamp!r}")
        return cls(dt.date(int(s[0:4]), int(s[4:6]), int(s[6:8])), int(s[8:10]))

    @property
    def init_time(self) -> dt.datetime:
        return dt.datetime(self.date.year, self.date.month, self.date.day, self.hour, tzinfo=dt.timezone.utc)

    def __str__(self) -> str:  # pragma: no cover - display only
        return self.stamp
