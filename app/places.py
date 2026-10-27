"""Places lane: EXIF GPS -> city name, fully offline.

The user's chosen design (docs/tech-stack.md, 二期): a *small built-in table of
city coordinates* + nearest-neighbour reverse lookup, instead of any online
geocoder. No network, no model download, no third-party data dependency — the
whole feature is this file plus four columns in SQLite.

Precision is honest and deliberately coarse: photos within ~80 km of a listed
city are labelled with that city; anything farther from every city (wilderness,
open sea) is labelled "其他地点" rather than pretending to know a place.

It rides the same slow-lane contract as OCR / semantics / people:
  - new rows start gps_status='pending' and are processed in one daemon thread;
  - ingest already has the EXIF open, so brand-new rows come in as 'done'
    (videos too: a poster frame carries no camera GPS);
  - start_backfill() is a cheap no-op when idle or empty, so ingest.
    kick_slow_lanes can call it after every scan/watch batch — this is what
    lights up GPS on photos indexed before this lane existed;
  - an unreadable file is marked 'failed' instead of crashing the batch.
"""
from __future__ import annotations

import math
import threading
import time

from .db import get_conn, place_groups

RADIUS_KM = 80.0        # "城市级" honesty: beyond this it is just 其他地点
OTHER_PLACE = "其他地点"

