"""Каталог IANA-зон для выбора пояса (летнее/зимнее время через tzdata)."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, available_timezones

# Первый сегмент идентификатора → группа в UI
REGION_LABEL_RU: dict[str, str] = {
    "Africa": "Африка",
    "America": "Америка",
    "Antarctica": "Антарктида",
    "Arctic": "Арктика",
    "Asia": "Азия",
    "Atlantic": "Атлантика",
    "Australia": "Австралия",
    "Europe": "Европа",
    "Indian": "Индийский океан",
    "Pacific": "Тихий океан",
    "UTC": "UTC",
    "Etc": "Другие (Etc, без истории DST)",
    "Other": "Прочее",
}


def _format_offset_at(dt: datetime) -> str:
    """Смещение от UTC для осознанного локального времени (с учётом DST)."""
    off = dt.utcoffset()
    if off is None:
        return "UTC"
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    h, rest = divmod(total, 3600)
    m, _ = divmod(rest, 60)
    if m:
        return f"UTC{sign}{h}:{m:02d}"
    return f"UTC{sign}{h}"


def _build_region_to_zones() -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for name in sorted(available_timezones()):
        if name.startswith("posix/") or name.startswith("right/"):
            continue
        parts = name.split("/", 1)
        region = parts[0] if len(parts) > 1 else "Other"
        groups.setdefault(region, []).append(name)
    return groups


_REGION_TO_ZONES: dict[str, list[str]] | None = None


def _regions() -> dict[str, list[str]]:
    global _REGION_TO_ZONES
    if _REGION_TO_ZONES is None:
        _REGION_TO_ZONES = _build_region_to_zones()
    return _REGION_TO_ZONES


def build_timezone_catalog(now_utc: datetime | None = None) -> list[dict]:
    """
    Группы для <select>: region_label + zones[{id, label}].
    label включает актуальное смещение (меняется при DST).
    """
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    regions = _regions()
    out: list[dict] = []
    for region in sorted(regions.keys(), key=lambda r: (REGION_LABEL_RU.get(r, r), r)):
        zones_out: list[dict] = []
        for zid in regions[region]:
            try:
                zi = ZoneInfo(zid)
                local = now.astimezone(zi)
                off_l = _format_offset_at(local)
                zones_out.append({"id": zid, "label": f"{zid} — сейчас {off_l}"})
            except Exception:
                continue
        if zones_out:
            out.append(
                {
                    "region": region,
                    "region_label": REGION_LABEL_RU.get(region, region),
                    "zones": zones_out,
                }
            )
    return out
