"""Strict TOML configuration loading for the Singapore rail station dataset."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import (
    ContractError,
    exact_bool,
    exact_float,
    exact_int,
    exact_str,
    https_endpoint,
    require_fields,
    safe_repo_path,
    string_list,
)


class ConfigError(ContractError):
    """Raised when ``pipeline.toml`` is missing, malformed, or unsafe."""


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """Identity of the project and its output schema."""

    name: str
    schema_version: int


@dataclass(frozen=True, slots=True)
class PathsConfig:
    """Repository-owned paths used by pipeline runs."""

    repo_root: Path
    runs_dir: Path
    station_catalog: Path
    stage1_reviewed_decisions: Path
    stage1_approved_baseline: Path
    stage2_approved_baseline: Path
    stage2_reviewed_coordinates: Path
    stage2_coordinate_ledger: Path
    stage2_trusted_onemap_baseline: Path
    stage2_trusted_supplemental_baseline: Path
    stage3_approved_geometry_baseline: Path


@dataclass(frozen=True, slots=True)
class LocaleConfig:
    """Timezone used to interpret service dates and report timestamps."""

    timezone: str

    @property
    def zoneinfo(self) -> ZoneInfo:
        """Return the configured IANA timezone."""

        return ZoneInfo(self.timezone)


@dataclass(frozen=True, slots=True)
class InventoryConfig:
    """Expected operational inventory invariants."""

    expected_station_count: int


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """A source endpoint and the only hosts it may use."""

    endpoint: str
    allowed_hosts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourcesConfig:
    """Authoritative source endpoints used by later collection phases."""

    lta_gtfs: SourceConfig
    lta_bus_stops: SourceConfig
    sgwiki_api: SourceConfig
    sgwiki_mapdata: SourceConfig
    osm_overpass: SourceConfig
    osm_api: SourceConfig


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    """Bounded retry, timeout, and rate settings for later collectors."""

    timeout_seconds: float
    max_attempts: int
    initial_backoff_seconds: float
    maximum_backoff_seconds: float
    minimum_interval_seconds: float


@dataclass(frozen=True, slots=True)
class ReleaseConfig:
    """Release gates evaluated after collection and semantic validation."""

    warnings_require_review: bool
    errors_block_release: bool
    criticals_abort_run: bool
    max_station_count_delta: int
    max_unmatched_operational_stations: int
    max_missing_sgwiki_pages: int
    max_unresolved_exit_coordinates: int
    coordinate_movement_review_metres: float


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """Complete validated pipeline configuration."""

    project: ProjectConfig
    paths: PathsConfig
    locale: LocaleConfig
    inventory: InventoryConfig
    sources: SourcesConfig
    network: NetworkConfig
    release: ReleaseConfig
    config_path: Path
    config_sha256: str


def _source(value: object, context: str) -> SourceConfig:
    table = require_fields(value, {"endpoint", "allowed_hosts"}, context)
    hosts = string_list(table["allowed_hosts"], f"{context}.allowed_hosts", nonempty=True)
    for index, host in enumerate(hosts):
        if "://" in host or "/" in host or host.lower() != host:
            raise ConfigError(
                f"{context}.allowed_hosts[{index}]: expected a lowercase hostname without scheme or path"
            )
    endpoint = https_endpoint(table["endpoint"], hosts, f"{context}.endpoint")
    return SourceConfig(endpoint=endpoint, allowed_hosts=hosts)


def _load_toml(path: Path) -> tuple[dict[str, object], str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read configuration: {exc}") from exc
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    if type(parsed) is not dict:
        raise ConfigError(f"{path}: expected a TOML document")
    return parsed, sha256(raw).hexdigest()


def _load_config(path: str | Path) -> PipelineConfig:
    """Load and strictly validate a pipeline TOML file.

    The configuration must be located in the repository's ``config`` directory.
    Configured paths are interpreted relative to the repository root and may not
    escape it, including through existing symlinks.
    """

    config_path = Path(path).expanduser().resolve()
    if config_path.parent.name != "config":
        raise ConfigError(f"{config_path}: configuration must be inside a config directory")
    repo_root = config_path.parent.parent.resolve()
    parsed, config_sha256 = _load_toml(config_path)
    document = require_fields(
        parsed,
        {"project", "paths", "locale", "inventory", "sources", "network", "release"},
        "pipeline",
    )

    project_table = require_fields(document["project"], {"name", "schema_version"}, "project")
    project = ProjectConfig(
        name=exact_str(project_table["name"], "project.name"),
        schema_version=exact_int(project_table["schema_version"], "project.schema_version", minimum=1),
    )
    if project.name != "sg-rail-station-data":
        raise ConfigError("project.name: unsupported project identity")
    if project.schema_version != 1:
        raise ConfigError(
            f"project.schema_version: unsupported version {project.schema_version}; expected 1"
        )

    paths_table = require_fields(
        document["paths"],
        {
            "runs_dir",
            "station_catalog",
            "stage1_reviewed_decisions",
            "stage1_approved_baseline",
            "stage2_approved_baseline",
            "stage2_reviewed_coordinates",
            "stage2_coordinate_ledger",
            "stage2_trusted_onemap_baseline",
            "stage2_trusted_supplemental_baseline",
            "stage3_approved_geometry_baseline",
        },
        "paths",
    )
    paths = PathsConfig(
        repo_root=repo_root,
        runs_dir=safe_repo_path(paths_table["runs_dir"], repo_root, "paths.runs_dir"),
        station_catalog=safe_repo_path(
            paths_table["station_catalog"], repo_root, "paths.station_catalog"
        ),
        stage1_reviewed_decisions=safe_repo_path(
            paths_table["stage1_reviewed_decisions"],
            repo_root,
            "paths.stage1_reviewed_decisions",
        ),
        stage1_approved_baseline=safe_repo_path(
            paths_table["stage1_approved_baseline"],
            repo_root,
            "paths.stage1_approved_baseline",
        ),
        stage2_approved_baseline=safe_repo_path(
            paths_table["stage2_approved_baseline"],
            repo_root,
            "paths.stage2_approved_baseline",
        ),
        stage2_reviewed_coordinates=safe_repo_path(
            paths_table["stage2_reviewed_coordinates"],
            repo_root,
            "paths.stage2_reviewed_coordinates",
        ),
        stage2_coordinate_ledger=safe_repo_path(
            paths_table["stage2_coordinate_ledger"],
            repo_root,
            "paths.stage2_coordinate_ledger",
        ),
        stage2_trusted_onemap_baseline=safe_repo_path(
            paths_table["stage2_trusted_onemap_baseline"],
            repo_root,
            "paths.stage2_trusted_onemap_baseline",
        ),
        stage2_trusted_supplemental_baseline=safe_repo_path(
            paths_table["stage2_trusted_supplemental_baseline"],
            repo_root,
            "paths.stage2_trusted_supplemental_baseline",
        ),
        stage3_approved_geometry_baseline=safe_repo_path(
            paths_table["stage3_approved_geometry_baseline"],
            repo_root,
            "paths.stage3_approved_geometry_baseline",
        ),
    )

    locale_table = require_fields(document["locale"], {"timezone"}, "locale")
    timezone = exact_str(locale_table["timezone"], "locale.timezone")
    if timezone != "Asia/Singapore":
        raise ConfigError("locale.timezone: must be Asia/Singapore")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"locale.timezone: unknown IANA timezone {timezone!r}") from exc
    locale = LocaleConfig(timezone=timezone)

    inventory_table = require_fields(
        document["inventory"], {"expected_station_count"}, "inventory"
    )
    inventory = InventoryConfig(
        expected_station_count=exact_int(
            inventory_table["expected_station_count"],
            "inventory.expected_station_count",
            minimum=1,
        )
    )

    sources_table = require_fields(
        document["sources"],
        {
            "lta_gtfs", "lta_bus_stops", "sgwiki_api", "sgwiki_mapdata",
            "osm_overpass", "osm_api",
        },
        "sources",
    )
    sources = SourcesConfig(
        lta_gtfs=_source(sources_table["lta_gtfs"], "sources.lta_gtfs"),
        lta_bus_stops=_source(sources_table["lta_bus_stops"], "sources.lta_bus_stops"),
        sgwiki_api=_source(sources_table["sgwiki_api"], "sources.sgwiki_api"),
        sgwiki_mapdata=_source(sources_table["sgwiki_mapdata"], "sources.sgwiki_mapdata"),
        osm_overpass=_source(sources_table["osm_overpass"], "sources.osm_overpass"),
        osm_api=_source(sources_table["osm_api"], "sources.osm_api"),
    )

    network_table = require_fields(
        document["network"],
        {
            "timeout_seconds",
            "max_attempts",
            "initial_backoff_seconds",
            "maximum_backoff_seconds",
            "minimum_interval_seconds",
        },
        "network",
    )
    network = NetworkConfig(
        timeout_seconds=exact_float(
            network_table["timeout_seconds"], "network.timeout_seconds", minimum=0.1
        ),
        max_attempts=exact_int(
            network_table["max_attempts"], "network.max_attempts", minimum=1, maximum=10
        ),
        initial_backoff_seconds=exact_float(
            network_table["initial_backoff_seconds"],
            "network.initial_backoff_seconds",
            minimum=0.0,
        ),
        maximum_backoff_seconds=exact_float(
            network_table["maximum_backoff_seconds"],
            "network.maximum_backoff_seconds",
            minimum=0.0,
        ),
        minimum_interval_seconds=exact_float(
            network_table["minimum_interval_seconds"],
            "network.minimum_interval_seconds",
            minimum=0.0,
        ),
    )
    if network.maximum_backoff_seconds < network.initial_backoff_seconds:
        raise ConfigError(
            "network.maximum_backoff_seconds: must be greater than or equal to "
            "network.initial_backoff_seconds"
        )

    release_table = require_fields(
        document["release"],
        {
            "warnings_require_review",
            "errors_block_release",
            "criticals_abort_run",
            "max_station_count_delta",
            "max_unmatched_operational_stations",
            "max_missing_sgwiki_pages",
            "max_unresolved_exit_coordinates",
            "coordinate_movement_review_metres",
        },
        "release",
    )
    release = ReleaseConfig(
        warnings_require_review=exact_bool(
            release_table["warnings_require_review"], "release.warnings_require_review"
        ),
        errors_block_release=exact_bool(
            release_table["errors_block_release"], "release.errors_block_release"
        ),
        criticals_abort_run=exact_bool(
            release_table["criticals_abort_run"], "release.criticals_abort_run"
        ),
        max_station_count_delta=exact_int(
            release_table["max_station_count_delta"],
            "release.max_station_count_delta",
            minimum=0,
        ),
        max_unmatched_operational_stations=exact_int(
            release_table["max_unmatched_operational_stations"],
            "release.max_unmatched_operational_stations",
            minimum=0,
        ),
        max_missing_sgwiki_pages=exact_int(
            release_table["max_missing_sgwiki_pages"],
            "release.max_missing_sgwiki_pages",
            minimum=0,
        ),
        max_unresolved_exit_coordinates=exact_int(
            release_table["max_unresolved_exit_coordinates"],
            "release.max_unresolved_exit_coordinates",
            minimum=0,
        ),
        coordinate_movement_review_metres=exact_float(
            release_table["coordinate_movement_review_metres"],
            "release.coordinate_movement_review_metres",
            minimum=0.0,
        ),
    )
    if not release.warnings_require_review:
        raise ConfigError("release.warnings_require_review: must be true for fail-loud review policy")
    if not release.errors_block_release:
        raise ConfigError("release.errors_block_release: must be true for fail-loud release policy")
    if not release.criticals_abort_run:
        raise ConfigError("release.criticals_abort_run: must be true for fail-loud run policy")

    return PipelineConfig(
        project=project,
        paths=paths,
        locale=locale,
        inventory=inventory,
        sources=sources,
        network=network,
        release=release,
        config_path=config_path,
        config_sha256=config_sha256,
    )


def load_config(path: str | Path) -> PipelineConfig:
    """Load a pipeline configuration, normalising all contract failures."""

    try:
        return _load_config(path)
    except ConfigError:
        raise
    except ContractError as exc:
        raise ConfigError(str(exc)) from exc


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "pipeline.toml"


def load_default_config() -> PipelineConfig:
    """Load the repository's standard pipeline configuration."""

    return load_config(DEFAULT_CONFIG_PATH)
