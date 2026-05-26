#!/usr/bin/env python3
"""
조석예보 MCP 서버

Claude가 한국 해역 조석예보 데이터를 직접 조회할 수 있는 도구 모음.
data/ 디렉토리의 JSON 파일을 읽어 응답합니다.

실행:
  python src/mcp_server.py        # stdio 모드 (Claude Code / MCP 클라이언트용)

도구 목록:
  list_stations         — 지점 목록 조회 (필터·검색 가능)
  get_available_months  — 수집된 데이터의 가용 월 확인
  get_tide_forecast     — 특정 지점·날짜의 고저조 예보
  get_monthly_summary   — 월간 조석 통계
  get_lunar_info        — 음력 날짜·달 위상·물때 조회
  compare_tides         — 여러 지점 조석 비교 (최대 5개)
"""

import json
from datetime import date
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ── 경로 ──────────────────────────────────────────────────────────────────────
DATA_DIR = Path(__file__).parent.parent / "data"

# ── FastMCP 인스턴스 ──────────────────────────────────────────────────────────
mcp = FastMCP(
    "tide-forecast",
    instructions=(
        "한국 해역 조석예보 도구입니다. "
        "data/ 디렉토리에 수집된 JSON 데이터를 기반으로 응답합니다. "
        "데이터가 없는 월은 GitHub Actions로 먼저 수집해야 합니다."
    ),
)


# ── ① 음력 엔진 ───────────────────────────────────────────────────────────────
# 삭일 테이블 (한국천문연구원 KST 기준, 2024-11 ~ 2027-03)
# 형식: (YYYYMMDD, 음력연, 음력월, 윤월)
_SAKIL: list[tuple[str, int, int, bool]] = [
    ("20241101", 2024, 10, False), ("20241201", 2024, 11, False), ("20241231", 2024, 12, False),
    ("20250129", 2025,  1, False), ("20250228", 2025,  2, False), ("20250329", 2025,  3, False),
    ("20250428", 2025,  4, False), ("20250527", 2025,  5, False), ("20250625", 2025,  6, False),
    ("20250725", 2025,  6, True ),  # 윤6월
    ("20250823", 2025,  7, False), ("20250922", 2025,  8, False), ("20251021", 2025,  9, False),
    ("20251120", 2025, 10, False), ("20251220", 2025, 11, False), ("20260119", 2025, 12, False),
    ("20260217", 2026,  1, False), ("20260319", 2026,  2, False), ("20260417", 2026,  3, False),
    ("20260516", 2026,  4, False), ("20260615", 2026,  5, False), ("20260714", 2026,  6, False),
    ("20260813", 2026,  7, False), ("20260911", 2026,  8, False), ("20261011", 2026,  9, False),
    ("20261109", 2026, 10, False), ("20261209", 2026, 11, False), ("20270107", 2026, 12, False),
    ("20270206", 2027,  1, False), ("20270308", 2027,  2, False),
]

_MOON_PHASE = [
    "🌑 삭(朔)", "🌒 초승달", "🌒 초승달", "🌒 초승달", "🌒 초승달",
    "🌒 초승달", "🌒 초승달", "🌓 상현(上弦)", "🌔 상현달", "🌔 상현달",
    "🌔 상현달", "🌔 상현달", "🌔 상현달", "🌔 상현달", "🌕 망(望/보름)",
    "🌖 보름달", "🌖 보름달", "🌖 보름달", "🌖 보름달", "🌖 보름달",
    "🌖 보름달", "🌗 하현(下弦)", "🌘 그믐달", "🌘 그믐달", "🌘 그믐달",
    "🌘 그믐달", "🌘 그믐달", "🌘 그믐달", "🌘 그믐달", "🌑 그믐",
]

