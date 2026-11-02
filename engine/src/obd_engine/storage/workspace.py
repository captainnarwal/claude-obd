"""Per-folder .obd workspace and obd-reports output folder (design doc section 6.1)."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Workspace:
    root: Path  # the user's working folder

    @classmethod
    def current(cls) -> "Workspace":
        override = os.environ.get("OBD_WORKSPACE")
        return cls(Path(override).resolve() if override else Path.cwd().resolve())

    @property
    def obd_dir(self) -> Path:
        return self.root / ".obd"

    @property
    def catalog_path(self) -> Path:
        return self.obd_dir / "catalog.sqlite"

    @property
    def datasets_dir(self) -> Path:
        return self.obd_dir / "datasets"

    @property
    def tmp_dir(self) -> Path:
        return self.obd_dir / "tmp"

    @property
    def reports_dir(self) -> Path:
        return self.root / "obd-reports"

    @property
    def charts_dir(self) -> Path:
        return self.reports_dir / "charts"

    def dataset_dir(self, dataset_id: str) -> Path:
        return self.datasets_dir / dataset_id

    def ensure(self) -> None:
        self.obd_dir.mkdir(exist_ok=True)
        gi = self.obd_dir / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n", encoding="utf-8")
        self.datasets_dir.mkdir(exist_ok=True)

    def ensure_reports(self) -> None:
        self.charts_dir.mkdir(parents=True, exist_ok=True)

    def make_tmp(self) -> Path:
        self.ensure()
        self.tmp_dir.mkdir(exist_ok=True)
        return Path(tempfile.mkdtemp(dir=self.tmp_dir))

    def exists(self) -> bool:
        return self.obd_dir.exists()

    def wipe(self) -> None:
        if self.obd_dir.exists():
            shutil.rmtree(self.obd_dir)
