"""Adapter registry: highest sniff score wins."""

from __future__ import annotations

from obd_engine.cli.envelope import ObdError
from obd_engine.ingestion.adapters.base import Adapter, GenericDelimitedAdapter
from obd_engine.ingestion.adapters.generic_excel import GenericExcelAdapter
from obd_engine.ingestion.adapters.long_format import LongFormatAdapter
from obd_engine.ingestion.adapters.torque_pro import TorqueProAdapter
from obd_engine.ingestion.discovery.sniff import FileProfile

ADAPTERS: list[Adapter] = [TorqueProAdapter(), LongFormatAdapter(), GenericDelimitedAdapter(), GenericExcelAdapter()]


def select_adapter(profile: FileProfile) -> tuple[Adapter, float]:
    scored = sorted(((a.sniff(profile), a) for a in ADAPTERS), key=lambda x: -x[0])
    score, adapter = scored[0]
    if score <= 0:
        raise ObdError("NO_ADAPTER", "No adapter understands this file.")
    return adapter, score


def get_adapter(adapter_id: str) -> Adapter:
    for a in ADAPTERS:
        if a.id == adapter_id:
            return a
    raise ObdError("NO_ADAPTER", f"Unknown adapter {adapter_id}")
