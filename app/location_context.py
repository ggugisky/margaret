from __future__ import annotations

from typing import Any


def normalize_location(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None

    lat = raw.get("lat", raw.get("latitude"))
    lng = raw.get("lng", raw.get("longitude"))
    if lat is None or lng is None:
        return None

    try:
        normalized: dict[str, Any] = {
            "lat": float(lat),
            "lng": float(lng),
        }
    except (TypeError, ValueError):
        return None

    for source_key, target_key in (
        ("accuracy", "accuracy_m"),
        ("accuracy_m", "accuracy_m"),
        ("altitude", "altitude_m"),
        ("altitude_m", "altitude_m"),
        ("heading", "heading"),
        ("speed", "speed_mps"),
        ("speed_mps", "speed_mps"),
        ("timestamp", "timestamp"),
    ):
        value = raw.get(source_key)
        if value is not None:
            normalized[target_key] = value

    return normalized


def text_with_location_context(text: str, location: Any) -> str:
    normalized = normalize_location(location)
    if not normalized:
        return text

    lines = [
        "",
        "",
        "[Voice GPS context]",
        f"latitude: {normalized['lat']}",
        f"longitude: {normalized['lng']}",
    ]
    if normalized.get("accuracy_m") is not None:
        lines.append(f"accuracy_m: {normalized['accuracy_m']}")
    if normalized.get("altitude_m") is not None:
        lines.append(f"altitude_m: {normalized['altitude_m']}")
    if normalized.get("heading") is not None:
        lines.append(f"heading: {normalized['heading']}")
    if normalized.get("speed_mps") is not None:
        lines.append(f"speed_mps: {normalized['speed_mps']}")
    if normalized.get("timestamp") is not None:
        lines.append(f"timestamp: {normalized['timestamp']}")
    lines.append("Use this location only when it is relevant to the user request.")
    return text + "\n".join(lines)