# ---------------------------------------------------------------------------
# offline city table (name, lat, lon). Chinese prefecture-level cities first,
# then Hong Kong / Macau / Taiwan and a compact set of overseas destinations.
# 80 km covers a city's whole metro area and day-trip belt without swallowing
# the next city (Shanghai↔Suzhou ≈ 85 km apart).
# ---------------------------------------------------------------------------
_CITY_TABLE = """
北京 39.9042 116.4074
上海 31.2304 121.4737
广州 23.1291 113.2644
深圳 22.5431 114.0579
天津 39.0842 117.2009
重庆 29.5630 106.5516
成都 30.5728 104.0668
杭州 30.2741 120.1551
武汉 30.5928 114.3055
西安 34.3416 108.9398
南京 32.0603 118.7969
苏州 31.2989 120.5853
长沙 28.2282 112.9388
郑州 34.7466 113.6254
青岛 36.0671 120.3826
沈阳 41.8057 123.4315
大连 38.9140 121.6147
厦门 24.4798 118.0894
福州 26.0745 119.2965
昆明 25.0389 102.7183
合肥 31.8206 117.2272
济南 36.6512 117.1201
哈尔滨 45.8038 126.5349
长春 43.8171 125.3235
石家庄 38.0428 114.5149
太原 37.8706 112.5489
南昌 28.6820 115.8579
贵阳 26.6470 106.6302
南宁 22.8170 108.3665
海口 20.0440 110.1999
三亚 18.2528 109.5119
兰州 36.0611 103.8343
西宁 36.6171 101.7782
银川 38.4872 106.2309
乌鲁木齐 43.8256 87.6168
呼和浩特 40.8424 111.7490
拉萨 29.6520 91.1721
宁波 29.8683 121.5440
温州 27.9938 120.6994
无锡 31.4912 120.3119
常州 31.8107 119.9741
南通 31.9802 120.8943
徐州 34.2058 117.2848
扬州 32.3942 119.4129
绍兴 30.0303 120.5802
嘉兴 30.7522 120.7550
金华 29.0790 119.6474
台州 28.6562 121.4208
珠海 22.2707 113.5767
佛山 23.0215 113.1214
东莞 23.0207 113.7518
中山 22.5170 113.3926
惠州 23.1115 114.4162
汕头 23.3541 116.6819
湛江 21.2707 110.3594
桂林 25.2736 110.2900
柳州 24.3146 109.4281
泉州 24.8741 118.6757
漳州 24.5130 117.6471
莆田 25.4540 119.0078
洛阳 34.6197 112.4540
开封 34.7971 114.3076
邯郸 36.6256 114.5392
保定 38.8740 115.4646
唐山 39.6309 118.1802
秦皇岛 39.9354 119.5996
烟台 37.4638 121.4479
潍坊 36.7069 119.1618
淄博 36.8135 118.0548
威海 37.5128 122.1201
临沂 35.1047 118.3564
济宁 35.4149 116.5871
泰安 36.2003 117.0874
绵阳 31.4675 104.6796
德阳 31.1289 104.3980
宜宾 28.7518 104.6432
泸州 28.8717 105.4433
遵义 27.7257 106.9272
大理 25.6065 100.2676
丽江 26.8550 100.2278
张家界 29.1170 110.4791
岳阳 29.3571 113.1289
株洲 27.8274 113.1340
衡阳 26.8934 112.5719
九江 29.7051 116.0019
赣州 25.8311 114.9350
景德镇 29.2688 117.1784
芜湖 31.3525 118.4331
蚌埠 32.9166 117.3893
安庆 30.5435 117.0635
襄阳 32.0090 112.1224
宜昌 30.6919 111.2865
荆州 30.3348 112.2410
十堰 32.5915 110.7989
南阳 32.9908 112.5283
新乡 35.3030 113.9268
焦作 35.2159 113.2418
安阳 36.0977 114.3931
廊坊 39.5379 116.6835
沧州 38.3045 116.8388
张家口 40.7686 114.8869
承德 40.9515 117.9632
大同 40.0768 113.3001
包头 40.6574 109.8403
鄂尔多斯 39.6086 109.7813
赤峰 42.2586 118.8880
鞍山 41.1086 122.9946
锦州 41.0951 121.1270
丹东 40.0008 124.3547
吉林 43.8378 126.5496
齐齐哈尔 47.3543 123.9182
大庆 46.5893 125.1038
牡丹江 44.5514 129.6331
香港 22.3193 114.1694
澳门 22.1987 113.5439
台北 25.0330 121.5654
高雄 22.6273 120.3014
东京 35.6762 139.6503
大阪 34.6937 135.5023
京都 35.0116 135.7681
首尔 37.5665 126.9780
新加坡 1.3521 103.8198
曼谷 13.7563 100.5018
吉隆坡 3.1390 101.6869
登巴萨 -8.6500 115.2167
雅加达 -6.2088 106.8456
马尼拉 14.5995 120.9842
河内 21.0285 105.8542
胡志明市 10.8231 106.6297
迪拜 25.2048 55.2708
伊斯坦布尔 41.0082 28.9784
莫斯科 55.7558 37.6173
伦敦 51.5074 -0.1278
巴黎 48.8566 2.3522
柏林 52.5200 13.4050
罗马 41.9028 12.4964
米兰 45.4642 9.1900
巴塞罗那 41.3851 2.1734
马德里 40.4168 -3.7038
阿姆斯特丹 52.3676 4.9041
苏黎世 47.3769 8.5417
维也纳 48.2082 16.3738
布拉格 50.0755 14.4378
雅典 37.9838 23.7275
斯德哥尔摩 59.3293 18.0686
哥本哈根 55.6761 12.5683
赫尔辛基 60.1699 24.9384
里斯本 38.7223 -9.1393
法兰克福 50.1109 8.6821
慕尼黑 48.1351 11.5820
纽约 40.7128 -74.0060
洛杉矶 34.0522 -118.2437
旧金山 37.7749 -122.4194
西雅图 47.6062 -122.3321
芝加哥 41.8781 -87.6298
波士顿 42.3601 -71.0589
华盛顿 38.9072 -77.0369
拉斯维加斯 36.1699 -115.1398
迈阿密 25.7617 -80.1918
多伦多 43.6532 -79.3832
温哥华 49.2827 -123.1207
悉尼 -33.8688 151.2093
墨尔本 -37.8136 144.9631
奥克兰 -36.8485 174.7633
开罗 30.0444 31.2357
开普敦 -33.9249 18.4241
内罗毕 -1.2921 36.8219
圣保罗 -23.5505 -46.6333
里约热内卢 -22.9068 -43.1729
墨西哥城 19.4326 -99.1332
"""

CITIES: tuple[tuple[str, float, float], ...] = tuple(
    (name, float(lat), float(lon))
    for name, lat, lon in (ln.split() for ln in _CITY_TABLE.strip().splitlines())
    if name
)

_progress_lock = threading.Lock()
PROGRESS = {
    "running": False, "scanned": 0, "located": 0, "failed": 0,
    "total": 0, "started_at": None, "finished_at": None, "error": None,
}


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest_city(lat: float, lon: float) -> tuple[str, float]:
    """(city name, distance in km) of the closest table entry."""
    best, best_d = "", float("inf")
    for name, clat, clon in CITIES:
        d = _haversine_km(lat, lon, clat, clon)
        if d < best_d:
            best, best_d = name, d
    return best, best_d


def place_for(lat: float, lon: float) -> str:
    """City name when within RADIUS_KM, else 其他地点 (never None for a fix)."""
    name, dist = nearest_city(lat, lon)
    return name if name and dist <= RADIUS_KM else OTHER_PLACE


# ---------------------------------------------------------------------------
# EXIF GPS
# ---------------------------------------------------------------------------


