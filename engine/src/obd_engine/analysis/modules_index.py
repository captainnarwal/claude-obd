"""Importing this module registers all analysis modules."""

from obd_engine.analysis.generic import anomalies, correlation, statistics, time_series  # noqa: F401
from obd_engine.analysis.modules import data_quality  # noqa: F401
