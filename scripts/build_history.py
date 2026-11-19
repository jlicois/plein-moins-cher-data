#!/usr/bin/env python3
"""Build rolling station/fuel price history from official French annual archives."""
from __future__ import annotations

import datetime as dt
import gzip
import io
import json
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "shards"
PUBLIC = ROOT / "public" / "history"
SHARDS = 64
KEEP_DAYS = 400
ANNUAL_URL = "https://donnees.roulez-eco.fr/opendata/annee/{}"
DAILY_URL = "https://donnees.roulez-eco.fr/opendata/jour/{}"


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def shard_index(station_id: str) -> int:
    try:
        return int(station_id) % SHARDS
    except ValueError:
        return sum(station_id.encode("utf-8")) % SHARDS


def load_shards() -> list[dict]:
    DATA.mkdir(parents=True, exist_ok=True)
    result = []
    for i in range(SHARDS):
        path = DATA / f"{i:02d}.json"
        if path.exists():
            try:
                result.append(json.loads(path.read_text(encoding="utf-8")).get("stations", {}))
                continue
            except Exception:
                print(f"WARNING: shard illisible, recréation: {path}")
        result.append({})
    return result


def add_record(shards: list[dict], station_id: str, fuel: str, stamp: int, value: float) -> None:
    if not station_id or not fuel or stamp <= 0 or value <= 0:
        return
    station = shards[shard_index(station_id)].setdefault(station_id, {})
    records = station.setdefault(fuel, [])
    records.append([stamp, round(value, 3)])


def parse_price_records(payload: bytes, shards: list[dict], cutoff: int) -> int:
    count = 0
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        xml_names = [n for n in archive.namelist() if n.lower().endswith(".xml")]
        if not xml_names:
            raise RuntimeError("Aucun XML dans l'archive officielle")
        for name in xml_names:
            with archive.open(name) as stream:
                for _, elem in ET.iterparse(stream, events=("end",)):
                    if local_name(elem.tag) == "pdv":
                        station_id = elem.attrib.get("id", "").strip()
                        if station_id:
                            for child in elem:
                                if local_name(child.tag) != "prix":
                                    continue
                                fuel = child.attrib.get("nom", "").strip()
                                raw = child.attrib.get("valeur", "").strip().replace(",", ".")
                                updated = child.attrib.get("maj", "").strip()
                                try:
                                    value = float(raw)
                                    date = dt.datetime.strptime(updated, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
                                    stamp = int(date.timestamp())
                                except (TypeError, ValueError):
                                    continue
                                if stamp >= cutoff and value > 0:
                                    add_record(shards, station_id, fuel, stamp, value)
                                    count += 1
                        elem.clear()
    return count


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "PleinMoinsCher-History/2.0"})
    with urllib.request.urlopen(request, timeout=180) as response:
        return response.read()


def import_annual_history(shards: list[dict], today: dt.date, cutoff: int) -> int:
    total = 0
    # Annual stocks are refreshed daily and contain the year's price records.
    for year in (today.year - 1, today.year):
        url = ANNUAL_URL.format(year)
        print(f"Import archive annuelle {year}: {url}", flush=True)
        payload = fetch(url)
        found = parse_price_records(payload, shards, cutoff)
        print(f"  {found} relevé(s) horodaté(s) retenu(s)", flush=True)
        total += found
    if total < 2:
        raise RuntimeError("Les archives annuelles n'ont pas fourni assez de relevés horodatés.")
    return total


def import_recent_days(shards: list[dict], today: dt.date, cutoff: int) -> int:
    total = 0
    for offset in (3, 2, 1):
        day = today - dt.timedelta(days=offset)
        url = DAILY_URL.format(day.strftime("%Y%m%d"))
        print(f"Complément quotidien {day.isoformat()}: {url}", flush=True)
        payload = fetch(url)
        # Daily XML contains the latest price per station; maj gives its real update time.
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            xml_names = [n for n in archive.namelist() if n.lower().endswith(".xml")]
            if not xml_names:
                continue
            for name in xml_names:
                with archive.open(name) as stream:
                    for _, elem in ET.iterparse(stream, events=("end",)):
                        if local_name(elem.tag) == "pdv":
                            station_id = elem.attrib.get("id", "").strip()
                            if station_id:
                                for child in elem:
                                    if local_name(child.tag) != "prix":
                                        continue
                                    try:
                                        fuel = child.attrib.get("nom", "").strip()
                                        value = float(child.attrib.get("valeur", "").strip().replace(",", "."))
                                        date = dt.datetime.strptime(child.attrib.get("maj", "").strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
                                        stamp = int(date.timestamp())
                                        if stamp >= cutoff and value > 0:
                                            add_record(shards, station_id, fuel, stamp, value)
                                            total += 1
                                    except (TypeError, ValueError):
                                        continue
                            elem.clear()
    return total


def main() -> None:
    today = dt.datetime.now(dt.timezone.utc).date()
    cutoff_day = today - dt.timedelta(days=KEEP_DAYS)
    cutoff = int(dt.datetime.combine(cutoff_day, dt.time(), tzinfo=dt.timezone.utc).timestamp())
    shards = load_shards()
    initialized = any(bool(s) for s in shards)

    if not initialized:
        imported = import_annual_history(shards, today, cutoff)
    else:
        imported = import_recent_days(shards, today, cutoff)
        if imported == 0:
            raise RuntimeError("Aucun relevé récent n'a pu être importé.")

    DATA.mkdir(parents=True, exist_ok=True)
    PUBLIC.mkdir(parents=True, exist_ok=True)
    for i, stations in enumerate(shards):
        for station_id in list(stations):
            fuels = stations[station_id]
            for fuel in list(fuels):
                # Deduplicate identical timestamps and keep chronological order.
                unique = {int(r[0]): [int(r[0]), round(float(r[1]), 3)] for r in fuels[fuel] if int(r[0]) >= cutoff}
                fuels[fuel] = [unique[k] for k in sorted(unique)]
                if not fuels[fuel]:
                    del fuels[fuel]
            if not fuels:
                del stations[station_id]
        raw = json.dumps({"updated": today.isoformat(), "stations": stations}, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode("utf-8")
        (DATA / f"{i:02d}.json").write_bytes(raw)
        (PUBLIC / f"{i:02d}.json.gz").write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))

    manifest = {
        "updated": today.isoformat(),
        "shards": SHARDS,
        "retention_days": KEEP_DAYS,
        "history_days_target": 365,
        "source": "https://donnees.roulez-eco.fr/opendata/annee/AAAA",
        "records_imported_this_run": imported,
    }
    (PUBLIC / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")

    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], cwd=ROOT, check=True)
    subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=ROOT, check=True)
    subprocess.run(["git", "add", "data/shards", "public/history"], cwd=ROOT, check=True)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT)
    if diff.returncode == 1:
        subprocess.run(["git", "commit", "-m", f"Mettre à jour l'historique carburants ({today.isoformat()})"], cwd=ROOT, check=True)
        subprocess.run(["git", "push"], cwd=ROOT, check=True)
    elif diff.returncode != 0:
        raise RuntimeError("Impossible de vérifier les changements Git.")
    print(f"OK : {imported} relevé(s) importé(s), fichiers JSON gzip publiés dans le dépôt.", flush=True)


if __name__ == "__main__":
    main()