# 물때표 (음력 1일 기준 15일 주기, 서해 기준)
_MT: list[dict] = [
    {"num": 7,      "label": "7물",  "type": "mid"},
    {"num": 8,      "label": "사리", "type": "sari"},
    {"num": 9,      "label": "9물",  "type": "mid"},
    {"num": 10,     "label": "10물", "type": "mid"},
    {"num": 11,     "label": "11물", "type": "mid"},
    {"num": 12,     "label": "12물", "type": "mid"},
    {"num": 13,     "label": "13물", "type": "mid"},
    {"num": 1,      "label": "1물",  "type": "mid"},
    {"num": "조금", "label": "조금", "type": "jogeum"},
    {"num": "무시", "label": "무시", "type": "musi"},
    {"num": 4,      "label": "4물",  "type": "mid"},
    {"num": 5,      "label": "5물",  "type": "mid"},
    {"num": 6,      "label": "6물",  "type": "mid"},
    {"num": 7,      "label": "7물",  "type": "mid"},
    {"num": 8,      "label": "8물",  "type": "mid"},
]

_FESTIVALS: dict[str, str] = {
    "1/1": "설날", "1/2": "설날연휴", "1/15": "대보름", "4/8": "초파일",
    "7/7": "칠석", "7/15": "백중",
    "8/14": "추석연휴", "8/15": "추석", "8/16": "추석연휴",
    "9/9": "중양절",
}


def _to_date(s: str) -> date:
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def _get_lunar(yyyymmdd: str) -> dict | None:
    """양력 날짜(YYYYMMDD) → 음력 정보 dict"""
    best = None
    for entry in _SAKIL:
        if entry[0] <= yyyymmdd:
            best = entry
        else:
            break
    if best is None:
        return None
    sak_date, ly, lm, leap = best
    day = (_to_date(yyyymmdd) - _to_date(sak_date)).days + 1
    mt = _MT[(day - 1) % 15]
    return {
        "year": ly,
        "month": lm,
        "day": day,
        "leap": leap,
        "moon": _MOON_PHASE[min(day - 1, 29)],
        "multtae": mt,
        "festival": _FESTIVALS.get(f"{lm}/{day}", ""),
    }


