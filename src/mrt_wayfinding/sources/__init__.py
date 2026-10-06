"""External source collectors and their strict source contracts."""

from .lta_bus_stops import (
    BusStop,
    BusStopsCollectionResult,
    BusStopsSourceError,
    HttpResponse,
    collect_lta_bus_stops,
    select_required_bus_stops,
)

__all__ = [
    "BusStop",
    "BusStopsCollectionResult",
    "BusStopsSourceError",
    "HttpResponse",
    "collect_lta_bus_stops",
    "select_required_bus_stops",
]
