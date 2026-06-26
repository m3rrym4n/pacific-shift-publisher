from dataclasses import dataclass


@dataclass(frozen=True)
class NavigationItem:
    label: str
    endpoint: str
    icon: str
    order: int
    group: str | None = None
    badge: str | None = None
    active_endpoints: tuple[str, ...] = ()


NAVIGATION_ITEMS = (
    NavigationItem(
        label="Dashboard",
        endpoint="dashboard",
        icon="layout-dashboard",
        order=10,
    ),
    NavigationItem(
        label="Runs",
        endpoint="runs",
        icon="player-play",
        order=20,
    ),
    NavigationItem(
        label="Logs",
        endpoint="logs",
        icon="terminal-2",
        order=30,
    ),
    NavigationItem(
        label="Manual Upload",
        endpoint="manual_upload",
        icon="upload",
        order=40,
        active_endpoints=("index", "manual_upload", "upload"),
    ),
    NavigationItem(
        label="Settings",
        endpoint="settings",
        icon="settings",
        order=50,
        active_endpoints=(
            "settings",
            "save_azuracast_settings",
            "source_settings",
            "save_source_settings",
            "refresh_source_settings",
        ),
    ),
)


def get_navigation():
    return sorted(NAVIGATION_ITEMS, key=lambda item: item.order)
