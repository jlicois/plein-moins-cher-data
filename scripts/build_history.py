#!/usr/bin/env python3
"""Build daily station/fuel history from the French government open-data snapshots."""
from __future__ import annotations

import datetime as dt
import gzip
import io
import json
import os
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
BASE_URL = "https://donnees.roulez-eco.fr/opendata/jour/{}"


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def load_shards() -> list[dict]:
    DATA.mkdir(parents=True, exist_ok=True)
    result = []
    for i in range(SHARDS):
        path = DATA / f"{i:02d}.json"
        if path.exists():
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
                result.append(obj.get("stations", {}))
                continue
            except Exception:
                print(f"WARNING: shard illisible, recréation: {path}")
        result.append({})
    return result


def fetch_day(day: dt.date) -> dict[str, dict[str, float]]:
    url = BASE_URL.format(day.strftime("%Y%m%d"))
    request = urllib.request.Request(url, headers={"User-Agent": "PleinMoinsCher-History/1.0"})
    print(f"Download {day.isoformat()} : {url}", flush=True)
    with urllib.request.urlopen(request, timeout=90) as response:
        payload = response.read()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        xml_names = [n for n in archive.namelist() if n.lower().endswith(".xml")]
        if not xml_names:
            raise RuntimeError(f"Aucun XML dans l'archive du {day.isoformat()}")
        result: dict[str, dict[str, float]] = {}
        for name in xml_names:
            with archive.open(name) as stream:
                for _, elem in ET.iterparse(stream, events=("end",)):
                    if local_name(elem.tag) != "pdv":
                        continue
                    station_id = elem.attrib.get("id", "").strip()
                    if station_id:
                        prices: dict[str, float] = {}
                        for child in elem:
                            if local_name(child.tag) != "prix":
                                continue
                            fuel = child.attrib.get("nom", "").strip()
                            raw = child.attrib.get("valeur", "").strip().replace(",", ".")
                            try:
                                value = round(float(raw), 3)
                            except (TypeError, ValueError):
                                continue
                            if fuel and value > 0:
                                prices[fuel] = value
                        if prices:
                            result[station_id] = prices
                    elem.clear()
    if not result:
        raise RuntimeError(f"Aucune station lue dans l'archive du {day.isoformat()}")
    print(f"  {len(result)} stations avec prix", flush=True)
    return result


def main() -> None:
    today = dt.datetime.now(dt.timezone.utc).date()
    shards = load_shards()
    initialized = any(bool(s) for s in shards)
    days = [today - dt.timedelta(days=n) for n in (3, 2, 1)]
    if not initialized:
        # Le flux quotidien officiel est consultable sur les 30 derniers jours.
        days = [today - dt.timedelta(days=n) for n in range(30, 0, -1)]

    successful = 0
    for day in days:
        try:
            snapshot = fetch_day(day)
        except Exception as exc:
            print(f"ERROR {day.isoformat()}: {exc}", flush=True)
            if not initialized:
                raise
            continue
        stamp = int(dt.datetime.combine(day, dt.time(), tzinfo=dt.timezone.utc).timestamp())
        for station_id, prices in snapshot.items():
            try:
                shard_no = int(station_id) % SHARDS
            except ValueError:
                shard_no = sum(station_id.encode("utf-8")) % SHARDS
            station = shards[shard_no].setdefault(station_id, {})
            for fuel, value in prices.items():
                records = station.setdefault(fuel, [])
                # Un instantané par jour : remplace une éventuelle réexécution du même jour.
                records[:] = [r for r in records if int(r[0]) != stamp]
                records.append([stamp, value])
                records.sort(key=lambda r: int(r[0]))
        successful += 1

    if successful == 0:
        raise RuntimeError("Aucun relevé quotidien n'a pu être importé.")

    cutoff = int((today - dt.timedelta(days=KEEP_DAYS)).replace(
        tzinfo=dt.timezone.utc
    ).timestamp())
    DATA.mkdir(parents=True, exist_ok=True)
    PUBLIC.mkdir(parents=True, exist_ok=True)
    for i, stations in enumerate(shards):
        for station_id in list(stations):
            fuels = stations[station_id]
            for fuel in list(fuels):
                fuels[fuel] = [r for r in fuels[fuel] if int(r[0]) >= cutoff]
                if not fuels[fuel]:
                    del fuels[fuel]
            if not fuels:
                del stations[station_id]
        obj = {"updated": today.isoformat(), "stations": stations}
        raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode("utf-8")
        (DATA / f"{i:02d}.json").write_bytes(raw)
        (PUBLIC / f"{i:02d}.json.gz").write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))

    manifest = {
        "updated": today.isoformat(),
        "shards": SHARDS,
        "retention_days": KEEP_DAYS,
        "source": "https://donnees.roulez-eco.fr/opendata/jour/AAAAMMJJ",
        "days_imported_this_run": successful,
    }
    (PUBLIC / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    # Commit uniquement les données sources compactes ; public/ est un artefact Pages.
    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], cwd=ROOT, check=True)
    subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=ROOT, check=True)
    subprocess.run(["git", "add", "data/shards"], cwd=ROOT, check=True)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT)
    if diff.returncode == 1:
        subprocess.run(["git", "commit", "-m", f"Mettre à jour l'historique quotidien ({today.isoformat()})"], cwd=ROOT, check=True)
        subprocess.run(["git", "push"], cwd=ROOT, check=True)
    elif diff.returncode != 0:
        raise RuntimeError("Impossible de vérifier les changements Git.")
    print(f"OK : {successful} journée(s) importée(s), publication préparée.", flush=True)


if __name__ == "__main__":
    main()