# ── 데이터 로더 ───────────────────────────────────────────────────────────────
def _load_index() -> dict:
    p = DATA_DIR / "index.json"
    if not p.exists():
        return {"months": {}, "stations": {}, "coords": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def _load_month(code: str, year: int, month: int) -> dict | None:
    p = DATA_DIR / f"{year}{month:02d}" / f"{code}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _validate_date(ds: str) -> str | None:
    """오류 메시지 반환, 정상이면 None"""
    if len(ds) != 8 or not ds.isdigit():
        return "날짜 형식 오류: YYYYMMDD (예: 20260422)"
    try:
        _to_date(ds)
    except ValueError as e:
        return f"날짜 오류: {e}"
    return None


# ── 도구 ① list_stations ─────────────────────────────────────────────────────
@mcp.tool()
def list_stations(prefix: str = "all", search: str = "") -> str:
    """
    조석예보 지점 목록을 조회합니다.

    Args:
        prefix: 지점 유형 필터. "DT"(주요 검조소), "IE"(이어도·외해),
                "SO"(보조 지점), "all"(전체). 기본값: "all"
        search: 지점명 또는 코드 검색어 (선택)
    """
    idx = _load_index()
    stations: dict[str, str] = idx.get("stations", {})
    coords: dict[str, list] = idx.get("coords", {})

    if not stations:
        return "지점 데이터 없음 — data/index.json을 확인하거나 데이터를 수집해주세요."

    q = search.strip().lower()
    groups: dict[str, list[str]] = {"DT": [], "IE": [], "SO": [], "기타": []}
    for code, name in sorted(stations.items()):
        if prefix != "all" and not code.startswith(prefix + "_"):
            continue
        if q and q not in name.lower() and q not in code.lower():
            continue
        p = code.split("_")[0]
        has_coord = "✓" if code in coords else "✗"
        groups.get(p, groups["기타"]).append(f"  {code:<12} {name:<10} 좌표:{has_coord}")

    total = sum(len(v) for v in groups.values())
    if total == 0:
        return "검색 결과 없음"

    labels = {"DT": "주요 검조소", "IE": "이어도·외해", "SO": "보조 지점", "기타": "기타"}
    lines = [f"총 {total}개 지점\n"]
    for p, items in groups.items():
        if items:
            lines.append(f"[{labels[p]}] ({len(items)}개)")
            lines.extend(items)
            lines.append("")
    return "\n".join(lines).rstrip()


# ── 도구 ② get_available_months ──────────────────────────────────────────────
@mcp.tool()
def get_available_months(station_code: str = "") -> str:
    """
    수집된 데이터의 가용 월 목록을 반환합니다.

    Args:
        station_code: 특정 지점 코드 (예: DT_0001). 비워두면 전체 월 목록 반환.
    """
    idx = _load_index()
    months: dict[str, list] = idx.get("months", {})

    if not months:
        return "수집된 데이터가 없습니다.\n'python src/fetch_all.py --months 2'로 데이터를 수집해주세요."

    code = station_code.strip()
    if code:
        name = idx.get("stations", {}).get(code, code)
        available = sorted(ym for ym, codes in months.items() if code in codes)
        if not available:
            return f"{name} ({code}) 지점의 데이터가 없습니다."
        lines = [f"{name} ({code}) 가용 월 — {len(available)}개월\n"]
        for ym in available:
            lines.append(f"  {ym[:4]}년 {int(ym[4:]):2d}월  ({ym})")
        return "\n".join(lines)

    lines = [f"전체 가용 월 — {len(months)}개월\n"]
    for ym, codes in sorted(months.items()):
        lines.append(f"  {ym[:4]}년 {int(ym[4:]):2d}월  ({ym})  {len(codes)}개 지점")
    return "\n".join(lines)


# ── 도구 ③ get_tide_forecast ─────────────────────────────────────────────────
@mcp.tool()
def get_tide_forecast(
    station_code: str,
    date: str,
    include_timeseries: bool = False,
) -> str:
    """
    특정 지점과 날짜의 조석예보를 조회합니다.

    Args:
        station_code: 지점 코드 (예: DT_0001 인천, DT_0013 부산)
        date: 조회 날짜 YYYYMMDD (예: 20260422)
        include_timeseries: 60분 간격 시계열 조위 포함 여부 (기본: False)
    """
    err = _validate_date(date)
    if err:
        return err

    year, month = int(date[:4]), int(date[4:6])
    data = _load_month(station_code, year, month)
    if data is None:
        return (
            f"{station_code} 지점의 {year}년 {month:02d}월 데이터가 없습니다.\n"
            f"'python src/fetch_all.py --year {year} --month {month} --station {station_code}'"
            f" 로 수집하세요."
        )

    day_data = data.get("days", {}).get(date)
    if day_data is None:
        return f"{date[:4]}/{date[4:6]}/{date[6:8]} 날짜 데이터가 없습니다."

    idx = _load_index()
    name = idx.get("stations", {}).get(station_code, station_code)
    lunar = _get_lunar(date)

    lines = [f"{'═'*50}"]
    lines.append(f"  {name} ({station_code})")
    lines.append(f"  {date[:4]}/{date[4:6]}/{date[6:8]} (양력)")
    if lunar:
        leap = "윤" if lunar["leap"] else ""
        mt = lunar["multtae"]
        fest = f"  ★ {lunar['festival']}" if lunar["festival"] else ""
        lines.append(
            f"  음{lunar['year']}/{leap}{lunar['month']}/{lunar['day']}일  "
            f"{lunar['moon']}  물때:{mt['label']}{fest}"
        )
    lines.append(f"{'═'*50}\n")

    hl = day_data.get("hightlow", [])
    if hl:
        lines.append("▸ 고·저조 예보")
        for x in hl:
            kind = "▲ 고조" if x["type"] == "H" else "▽ 저조"
            lines.append(f"  {kind}  {x['datetime'][5:16]}  {x['tideLevel']:>6.1f} cm")
    else:
        lines.append("▸ 고·저조 데이터 없음")

    if include_timeseries:
        ts = day_data.get("timeseries", [])
        lines.append(f"\n▸ 시계열 조위 (60분 간격, {len(ts)}건)")
        if ts:
            for x in ts:
                lines.append(f"  {x['datetime'][11:16]}  {x['tideLevel']:>7.1f} cm")
        else:
            lines.append("  데이터 없음")

    return "\n".join(lines)


# ── 도구 ④ get_monthly_summary ───────────────────────────────────────────────
@mcp.tool()
def get_monthly_summary(station_code: str, year: int, month: int) -> str:
    """
    특정 지점의 월간 조석 통계를 반환합니다.
    최고/최저 조위, 평균 조차, 사리·조금 날짜 등을 포함합니다.

    Args:
        station_code: 지점 코드 (예: DT_0001)
        year: 연도 (예: 2026)
        month: 월 1~12 (예: 4)
    """
    if not (1 <= month <= 12):
        return "월은 1~12 사이여야 합니다."

    data = _load_month(station_code, year, month)
    if data is None:
        return (
            f"{station_code} 지점의 {year}년 {month:02d}월 데이터가 없습니다.\n"
            f"'python src/fetch_all.py --year {year} --month {month} --station {station_code}'"
            f" 로 수집하세요."
        )

    idx = _load_index()
    name = idx.get("stations", {}).get(station_code, station_code)
    days = data.get("days", {})

    all_h: list[float] = []
    all_l: list[float] = []
    sari_days: list[str] = []
    jogeum_days: list[str] = []
    max_range = -1.0
    min_range = 9999.0
    max_range_day = min_range_day = ""

    for ds, dv in sorted(days.items()):
        hl = dv.get("hightlow", [])
        highs = [x["tideLevel"] for x in hl if x["type"] == "H"]
        lows  = [x["tideLevel"] for x in hl if x["type"] == "L"]
        all_h.extend(highs)
        all_l.extend(lows)

        if highs and lows:
            r = max(highs) - min(lows)
            if r > max_range:
                max_range = r
                max_range_day = ds
            if r < min_range:
                min_range = r
                min_range_day = ds

        lunar = _get_lunar(ds)
        if lunar:
            t = lunar["multtae"]["type"]
            day_label = f"{ds[6:8]}일"
            if t == "sari":
                sari_days.append(day_label)
            elif t == "jogeum":
                jogeum_days.append(day_label)

    def ymd(ds: str) -> str:
        return f"{ds[:4]}/{ds[4:6]}/{ds[6:8]}" if ds else "—"

    def avg(lst: list[float]) -> str:
        return f"{sum(lst)/len(lst):.1f}" if lst else "—"

    lines = [
        f"{'═'*50}",
        f"  {name} ({station_code})  {year}년 {month:02d}월 요약",
        f"{'═'*50}\n",
        f"수집 일수    : {len(days)}일",
        f"fetchedAt    : {data.get('meta', {}).get('fetchedAt', '—')}\n",
        f"월 최고 조위 : {max(all_h):.1f} cm  (고조 기준)" if all_h else "월 최고 조위 : 데이터 없음",
        f"월 최저 조위 : {min(all_l):.1f} cm  (저조 기준)" if all_l else "월 최저 조위 : 데이터 없음",
        f"평균 고조위  : {avg(all_h)} cm",
        f"평균 저조위  : {avg(all_l)} cm",
        f"평균 조차    : {(sum(all_h)/len(all_h)-sum(all_l)/len(all_l)):.1f} cm"
            if all_h and all_l else "평균 조차    : —",
        "",
        f"최대 조차일  : {ymd(max_range_day)}  ({max_range:.0f} cm)" if max_range_day else "",
        f"최소 조차일  : {ymd(min_range_day)}  ({min_range:.0f} cm)" if min_range_day else "",
        "",
        f"사리 날짜    : {', '.join(sari_days)}" if sari_days else "사리 날짜    : —",
        f"조금 날짜    : {', '.join(jogeum_days)}" if jogeum_days else "조금 날짜    : —",
        "",
        "※ 물때는 서해 기준 근사값입니다.",
    ]
    return "\n".join(l for l in lines if l is not None)


# ── 도구 ⑤ get_lunar_info ────────────────────────────────────────────────────
@mcp.tool()
def get_lunar_info(date: str) -> str:
    """
    양력 날짜의 음력 정보를 반환합니다.
    음력 날짜, 달 위상, 물때(서해 기준), 명절 여부를 포함합니다.

    Args:
        date: 양력 날짜 YYYYMMDD (예: 20260422)
    """
    err = _validate_date(date)
    if err:
        return err

    lunar = _get_lunar(date)
    if lunar is None:
        return (
            f"{date[:4]}/{date[4:6]}/{date[6:8]}는 음력 데이터 범위 밖입니다.\n"
            "지원 범위: 2024년 11월 ~ 2027년 3월"
        )

    leap = "윤" if lunar["leap"] else ""
    mt = lunar["multtae"]
    fest = f"\n명절/절기  : {lunar['festival']}" if lunar["festival"] else ""

    return (
        f"양력 {date[:4]}/{date[4:6]}/{date[6:8]} 음력 정보\n\n"
        f"음력 날짜  : {lunar['year']}년 {leap}{lunar['month']}월 {lunar['day']}일\n"
        f"달 위상    : {lunar['moon']}\n"
        f"물때(서해) : {mt['label']}  ({mt['type']}){fest}\n\n"
        "※ 물때는 서해 기준 근사값입니다."
    )


# ── 도구 ⑥ compare_tides ────────────────────────────────────────────────────
@mcp.tool()
def compare_tides(station_codes: list[str], date: str) -> str:
    """
    여러 지점의 특정 날짜 조석을 비교합니다. 최대 5개 지점까지 가능합니다.

    Args:
        station_codes: 비교할 지점 코드 목록 (예: ["DT_0001","DT_0013","DT_0020"])
        date: 비교 날짜 YYYYMMDD (예: 20260422)
    """
    if not station_codes:
        return "지점 코드를 하나 이상 입력하세요."
    if len(station_codes) > 5:
        return "최대 5개 지점까지 비교할 수 있습니다."

    err = _validate_date(date)
    if err:
        return err

    year, month = int(date[:4]), int(date[4:6])
    idx = _load_index()
    lunar = _get_lunar(date)

    lines = [f"{'═'*55}"]
    lines.append(f"  {date[:4]}/{date[4:6]}/{date[6:8]} 지점별 조석 비교")
    if lunar:
        leap = "윤" if lunar["leap"] else ""
        mt = lunar["multtae"]
        lines.append(
            f"  음{lunar['year']}/{leap}{lunar['month']}/{lunar['day']}  "
            f"{lunar['moon']}  물때:{mt['label']}"
        )
    lines.append(f"{'═'*55}\n")
    lines.append(f"  {'지점':<10} {'코드':<12} {'최고조위':>8} {'최저조위':>8} {'조차':>7}")
    lines.append(f"  {'-'*53}")

    for code in station_codes:
        name = idx.get("stations", {}).get(code, code)
        data = _load_month(code, year, month)
        if data is None:
            lines.append(f"  {name:<10} {code:<12} {'데이터 없음':>25}")
            continue

        day_data = data.get("days", {}).get(date)
        if day_data is None:
            lines.append(f"  {name:<10} {code:<12} {'해당 날짜 없음':>25}")
            continue

        hl = day_data.get("hightlow", [])
        highs = [x["tideLevel"] for x in hl if x["type"] == "H"]
        lows  = [x["tideLevel"] for x in hl if x["type"] == "L"]
        max_h = f"{max(highs):.0f}cm" if highs else "—"
        min_l = f"{min(lows):.0f}cm"  if lows  else "—"
        rng   = f"{max(highs)-min(lows):.0f}cm" if highs and lows else "—"
        lines.append(f"  {name:<10} {code:<12} {max_h:>8} {min_l:>8} {rng:>7}")

    lines.append(f"\n{'─'*55}")
    lines.append("※ 물때는 서해 기준 근사값 / 남해·동해는 조차가 작아 참고용으로만 활용하세요.")
    return "\n".join(lines)


# ── 진입점 ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mcp.run()