def _dms(value) -> float | None:
    """GPS coordinate tag: DMS rational triple, or one plain number."""
    try:
        parts = list(value)
        if len(parts) >= 3:
            d, m, s = (float(x) for x in parts[:3])
            return d + m / 60.0 + s / 3600.0
        if len(parts) == 1:
            return float(parts[0])
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def gps_from_exif(img) -> tuple[float, float] | None:
    """(lat, lon) in decimal degrees from the GPS IFD (0x8825), else None.

    Reads the header-level EXIF only — no pixel decode, so it is safe on any
    Pillow-openable original (heic/avif included) and does not disturb drafts.
    """
    try:
        gps = img.getexif().get_ifd(0x8825)
    except Exception:
        return None
    if not gps:
        return None
    lat = _dms(gps.get(2))
    lon = _dms(gps.get(4))
    if lat is None or lon is None:
        return None
    lat_ref = str(gps.get(1) or "N").strip().upper()[:1]
    lon_ref = str(gps.get(3) or "E").strip().upper()[:1]
    if lat_ref == "S":
        lat = -lat
    if lon_ref == "W":
        lon = -lon
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    if abs(lat) < 1e-6 and abs(lon) < 1e-6:   # unset / "null island"
        return None
    return round(lat, 6), round(lon, 6)


def read_gps_file(path: str) -> tuple[float, float] | None:
    """Header-only GPS read straight from an original file (backfill path)."""
    from PIL import Image
    with Image.open(path) as img:
        return gps_from_exif(img)


# ---------------------------------------------------------------------------
# slow lane (for rows indexed before this feature existed)
# ---------------------------------------------------------------------------


def _bump(**kw) -> None:
    with _progress_lock:
        for k, v in kw.items():
            PROGRESS[k] = PROGRESS.get(k, 0) + v


def _backfill_worker() -> None:
    conn = get_conn()
    try:
        todo = conn.execute(
            "SELECT id, path FROM photos WHERE gps_status='pending'"
            " AND status='ok' AND kind!='video' ORDER BY taken_at DESC"
        ).fetchall()
        with _progress_lock:
            PROGRESS["total"] = len(todo)
        for r in todo:
            fid = r["id"]
            try:
                gps = read_gps_file(r["path"])
                lat, lon = gps if gps else (None, None)
                place = place_for(lat, lon) if gps else None
                conn.execute(
                    "UPDATE photos SET gps_lat=?, gps_lon=?, place=?,"
                    " gps_status='done' WHERE id=?", (lat, lon, place, fid))
                _bump(scanned=1)
                if place:
                    _bump(located=1)
            except Exception:
                conn.execute(
                    "UPDATE photos SET gps_status='failed' WHERE id=?", (fid,))
                _bump(failed=1)
            if PROGRESS["scanned"] % 200 == 0:
                conn.commit()
        conn.commit()
    except Exception as e:
        with _progress_lock:
            PROGRESS["error"] = f"{type(e).__name__}: {e}"
    finally:
        with _progress_lock:
            PROGRESS["running"] = False
            PROGRESS["finished_at"] = time.time()


def start_backfill() -> dict:
    conn = get_conn()
    pending = conn.execute(
        "SELECT COUNT(*) c FROM photos WHERE gps_status='pending'"
        " AND status='ok' AND kind!='video'").fetchone()["c"]
    with _progress_lock:
        if PROGRESS["running"]:
            return {"started": False, "reason": "backfill already running"}
        if pending == 0:
            return {"started": False, "reason": "nothing pending"}
        PROGRESS.update(running=True, scanned=0, located=0, failed=0,
                        total=pending, started_at=time.time(),
                        finished_at=None, error=None)
    threading.Thread(target=_backfill_worker, daemon=True,
                     name="places-backfill").start()
    return {"started": True, "pending": pending}


def progress_snapshot() -> dict:
    with _progress_lock:
        return dict(PROGRESS)


def summary() -> dict:
    conn = get_conn()
    with_gps = conn.execute(
        "SELECT COUNT(*) c FROM photos WHERE gps_lat IS NOT NULL"
        " AND status!='missing'").fetchone()["c"]
    named = conn.execute(
        "SELECT COUNT(*) c FROM photos WHERE place IS NOT NULL"
        " AND place!=? AND status!='missing'", (OTHER_PLACE,)).fetchone()["c"]
    other = conn.execute(
        "SELECT COUNT(*) c FROM photos WHERE place=? AND status!='missing'",
        (OTHER_PLACE,)).fetchone()["c"]
    pending = conn.execute(
        "SELECT COUNT(*) c FROM photos WHERE gps_status='pending'"
        " AND status='ok' AND kind!='video'").fetchone()["c"]
    return {"with_gps": with_gps, "named": named, "other": other,
            "pending": pending, "places": place_groups(),
            "progress": progress_snapshot()}
