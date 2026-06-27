from urllib.parse import quote

import requests

from azuracast_config import AzuraCastConfigStore, get_azuracast_api_key


DEFAULT_TIMEOUT_SECONDS = 15


def test_azuracast_connection(store=None, http_get=None, timeout=DEFAULT_TIMEOUT_SECONDS):
    store = store or AzuraCastConfigStore()
    config = store.get_config()

    if not config.base_url:
        return _result(store, "missing_base_url", "AzuraCast base URL is not configured.")
    if not config.station_id:
        return _result(store, "missing_station", "AzuraCast Station ID is not configured.")
    if not config.streamer_id:
        return _result(store, "missing_streamer", "AzuraCast Streamer ID is not configured.")

    api_key = get_azuracast_api_key(store)
    if not api_key:
        return _result(store, "missing_api_key", "AzuraCast API key is not configured.")

    station_id = quote(str(config.station_id), safe="")
    streamer_id = quote(str(config.streamer_id), safe="")
    endpoint = (
        f"{config.base_url.rstrip('/')}/api/station/{station_id}"
        f"/streamer/{streamer_id}/broadcasts"
    )
    http_get = http_get or requests.get

    try:
        response = http_get(
            endpoint,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return _result(
            store,
            "network_error",
            f"AzuraCast API connection failed: {exc.__class__.__name__}.",
            endpoint=endpoint,
        )

    status_code = getattr(response, "status_code", None)
    if status_code in {401, 403}:
        return _result(
            store,
            "authentication_failed",
            "AzuraCast API authentication or authorization failed.",
            endpoint=endpoint,
            status_code=status_code,
        )
    if status_code is None or status_code < 200 or status_code >= 300:
        return _result(
            store,
            "api_error",
            f"AzuraCast API returned HTTP {status_code or 'unknown'}.",
            endpoint=endpoint,
            status_code=status_code,
        )

    try:
        payload = response.json()
    except ValueError:
        return _result(
            store,
            "unexpected_response",
            "AzuraCast API returned an unexpected response.",
            endpoint=endpoint,
            status_code=status_code,
        )
    if not isinstance(payload, (dict, list)):
        return _result(
            store,
            "unexpected_response",
            "AzuraCast API returned an unexpected response.",
            endpoint=endpoint,
            status_code=status_code,
        )

    return _result(
        store,
        "success",
        "AzuraCast API connection succeeded.",
        ok=True,
        endpoint=endpoint,
        status_code=status_code,
    )


def _result(store, status, message, *, ok=False, endpoint=None, status_code=None):
    store.record_check_result(message, success=ok)
    return {
        "ok": ok,
        "status": status,
        "message": message,
        "endpoint": endpoint,
        "status_code": status_code,
    }
