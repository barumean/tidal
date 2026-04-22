#!/usr/bin/env python3
"""
조석예보 데이터 수집기
사용법:
  python fetch_all.py --months 2                        # 이번 달 + 다음 달
  python fetch_all.py --year 2026 --month 5             # 특정 월
  python fetch_all.py --months 1 --station DT_0001      # 특정 지점만
"""

import argparse
import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

sys.path.insert(0, str(Path(__file__).parent))
from config import (
    CALL_INTERVAL,
    KNOWN_COORDS,
    STATIONS,
    TIMESERIES_MIN,
    URL_HIGHTLOW,
    URL_TIMESERIES,
)

DATA_DIR = Path(__file__).parent.parent / "data"


def get_api_key() -> str:
    key = os.environ.get("TIDE_API_KEY", "")
    if not key:
        sys.exit("환경변수 TIDE_API_KEY가 설정되지 않았습니다.")
    return key


def target_months(args) -> list[tuple[int, int]]:
    """수집할 (year, month) 목록 반환"""
    today = date.today()
    if args.year and args.month:
        return [(args.year, args.month)]
    months = []
    y, m = today.year, today.month
    for _ in range(args.months):
        months.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def days_in_month(year: int, month: int) -> list[str]:
    """YYYYMMDD 문자열 목록 반환"""
    d = date(year, month, 1)
    result = []
    while d.month == month:
        result.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return result


def fetch_xml(url: str, params: dict, retries: int = 3) -> ET.Element | None:
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            code = root.findtext(".//resultCode", "")
            if code != "00":
                msg = root.findtext(".//resultMsg", "")
                print(f"    API 오류 {code}: {msg}")
                return None
            return root
        except Exception as e:
            if attempt < retries - 1:
                wait = 2 ** attempt
                print(f"    재시도 {attempt+1}/{retries} ({e}) — {wait}s 대기")
                time.sleep(wait)
            else:
                print(f"    실패: {e}")
    return None


def parse_hightlow(root: ET.Element) -> tuple[list[dict], float | None, float | None]:
    items = []
    lat = lon = None
    for item in root.findall(".//item"):
        dt = item.findtext("predcDt", "").strip()
        lv = item.findtext("predcTdlvVl", "").strip()
        kind = item.findtext("extrSe", "").strip()
        if lat is None:
            raw_lat = item.findtext("lat", "")
            raw_lon = item.findtext("lot", "")
            if raw_lat:
                try:
                    lat, lon = float(raw_lat), float(raw_lon)
                except ValueError:
                    pass
        if dt and lv and kind:
            try:
                items.append({
                    "datetime": dt,
                    "tideLevel": float(lv),
                    "type": "H" if kind == "1" else "L",
                })
            except ValueError:
                continue
    return items, lat, lon


def parse_timeseries(root: ET.Element) -> list[dict]:
    items = []
    for item in root.findall(".//item"):
        dt = item.findtext("predcDt", "").strip()
        lv = item.findtext("tdlvHgt", "").strip()
        if dt and lv:
            try:
                items.append({"datetime": dt, "tideLevel": float(lv)})
            except ValueError:
                continue
    return items


def collect_station(obs_code: str, year: int, month: int, api_key: str) -> dict | None:
    station_name = STATIONS.get(obs_code, obs_code)
    print(f"  [{obs_code}] {station_name} {year}/{month:02d}")

    all_days = days_in_month(year, month)
    days_data: dict[str, dict] = {}
    lat = lon = None

    for day_str in all_days:
        common = {
            "obsCode": obs_code,
            "reqDate": day_str,
            "serviceKey": api_key,
            "_type": "xml",
        }

        # ① 고·저조 예보
        hl_root = fetch_xml(URL_HIGHTLOW, common)
        time.sleep(CALL_INTERVAL)

        # ② 시계열 조위
        ts_params = {**common, "min": TIMESERIES_MIN}
        ts_root = fetch_xml(URL_TIMESERIES, ts_params)
        time.sleep(CALL_INTERVAL)

        hl_items, day_lat, day_lon = parse_hightlow(hl_root) if hl_root is not None else ([], None, None)
        ts_items = parse_timeseries(ts_root) if ts_root is not None else []

        if day_lat and lat is None:
            lat, lon = day_lat, day_lon

        days_data[day_str] = {"hightlow": hl_items, "timeseries": ts_items}

    # 좌표 보완
    if lat is None and obs_code in KNOWN_COORDS:
        lat, lon = KNOWN_COORDS[obs_code]

    return {
        "meta": {
            "obsCode": obs_code,
            "stationName": station_name,
            "year": year,
            "month": month,
            "fetchedAt": date.today().isoformat(),
            "lat": lat,
            "lon": lon,
        },
        "days": days_data,
    }


def update_index(obs_code: str, station_name: str, year: int, month: int, lat, lon):
    index_path = DATA_DIR / "index.json"
    if index_path.exists():
        with open(index_path, encoding="utf-8") as f:
            idx = json.load(f)
    else:
        idx = {"months": {}, "stations": {}, "coords": {}}

    ym = f"{year}{month:02d}"
    if ym not in idx["months"]:
        idx["months"][ym] = []
    if obs_code not in idx["months"][ym]:
        idx["months"][ym].append(obs_code)

    idx["stations"][obs_code] = station_name

    if lat is not None:
        idx["coords"][obs_code] = [lat, lon]

    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)


def save_station(data: dict, year: int, month: int):
    obs_code = data["meta"]["obsCode"]
    ym_dir = DATA_DIR / f"{year}{month:02d}"
    ym_dir.mkdir(parents=True, exist_ok=True)
    out_path = ym_dir / f"{obs_code}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(description="조석예보 데이터 수집기")
    parser.add_argument("--months", type=int, default=1, help="이번 달부터 N개월 수집")
    parser.add_argument("--year", type=int, help="특정 연도")
    parser.add_argument("--month", type=int, help="특정 월")
    parser.add_argument("--station", help="특정 지점 코드만 수집 (예: DT_0001)")
    args = parser.parse_args()

    api_key = get_api_key()
    months = target_months(args)
    stations = {args.station: STATIONS[args.station]} if args.station and args.station in STATIONS else STATIONS

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    total = len(months) * len(stations)
    done = 0

    for year, month in months:
        print(f"\n=== {year}년 {month:02d}월 ({len(stations)}개 지점) ===")
        for obs_code, station_name in stations.items():
            done += 1
            print(f"[{done}/{total}]", end=" ")
            data = collect_station(obs_code, year, month, api_key)
            if data:
                save_station(data, year, month)
                update_index(
                    obs_code,
                    station_name,
                    year,
                    month,
                    data["meta"]["lat"],
                    data["meta"]["lon"],
                )

    print(f"\n완료: {done}개 지점 처리")


if __name__ == "__main__":
    main()
