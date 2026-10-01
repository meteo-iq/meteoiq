#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""أفق — توقعات المدى الممتد (45 يوماً) بميتوغرام احتمالي متعدد النماذج.
python longrange.py --state state --out site          # تشغيل عادي
python longrange.py --demo --state /tmp/s --out /tmp/o  # اختبار بلا إنترنت (بيانات وهمية)
"""
import argparse, bisect, csv, gzip, io, json, math, os, random, re, sys, time
import urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

UA = "OfuqLongRange/1.0 (personal use)"
D = 45                      # عدد الأيام المعروضة
N = D * 4                   # خطوات كل 6 ساعات
BAND_UB = [2, 5, 9, 15, 25, 46]                 # حدود نطاقات المدى (بالأيام)
BAND_AR = ["0–2", "3–5", "6–9", "10–15", "16–25", "26–45"]
QS = [.10, .25, .50, .75, .90]
RAIN_T = [1, 5, 10, 25]     # عتبات مطر يومي (مم)
RAIN6_T = [.2, 1, 5, 10]    # عتبات مطر 6 ساعات (مم)
DEMO = False

LOCS = {  # الاسم: (عرض، طول، الاسم العربي)  — يمكن تعديلها عبر locations.json
    "Nasiriyah": (31.05, 46.2667, "الناصرية"),
    "Al-Shatra": (31.4142, 46.1653, "الشطرة"),
    "Suq al-Shuyukh": (30.8878, 46.6306, "سوق الشيوخ"),
    "Qalat Sukkar": (31.8503, 46.0761, "قلعة سكر"),
    "Al-Sulaiman": (31.3927, 46.3346, "آل سليمان"),
}
# النماذج: الوزن الأولي لكل نطاق مدى (0 = غير متاح). يتعدل ذاتياً بعد التحقق من الرصد.
MODELS = {
    "ec46": dict(label="ECMWF EC46", api="seas", ids=["ecmwf_ec46"], days=46, prior=[.8, .9, 1, 1, 1, 1]),
    "ifs": dict(label="ECMWF IFS ENS", api="ens", ids=["ecmwf_ifs025"], days=15, prior=[1, 1, 1, .95, 0, 0]),
    "aifs": dict(label="ECMWF AIFS ENS", api="ens", ids=["ecmwf_aifs025"], days=15, prior=[.9, .9, .9, .85, 0, 0]),
    "gfs": dict(label="NOAA GFS ENS", api="ens", ids=["gfs_seamless", "gfs05"], days=35, prior=[.75, .7, .65, .5, .4, .35]),
    "gem": dict(label="GEM ENS", api="ens", ids=["gem_global"], days=32, prior=[.6, .55, .5, .4, .3, .3]),
}
KIND = {"temperature_2m": "inst", "dew_point_2m": "inst", "pressure_msl": "inst", "cloud_cover": "inst",
        "wind_speed_10m": "inst", "cape": "inst", "precipitation": "sum", "showers": "sum", "wind_gusts_10m": "max"}
A_PRIOR = {"rain": [1, .95, .8, .55, .3, .2], "temp": [1, .95, .85, .65, .4, .3]}


def lbin(lead):
    for i, u in enumerate(BAND_UB):
        if lead <= u:
            return i
    return len(BAND_UB) - 1


def fl(x):
    try:
        return float(x)
    except Exception:
        return None


def r1(x, k=1):
    return None if x is None else round(x, k)


def clip(x, a, b):
    return max(a, min(b, x))


# ---------------------------------------------------------------- شبكة
def get_json(url, tries=3, to=90):
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=to) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:160]
            last = RuntimeError(f"HTTP {e.code} {body}")
            if e.code == 429 or e.code >= 500:
                time.sleep(4 * (i + 1))
                continue
            raise last
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(str(last))


def ep(s):
    return int(datetime.strptime(s[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc).timestamp() // 60)


def members(block, var):
    out = []
    for k in sorted(block):
        v = block[k]
        if k == "time" or not isinstance(v, list):
            continue
        rest = k[len(var):]
        if k == var or (k.startswith(var + "_") and ("member" in rest or rest[1:].startswith(("ecmwf", "gfs", "gem", "ec46")))):
            out.append(v)
    return out


def binize(times, vals, kind, ep0):
    out = [None] * N
    if not times:
        return out
    ts = times
    step = min([b - a for a, b in zip(ts, ts[1:]) if b > a] or [60])
    need = max(1, 360 // step)
    acc = {}
    for t, v in zip(ts, vals):
        if v is None:
            continue
        off = t - ep0
        if kind == "inst":
            if off % 360 == 0 and 0 < off <= N * 360:
                out[off // 360 - 1] = v
            continue
        k = -((-off) // 360) - 1
        if 0 <= k < N:
            a = acc.setdefault(k, [])
            a.append(v)
    for k, a in acc.items():
        if len(a) >= need:
            out[k] = sum(a) if kind == "sum" else max(a)
    return out


def fetch_block(base, params, key, varlist, extra_names):
    """يجرب كل المتغيرات معاً ثم كل متغير على حدة، ويجرب أسماء بديلة للقسم الزمني."""
    blk_names = extra_names
    def one(vs):
        last = None
        for nm in blk_names:
            p = dict(params); p[nm] = ",".join(vs)
            try:
                d = get_json(base + "?" + urllib.parse.urlencode(p))
                b = d.get(nm) or d.get("hourly") or d.get("six_hourly")
                if b and "time" in b:
                    return b
                last = RuntimeError("no block")
            except Exception as e:
                last = e
        raise last
    try:
        return one(varlist)
    except Exception:
        merged = {}
        for v in varlist:
            try:
                b = one([v])
                merged.update({k: x for k, x in b.items() if k != "time"}); merged["time"] = b["time"]
            except Exception:
                pass
        if "time" not in merged:
            raise RuntimeError("all variables failed")
        return merged


def fetch_model(mk, lat, lon, ep0):
    m = MODELS[mk]
    if DEMO:
        return demo_members(mk, lat, lon)
    if m["api"] == "seas":
        base = "https://seasonal-api.open-meteo.com/v1/seasonal"
        vs = ["temperature_2m", "precipitation", "showers", "wind_speed_10m", "wind_gusts_10m", "cloud_cover", "dew_point_2m", "pressure_msl"]
        names = ["six_hourly", "hourly"]
    else:
        base = "https://ensemble-api.open-meteo.com/v1/ensemble"
        vs = ["temperature_2m", "precipitation", "wind_speed_10m", "wind_gusts_10m", "cloud_cover", "dew_point_2m", "pressure_msl", "cape"]
        names = ["hourly"]
    err = None
    for mid in m["ids"]:
        try:
            params = {"latitude": lat, "longitude": lon, "models": mid, "timezone": "GMT",
                      "forecast_days": min(m["days"], 46 if m["api"] == "seas" else 35), "wind_speed_unit": "kmh"}
            blk = fetch_block(base, params, None, vs, names)
            ts = [ep(t) for t in blk["time"]]
            res = {}
            for v in vs:
                arrs = [binize(ts, a, KIND[v], ep0) for a in members(blk, v)]
                arrs = [a for a in arrs if any(x is not None for x in a)]
                if arrs:
                    res[v] = arrs
            if "precipitation" in res:
                return res
            err = RuntimeError("no precipitation")
        except Exception as e:
            err = e
    raise err


def archive_daily(lat, lon, d0, d1, vars_):
    if DEMO:
        return demo_archive(lat, lon, d0, d1)
    q = urllib.parse.urlencode({"latitude": lat, "longitude": lon, "start_date": d0, "end_date": d1,
                                "daily": ",".join(vars_), "timezone": "GMT"})
    d = get_json("https://archive-api.open-meteo.com/v1/archive?" + q).get("daily", {})
    return d


# ---------------------------------------------------------------- إحصاء
def wq(vs, ws, qs):
    p = sorted((v, w) for v, w in zip(vs, ws) if v is not None and w > 0)
    if not p:
        return [None] * len(qs)
    tot = sum(w for _, w in p); cum = 0; mids = []
    for v, w in p:
        mids.append((cum + w / 2) / tot); cum += w
    out = []
    for q in qs:
        if q <= mids[0]:
            out.append(p[0][0]); continue
        if q >= mids[-1]:
            out.append(p[-1][0]); continue
        j = bisect.bisect_left(mids, q)
        f = (q - mids[j - 1]) / (mids[j] - mids[j - 1]) if mids[j] > mids[j - 1] else 0
        out.append(p[j - 1][0] + f * (p[j][0] - p[j - 1][0]))
    return out


def wprob(vs, ws, thr):
    t = sum(ws)
    return None if t <= 0 else sum(w for v, w in zip(vs, ws) if v >= thr) / t


def gather(S, i, lead, WT):
    vs, ws = [], []
    for m, arrs in S.items():
        w = WT[m][lbin(lead)]
        if w <= 0:
            continue
        vals = [a[i] for a in arrs if a[i] is not None]
        if vals:
            vs += vals; ws += [w / len(vals)] * len(vals)
    return vs, ws


def daily_of(arr, fn):
    out = []
    for d in range(D):
        seg = arr[4 * d:4 * d + 4]
        out.append(None if any(x is None for x in seg) else fn(seg))
    return out


def mean(x):
    return sum(x) / len(x)


# ---------------------------------------------------------------- مناخ ERA5
def doy(dt):
    return min(dt.timetuple().tm_yday, 366) - 1


def pct(sorted_v, q):
    if not sorted_v:
        return None
    i = q * (len(sorted_v) - 1); a = int(i); b = min(a + 1, len(sorted_v) - 1)
    return sorted_v[a] + (i - a) * (sorted_v[b] - sorted_v[a])


def build_clim(lat, lon, today):
    y1 = today.year - 1; y0 = y1 - 19
    raw = archive_daily(lat, lon, f"{y0}-01-01", f"{y1}-12-31",
                        ["temperature_2m_max", "temperature_2m_min", "temperature_2m_mean", "precipitation_sum"])
    T = raw.get("time", [])
    ser = {k: raw.get(k, []) for k in ("temperature_2m_max", "temperature_2m_min", "temperature_2m_mean", "precipitation_sum")}
    by = {}  # (سنة، يوم) -> قيم
    for i, t in enumerate(T):
        dt = datetime.strptime(t, "%Y-%m-%d")
        by[(dt.year, doy(dt))] = tuple(fl(ser[k][i]) for k in ser)
    keys = ["tx", "tn", "tm", "pr"]
    out = {"d": {k: [None] * 366 for k in ("txm", "txlo", "txhi", "tnm", "tnlo", "tnhi", "tmm", "prm", "f1", "f5", "f10", "f25")},
           "w": {k: [None] * 366 for k in ("tm", "t33", "t67", "pm", "f1", "f10", "f25")}, "made": today.strftime("%Y-%m-%d")}
    try:
        out["off"] = hourly_offsets(lat, lon, today)
    except Exception as e:
        print("! تعويض العينات", e); out["off"] = None
    for s in range(366):
        win = [(s + o) % 366 for o in range(-7, 8)]
        col = {i: [] for i in range(4)}
        for y in range(y0, y1 + 1):
            for dd in win:
                v = by.get((y, dd))
                if v:
                    for i in range(4):
                        if v[i] is not None:
                            col[i].append(v[i])
        if not col[0]:
            continue
        for i, (a, b, c) in enumerate([("txm", "txlo", "txhi"), ("tnm", "tnlo", "tnhi")]):
            sv = sorted(col[i]); out["d"][a][s] = mean(sv); out["d"][b][s] = pct(sv, .1); out["d"][c][s] = pct(sv, .9)
        out["d"]["tmm"][s] = mean(col[2]); pr = col[3]
        out["d"]["prm"][s] = mean(pr) if pr else None
        for t in RAIN_T:
            out["d"]["f%d" % t][s] = sum(1 for x in pr if x >= t) / len(pr) if pr else None
        tms, prs = [], []
        for y in range(y0, y1 + 1):
            for o in range(-3, 4):
                seg = [by.get((y, (s + o + k) % 366)) for k in range(7)]
                if all(seg):
                    a = [x[2] for x in seg if x[2] is not None]; p = [x[3] for x in seg if x[3] is not None]
                    if len(a) == 7 and len(p) == 7:
                        tms.append(mean(a)); prs.append(sum(p))
        if tms:
            st = sorted(tms)
            out["w"]["tm"][s] = mean(tms); out["w"]["t33"][s] = pct(st, 1 / 3); out["w"]["t67"][s] = pct(st, 2 / 3)
            out["w"]["pm"][s] = mean(prs)
            for t in (1, 10, 25):
                out["w"]["f%d" % t][s] = sum(1 for x in prs if x >= t) / len(prs)
    return out


def hourly_offsets(lat, lon, today):
    """عيّنات 6 ساعات (06/12/18/24 UTC) تفوّت الصغرى الحقيقية وقد تفوّت العظمى: نقيس الفرق تاريخياً من ERA5 الساعي ونصحّح به."""
    if DEMO:
        return None
    y1 = today.year - 1; y0 = y1 - 2
    q = urllib.parse.urlencode({"latitude": lat, "longitude": lon, "start_date": f"{y0}-01-01", "end_date": f"{y1}-12-31",
                                "hourly": "temperature_2m", "timezone": "GMT"})
    h = get_json("https://archive-api.open-meteo.com/v1/archive?" + q, to=180).get("hourly", {})
    days = {}
    for t, v in zip(h.get("time", []), h.get("temperature_2m", [])):
        if v is not None:
            days.setdefault(t[:10], {})[int(t[11:13])] = v
    acc = [[[], []] for _ in range(366)]
    for ds, hs in days.items():
        dt = datetime.strptime(ds, "%Y-%m-%d"); nx = days.get((dt + timedelta(days=1)).strftime("%Y-%m-%d"))
        if len(hs) < 24 or not nx or 0 not in nx:
            continue
        sm = [hs[6], hs[12], hs[18], nx[0]]; i = doy(dt)
        acc[i][0].append(max(hs.values()) - max(sm)); acc[i][1].append(min(hs.values()) - min(sm))
    res = {"mx": [None] * 366, "mn": [None] * 366}
    for s in range(366):
        for j, k in enumerate(("mx", "mn")):
            v = [x for o in range(-10, 11) for x in acc[(s + o) % 366][j]]
            res[k][s] = round(mean(v), 2) if v else None
    return res


def cl(C, grp, key, dt):
    try:
        return C[grp][key][doy(dt)]
    except Exception:
        return None


# ---------------------------------------------------------------- بيانات وهمية للاختبار
import zlib
CTX = {}


def _tbase(dt):
    return 24 + 14 * math.sin(2 * math.pi * (doy(dt) - 110) / 365)


def demo_members(mk, lat, lon):
    m = MODELS[mk]; T0 = CTX["T0"]
    nm = {"ec46": 51, "ifs": 51, "aifs": 51, "gfs": 31, "gem": 21}[mk]
    com = random.Random(zlib.crc32(f"{lat}{T0.date()}".encode()))
    rnd = random.Random(zlib.crc32(f"{mk}{lat}{T0.date()}".encode()))
    valid = min(N, (m["days"] - 1) * 4)
    wet = [com.random() < .18 for _ in range(D)]
    anom = []; a = 0
    for _ in range(D):
        a = .85 * a + com.gauss(0, 1.2); anom.append(a)
    res = {v: [] for v in ["temperature_2m", "precipitation", "wind_speed_10m", "wind_gusts_10m", "cloud_cover", "dew_point_2m", "pressure_msl"]}
    if mk == "ec46":
        res["showers"] = []
    else:
        res["cape"] = []
    for _ in range(nm):
        arr = {k: [None] * N for k in res}
        for k in range(valid):
            d = k // 4; dt = T0 + timedelta(hours=6 * (k + 1)); lead = d + 1
            sp = .3 + .25 * lead ** .5
            hr = dt.hour
            diur = 7 * math.sin(2 * math.pi * ((hr + 3 - 9) / 24))
            t = _tbase(dt) + diur + (anom[d] * (1 if lead < 20 else .5)) + rnd.gauss(0, sp)
            w = wet[d] if rnd.random() < clip(1 - lead / 60, .35, .95) else rnd.random() < .18
            pr = rnd.expovariate(1 / 4) if (w and rnd.random() < .5) else 0.0
            arr["temperature_2m"][k] = t
            arr["precipitation"][k] = pr
            arr["wind_speed_10m"][k] = max(0, 14 + 6 * anom[d] / 3 + rnd.gauss(0, 4))
            arr["wind_gusts_10m"][k] = arr["wind_speed_10m"][k] * 1.7 + rnd.random() * 8
            arr["cloud_cover"][k] = clip(15 + (50 if w else 0) + rnd.gauss(0, 18), 0, 100)
            arr["dew_point_2m"][k] = t - 18 + (6 if w else 0) + rnd.gauss(0, 2)
            arr["pressure_msl"][k] = 1012 - 1.5 * anom[d] + rnd.gauss(0, 2)
            if mk == "ec46":
                arr["showers"][k] = pr * .4 if pr > 3 else 0.0
            else:
                arr["cape"][k] = (rnd.random() * 1200 if w else rnd.random() * 150)
        for k in res:
            res[k].append(arr[k])
    return res


def demo_archive(lat, lon, d0, d1):
    a = datetime.strptime(d0, "%Y-%m-%d"); b = datetime.strptime(d1, "%Y-%m-%d")
    out = {"time": [], "temperature_2m_max": [], "temperature_2m_min": [], "temperature_2m_mean": [], "precipitation_sum": []}
    x = a
    while x <= b:
        r = random.Random(zlib.crc32(f"{lat}{x.date()}".encode()))
        tm = _tbase(x) + r.gauss(0, 2)
        out["time"].append(x.strftime("%Y-%m-%d"))
        out["temperature_2m_mean"].append(tm); out["temperature_2m_max"].append(tm + 8); out["temperature_2m_min"].append(tm - 8)
        out["precipitation_sum"].append(r.expovariate(1 / 5) if r.random() < .08 else 0.0)
        x += timedelta(days=1)
    return out


# ---------------------------------------------------------------- البناء لموقع واحد
def build_loc(PMs, C, WT, cal, T0, now):
    lead0 = (T0 - now).total_seconds() / 86400
    ls = lambda k: lead0 + (k + 1) / 4
    ld = lambda d: lead0 + d + .5
    dts = lambda d: T0 + timedelta(days=d)
    oc = lambda kind, d: ((C.get("off") or {}).get(kind) or [0] * 366)[doy(dts(d))] or 0
    bias = cal.get("bias", {}); aR = cal["a_rain"]; aT = cal["a_temp"]
    out = {}
    # --- سلاسل 6 ساعات
    st = {}
    for var, key in [("temperature_2m", "temp"), ("wind_speed_10m", "wind"), ("wind_gusts_10m", "gust"),
                     ("cloud_cover", "cloud"), ("dew_point_2m", "dew"), ("pressure_msl", "pres")]:
        S = {m: pm[var] for m, pm in PMs.items() if var in pm}
        if var == "temperature_2m":  # تصحيح الانحياز المتعلَّم
            S = {m: [[None if x is None else x - bias.get(m, [0] * 6)[lbin(ls(k))] for k, x in enumerate(a)] for a in ar] for m, ar in S.items()}
        arrs = [[] for _ in QS]
        for k in range(N):
            vs, ws = gather(S, k, ls(k), WT); q = wq(vs, ws, QS)
            for i in range(len(QS)):
                arrs[i].append(r1(q[i]))
        st[key] = arrs
    S = {m: pm["precipitation"] for m, pm in PMs.items() if "precipitation" in pm}
    rq = [[] for _ in range(4)]
    for k in range(N):
        vs, ws = gather(S, k, ls(k), WT); q = wq(vs, ws, [.5, .75, .9, .95])
        for i in range(4):
            rq[i].append(r1(q[i], 2))
    st["rain"] = rq
    # --- أعلام الرعد لكل عضو
    flags = {}
    for m, pm in PMs.items():
        pr = pm.get("precipitation", [])
        if "cape" in pm:
            cp = pm["cape"]
            flags[m] = ("C", [[None if (c is None or p is None) else int(c >= 400 and p >= .2) for c, p in zip(ca, pa)] for ca, pa in zip(cp, pr)])
        elif "showers" in pm:
            flags[m] = ("S", [[None if x is None else int(x >= .3) for x in a] for a in pm["showers"]])
    # --- تجميع يومي لكل عضو
    DM = {}
    for m, pm in PMs.items():
        e = {}
        if "precipitation" in pm:
            e["rain"] = [daily_of(a, sum) for a in pm["precipitation"]]
        if "temperature_2m" in pm:
            b0 = bias.get(m, [0] * 6)
            fx = lambda a, kind, fn: [None if x is None else x - b0[lbin(ld(d))] + oc(kind, d) for d, x in enumerate(daily_of(a, fn))]
            e["tmax"] = [fx(a, "mx", max) for a in pm["temperature_2m"]]
            e["tmin"] = [fx(a, "mn", min) for a in pm["temperature_2m"]]
            raw = [daily_of(a, mean) for a in pm["temperature_2m"]]
            e["tmean_raw"] = raw
            b = bias.get(m, [0] * 6)
            e["tmean"] = [[None if x is None else x - b[lbin(ld(d))] for d, x in enumerate(a)] for a in raw]
        for var, key, fn in [("wind_speed_10m", "wind", mean), ("wind_gusts_10m", "gust", max), ("cloud_cover", "cloud", mean),
                             ("dew_point_2m", "dew", mean), ("pressure_msl", "pres", mean)]:
            if var in pm:
                e[key] = [daily_of(a, fn) for a in pm[var]]
        if m in flags:
            e["th"] = [daily_of(a, max) for a in flags[m][1]]
        DM[m] = e
    G = lambda key: {m: e[key] for m, e in DM.items() if key in e}
    def dq(key, qs=QS):
        o = [[] for _ in qs]; S = G(key)
        for d in range(D):
            vs, ws = gather(S, d, ld(d), WT); q = wq(vs, ws, qs)
            for i in range(len(qs)):
                o[i].append(r1(q[i], 2 if key == "rain" else 1))
        return o
    def dprob(key, thrs, shrink=None):
        o = {t: [] for t in thrs}; S = G(key)
        for d in range(D):
            vs, ws = gather(S, d, ld(d), WT)
            for t in thrs:
                p = wprob(vs, ws, t) if vs else None
                if p is not None and shrink:
                    c = cl(C, "d", "f%d" % t, dts(d)); a = aR[lbin(ld(d))]
                    if c is not None:
                        p = a * p + (1 - a) * c
                o[t].append(r1(None if p is None else 100 * p))
        return [o[t] for t in thrs]
    dd = {"rain": dq("rain"), "p": dprob("rain", RAIN_T, True), "tmax": dq("tmax"), "tmin": dq("tmin"),
          "wind": dq("wind"), "gust": dq("gust"), "gp": dprob("gust", [40, 60]), "cloud": dq("cloud"), "dew": dq("dew"), "pres": dq("pres")}
    # رعد
    th = []; ths = []
    for d in range(D):
        vs, ws = gather(G("th"), d, ld(d), WT)
        th.append(r1(None if not vs else 100 * wprob(vs, ws, .5)))
        wc = sum(WT[m][lbin(ld(d))] for m in flags if flags[m][0] == "C" and m in DM and DM[m].get("th") and any(a[d] is not None for a in DM[m]["th"]))
        wt = sum(WT[m][lbin(ld(d))] for m in flags if m in DM and DM[m].get("th") and any(a[d] is not None for a in DM[m]["th"]))
        ths.append(1 if wt > 0 and wc / wt >= .5 else 0)
    dd["th"] = th; dd["ths"] = ths
    # مطر تراكمي
    cums = {}
    for m, e in DM.items():
        if "rain" in e:
            arrs = []
            for a in e["rain"]:
                run = 0; c = []
                for x in a:
                    if x is None or (c and c[-1] is None):
                        c.append(None)
                    else:
                        run += x; c.append(run)
                arrs.append(c)
            cums[m] = arrs
    cq = [[] for _ in range(3)]
    for d in range(D):
        vs, ws = gather(cums, d, ld(d), WT); q = wq(vs, ws, [.1, .5, .9])
        for i in range(3):
            cq[i].append(r1(q[i]))
    dd["cum"] = cq
    # مناخ
    cc = {"txm": [], "txlo": [], "txhi": [], "tnm": [], "tnlo": [], "tnhi": [], "tmm": [], "prm": [], "f": [[], [], [], []], "cum": []}
    run = 0
    for d in range(D):
        for k in ("txm", "txlo", "txhi", "tnm", "tnlo", "tnhi", "tmm", "prm"):
            cc[k].append(r1(cl(C, "d", k, dts(d))))
        for i, t in enumerate(RAIN_T):
            v = cl(C, "d", "f%d" % t, dts(d)); cc["f"][i].append(None if v is None else round(100 * v, 1))
        run += cl(C, "d", "prm", dts(d)) or 0; cc["cum"].append(r1(run))
    dd["c"] = cc
    out["s"] = st; out["d"] = dd
    # --- أسابيع
    wk = []
    for w in range(D // 7):
        days = range(7 * w, 7 * w + 7); lead = ld(7 * w + 3); b = lbin(lead)
        start = dts(7 * w)
        def wv(key, fn):
            o = {}
            for m, e in DM.items():
                if key in e:
                    o[m] = [[(None if any(a[d] is None for a in [arr] for d in days) else fn([arr[d] for d in days])) for arr in e[key]]]
            return o
        def wg(S):
            vs, ws = [], []
            for m, arrs in S.items():
                w_ = WT[m][b]; v = [x for x in arrs[0] if x is not None]
                if w_ > 0 and v:
                    vs += v; ws += [w_ / len(v)] * len(v)
            return vs, ws
        item = {"i": w, "d0": start.strftime("%Y-%m-%d"), "d1": (start + timedelta(days=6)).strftime("%Y-%m-%d")}
        tv, tw = wg(wv("tmean", mean)); ctm = cl(C, "w", "tm", start)
        if tv:
            q = wq(tv, tw, [.1, .5, .9])
            t = {"m": r1(q[1]), "lo": r1(q[0]), "hi": r1(q[2]), "clim": r1(ctm)}
            t33, t67 = cl(C, "w", "t33", start), cl(C, "w", "t67", start)
            if t33 is not None and t67 is not None:
                pb = sum(x for v, x in zip(tv, tw) if v < t33) / sum(tw); pa = sum(x for v, x in zip(tv, tw) if v > t67) / sum(tw)
                a = aT[b]; t["terc"] = [round(100 * (a * pb + (1 - a) / 3), 1), round(100 * (a * (1 - pb - pa) + (1 - a) / 3), 1), round(100 * (a * pa + (1 - a) / 3), 1)]
            item["t"] = t
        rv, rw = wg(wv("rain", sum))
        if rv:
            q = wq(rv, rw, [.1, .5, .9]); r = {"q": [r1(x) for x in q], "clim": r1(cl(C, "w", "pm", start)), "p": [], "c": []}
            for t in (1, 10, 25):
                p = wprob(rv, rw, t); c = cl(C, "w", "f%d" % t, start); a = aR[b]
                r["p"].append(r1(100 * (a * p + (1 - a) * c if c is not None else p))); r["c"].append(r1(None if c is None else 100 * c))
            item["r"] = r
        for key, nm in (("wind", "wd"), ("cloud", "cl"), ("dew", "dw")):
            v, x = wg(wv(key, mean)); item[nm] = r1(wq(v, x, [.5])[0]) if v else None
        v, x = wg(wv("th", sum)); item["th"] = r1(sum(a * b_ for a, b_ in zip(v, x)) / sum(x)) if v else None
        item["a"] = round((aR[b] + aT[b]) / 2, 2)
        wk.append(item)
    out["wk"] = wk
    # --- مقارنة النماذج
    mod = {}
    for m, e in DM.items():
        if "rain" not in e:
            continue
        p1, rm, tx = [], [], []
        for d in range(D):
            v = [a[d] for a in e["rain"] if a[d] is not None]
            p1.append(r1(100 * sum(1 for x in v if x >= 1) / len(v)) if v else None)
            rm.append(r1(mean(v), 2) if v else None)
            t = [a[d] for a in e.get("tmax", []) if a[d] is not None]
            tx.append(r1(mean(t)) if t else None)
        mod[m] = {"label": MODELS[m]["label"], "n": len(e["rain"]), "days": max([d + 1 for d in range(D) if p1[d] is not None] or [0]), "p1": p1, "rm": rm, "tx": tx}
    out["mod"] = mod
    # --- سجل للتعلم
    rows = []
    for m, e in DM.items():
        if "rain" not in e or "tmean_raw" not in e:
            continue
        for d in range(D):
            v = [a[d] for a in e["rain"] if a[d] is not None]; t = [a[d] for a in e["tmean_raw"] if a[d] is not None]
            if v and t:
                rows.append((m, dts(d).strftime("%Y-%m-%d"), d + 1, round(mean(v), 2), round(sum(1 for x in v if x >= 1) / len(v), 3), round(mean(t), 2)))
    return out, rows


# ---------------------------------------------------------------- التعلّم الذاتي
LOGH = ["run", "loc", "model", "day", "lead", "pm", "pp", "tm"]


def read_log(p):
    if not os.path.exists(p):
        return []
    try:
        with gzip.open(p, "rt", encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def write_log(p, rows):
    with gzip.open(p, "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f); w.writerow(LOGH); w.writerows(rows)


def prior_cal():
    return {"w": {m: [1.0] * 6 for m in MODELS}, "bias": {m: [0.0] * 6 for m in MODELS}, "a_rain": list(A_PRIOR["rain"]),
            "a_temp": list(A_PRIOR["temp"]), "n": {m: [0] * 6 for m in MODELS}, "ss": {m: [None] * 6 for m in MODELS}, "n_a": [0] * 6, "updated": None}


def learn(log, CL, now):
    cal = prior_cal()
    cut = (now - timedelta(days=6)).strftime("%Y-%m-%d")
    rows = [r for r in log if r["day"] <= cut and r["loc"] in LOCS and r["model"] in MODELS and fl(r["tm"]) is not None]
    if len(rows) < 60:
        return cal
    obs = {}
    for loc in {r["loc"] for r in rows}:
        ds = sorted(r["day"] for r in rows if r["loc"] == loc)
        try:
            d = archive_daily(LOCS[loc][0], LOCS[loc][1], ds[0], ds[-1], ["temperature_2m_mean", "precipitation_sum"])
            obs[loc] = {t: (fl(a), fl(b)) for t, a, b in zip(d.get("time", []), d.get("temperature_2m_mean", []), d.get("precipitation_sum", []))}
        except Exception:
            pass
    E = {}  # (model, band) -> [(tm, o_t, c_t, pp, y, c_f)]
    B = {}  # (run, loc, day) -> [(model, band, tm, pp)]
    meta = {}
    for r in rows:
        o = obs.get(r["loc"], {}).get(r["day"])
        if not o or o[0] is None or o[1] is None:
            continue
        dt = datetime.strptime(r["day"], "%Y-%m-%d"); C = CL.get(r["loc"], {})
        ct, cf = cl(C, "d", "tmm", dt), cl(C, "d", "f1", dt)
        if ct is None or cf is None:
            continue
        b = lbin(int(float(r["lead"]))); tm, pp = fl(r["tm"]), fl(r["pp"])
        E.setdefault((r["model"], b), []).append((tm, o[0], ct, pp, 1.0 if o[1] >= 1 else 0.0, cf))
        B.setdefault((r["run"], r["loc"], r["day"]), []).append((r["model"], b, tm, pp))
        meta[(r["run"], r["loc"], r["day"])] = (o[0], ct, 1.0 if o[1] >= 1 else 0.0, cf)
    for (m, b), v in E.items():
        n = len(v); ne = n / 8.0
        bias = mean([x[0] - x[1] for x in v]) * ne / (ne + 5)
        mae_m = mean([abs(x[0] - bias - x[1]) for x in v]); mae_c = mean([abs(x[2] - x[1]) for x in v])
        br_m = mean([(x[3] - x[4]) ** 2 for x in v]); br_c = mean([(x[5] - x[4]) ** 2 for x in v])
        ss = []
        if mae_c > 0:
            ss.append(1 - mae_m / mae_c)
        if br_c > 0:
            ss.append(1 - br_m / br_c)
        S = mean(ss) if ss else 0.0
        f = clip(1 + 1.5 * S, .4, 1.6)
        cal["w"][m][b] = round(1 + ne / (ne + 40) * (f - 1), 3)
        cal["bias"][m][b] = round(bias, 2); cal["n"][m][b] = n; cal["ss"][m][b] = round(clip(S, -1, 1), 3)
    num_r = [0.0] * 6; den_r = [1e-9] * 6; num_t = [0.0] * 6; den_t = [1e-9] * 6; cnt = [0] * 6
    for key, lst in B.items():
        o_t, ct, y, cf = meta[key]
        for b in set(x[1] for x in lst):
            sub = [x for x in lst if x[1] == b]
            w = [MODELS[x[0]]["prior"][b] for x in sub]
            if sum(w) <= 0:
                continue
            p = sum(wi * x[3] for wi, x in zip(w, sub)) / sum(w)
            bm = sum(wi * (x[2] - cal["bias"][x[0]][b]) for wi, x in zip(w, sub)) / sum(w)
            num_r[b] += (p - cf) * (y - cf); den_r[b] += (p - cf) ** 2
            num_t[b] += (bm - ct) * (o_t - ct); den_t[b] += (bm - ct) ** 2; cnt[b] += 1
    for b in range(6):
        ne = cnt[b] / 8.0
        for nm, num, den in (("a_rain", num_r, den_r), ("a_temp", num_t, den_t)):
            a = clip(num[b] / den[b], 0, 1) if cnt[b] else A_PRIOR[nm[2:]][b]
            cal[nm][b] = round((15 * A_PRIOR[nm[2:]][b] + ne * a) / (15 + ne), 3)
        cal["n_a"][b] = cnt[b]
    cal["updated"] = now.strftime("%Y-%m-%d")
    return cal


# ---------------------------------------------------------------- الصفحة
PAGE = r"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#171a1f"><title>أُفق — توقعات المدى الممتد</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Readex+Pro:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--paper:#0e1b25;--card:#142633;--ink:#e1ebf1;--mut:#93a7b5;--grid:#243a49;--line:#39566a;--rain:#5b9bff;--hot:#ff7a5c;--cold:#5db4f0;--wind:#3fd0b8;--cloud:#9db0c2;--th:#b98cff;--dew:#5fd3c3;--pres:#9a92ff;--acc:#5fc2e0}
@media(prefers-color-scheme:light){:root{--paper:#e9eff1;--card:#f7fafb;--ink:#13232f;--mut:#566a77;--grid:#c9d4da;--line:#9db0bb;--rain:#1c5fd1;--hot:#d9482b;--cold:#2b83c6;--wind:#0b8a78;--cloud:#66788a;--th:#7a2fc0;--dew:#2c9a8a;--pres:#5b52c8;--acc:#0f5a73}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font:400 14px/1.7 "Readex Pro",Tahoma,sans-serif;font-variant-numeric:tabular-nums}
main{max-width:1200px;margin:auto;padding:14px 12px 40px}
h1{font:600 30px/1.2 "Readex Pro",Tahoma,sans-serif;margin:4px 0 2px;letter-spacing:-.3px}
h2{font:600 16px/1.4 inherit;margin:0 0 8px}
.sub{color:var(--mut);font-size:12.5px;margin:0 0 12px}
.bar{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}
.bar button,.tabs button{-webkit-tap-highlight-color:transparent;font:inherit;font-size:13px;color:var(--ink);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:5px 12px;cursor:pointer;min-height:36px}
.bar button.on,.tabs button.on{background:var(--acc);color:var(--paper);border-color:var(--acc);font-weight:500}
.tabs{display:flex;gap:6px;margin:10px 0;overflow-x:auto}
.lay button{border-inline-start:5px solid var(--c);border-radius:4px 8px 8px 4px}
.lay button:not(.on){opacity:.55}
#ro{position:sticky;top:0;z-index:5;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 10px;margin:8px 0;font-size:12.5px;min-height:52px}
#ro b{font-weight:600}#ro span{white-space:nowrap;margin-inline-end:10px;display:inline-block}
#sc{direction:ltr;overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px;padding-bottom:6px;-webkit-overflow-scrolling:touch}
#plots{position:relative;width:max-content;direction:ltr}
#cur{position:absolute;top:0;bottom:0;width:1.5px;background:var(--ink);opacity:.55;pointer-events:none;display:none;z-index:3}
.prow{display:flex;direction:ltr}
.yax{position:sticky;left:0;z-index:2;background:var(--card);flex:none}
.yl{font-size:10px;fill:var(--mut);text-anchor:end}
.pt{position:sticky;left:8px;width:max-content;direction:rtl;font-weight:600;font-size:13px;padding:8px 0 2px 52px;border-inline-start:0;color:var(--c)}
.pt small{font-weight:400;color:var(--mut);font-size:11px}
.gl{stroke:var(--grid);stroke-width:1}.dl{stroke:var(--grid);stroke-width:.5;opacity:.6}.wl{stroke:var(--line);stroke-width:1}
.bd{fill:var(--ink)}
.ax text{font-size:10px;fill:var(--mut);text-anchor:middle}.ax .rb{font-size:10.5px;fill:var(--ink);font-weight:500}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.big{font:600 26px/1 "Readex Pro";direction:ltr;display:inline-block}
.terc{display:flex;height:16px;border-radius:4px;overflow:hidden;margin:6px 0 2px;direction:ltr}.terc i{display:block;font:normal 10px/16px "Readex Pro";text-align:center;color:#fff;overflow:hidden}
.mut{color:var(--mut);font-size:12px}.tag{display:inline-block;font-size:11px;border:1px solid var(--line);border-radius:10px;padding:0 8px;color:var(--mut)}
.pb{display:grid;grid-template-columns:52px 1fr 40px;gap:6px;align-items:center;font-size:12px;margin:2px 0}.pb div{height:8px;background:var(--grid);border-radius:4px;position:relative;direction:ltr}.pb div i{position:absolute;inset:0 auto 0 0;border-radius:4px;background:var(--rain)}.pb div u{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--ink)}
.hm{direction:ltr;overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px}
.hr{display:flex;align-items:center;margin:3px 0;min-width:max-content}.hr>span{position:sticky;left:0;background:var(--card);width:150px;flex:none;font-size:12px;direction:rtl;text-align:right;padding-right:8px;z-index:1}
.hr>div{display:grid;grid-template-columns:repeat(45,20px);gap:1px}.hr>div i{display:block;height:22px}
table{border-collapse:collapse;width:100%;font-size:12.5px;direction:rtl}th,td{padding:5px 6px;border-bottom:1px solid var(--grid);text-align:center}th{font-weight:600;color:var(--mut)}td:first-child,th:first-child{text-align:right}
.tw{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:6px}
footer{color:var(--mut);font-size:12px;line-height:1.9;margin-top:16px}
.warn{background:color-mix(in srgb,var(--hot) 14%,var(--card));border:1px solid var(--hot);border-radius:8px;padding:6px 10px;font-size:12.5px;margin:8px 0}

body{padding-bottom:calc(72px + env(safe-area-inset-bottom))}
main{padding-top:calc(14px + env(safe-area-inset-top))}
#locs{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none}#locs button{white-space:nowrap;flex:none}
.tabs{position:fixed;left:0;right:0;bottom:0;z-index:9;margin:0;gap:0;background:var(--card);border-top:1px solid var(--line);padding:4px 6px calc(4px + env(safe-area-inset-bottom))}
.tabs button{flex:1;border:0;background:none;border-radius:8px;min-height:46px;font-size:12.5px;color:var(--mut)}
.tabs button.on{background:none;color:var(--ink);box-shadow:inset 0 -3px 0 var(--acc)}
#ro{top:env(safe-area-inset-top);font-size:12px;line-height:1.6;min-height:44px}
.pt{width:max-content;max-width:calc(100vw - 64px);white-space:normal;line-height:1.5;padding-left:50px}
.tl{fill:var(--ink);font-size:11px;font-weight:600;text-anchor:middle}.tl.lo{fill:var(--mut);font-weight:400}
.cm{fill:none;stroke:var(--ink);stroke-width:1;stroke-dasharray:4 3;opacity:.55}
.bar button{min-height:40px}
@media(max-width:600px){h1{font-size:26px}main{padding-left:8px;padding-right:8px}.cards{grid-template-columns:1fr}}
@media(prefers-reduced-motion:no-preference){#sc{scroll-behavior:smooth}}
</style></head><body><main>
<h1>أُفق</h1><p class="sub" id="sub"></p>
<div class="bar" id="locs"></div><div class="tabs" id="tabs"></div><div id="view"></div>
<footer id="foot"></footer></main>
<script>
const DATA=__DATA__,M=DATA.meta,ND=M.D,NS=M.N,T0=Date.parse(M.t0),LC='ar-IQ-u-nu-latn',TZ='Asia/Baghdad';
const $=s=>document.querySelector(s),BUB=[2,5,9,15,25,46],bandOf=d=>{for(let i=0;i<6;i++)if(d+1<=BUB[i])return i;return 5};
const fmt=(ms,o)=>new Date(ms).toLocaleString(LC,Object.assign({timeZone:TZ},o)),stepMs=k=>T0+(k+1)*6*36e5,dayMs=d=>T0+d*864e5+12*36e5;
const f1=v=>v==null?'—':(+v).toFixed(1),f0=v=>v==null?'—':Math.round(v),iso=s=>Date.parse(s+'T12:00:00Z');
const LAY=[['temp','الحرارة','--hot'],['rain','الأمطار','--rain'],['cloud','الغيوم','--cloud'],['wind','الرياح','--wind'],['th','الرعد','--th'],['prob','احتمال المطر','--rain'],['cum','المطر التراكمي','--rain'],['dew','نقطة الندى','--dew'],['pres','الضغط','--pres']];
let S={loc:Object.keys(DATA.locs)[0],view:'mg',thr:0,mv:'p1',z:15,lay:{temp:1,rain:1,cloud:1,wind:1,th:1,prob:0,cum:0,dew:0,pres:0}};
let PW=26,SW=6.5;const setScale=()=>{const w=Math.min(document.documentElement.clientWidth,1200)-24-44;PW=Math.max(5.5,w/S.z);SW=PW/4};
try{const o=JSON.parse(localStorage.getItem('ofuq2')||'{}');Object.assign(S,o);if(!DATA.locs[S.loc])S.loc=Object.keys(DATA.locs)[0]}catch(e){}
const save=()=>{try{localStorage.setItem('ofuq2',JSON.stringify(S))}catch(e){}};
const nice=m=>{if(!(m>0))return 1;const p=Math.pow(10,Math.floor(Math.log10(m))),f=m/p;return(f<=1?1:f<=2?2:f<=5?5:10)*p};
const ticks=(a,b,n)=>{const st=nice((b-a)/n),o=[];for(let v=Math.ceil(a/st-1e-9)*st;v<=b+1e-9;v+=st)o.push(+v.toFixed(3));return o};
const flat=a=>a.flat().filter(v=>v!=null),mx=(...a)=>Math.max(...flat(a)),mn=(...a)=>Math.min(...flat(a));
const path=(arr,fx,y)=>{let s='',pen=false;arr.forEach((v,i)=>{if(v==null){pen=false;return}s+=(pen?'L':'M')+fx(i).toFixed(1)+' '+y(v).toFixed(1);pen=true});return s};
const area=(lo,hi,fx,y)=>{const segs=[];let c=[];lo.forEach((v,i)=>{if(v==null||hi[i]==null){if(c.length)segs.push(c);c=[]}else c.push(i)});if(c.length)segs.push(c);
return segs.map(s=>'M'+s.map(i=>fx(i).toFixed(1)+' '+y(hi[i]).toFixed(1)).join('L')+'L'+s.slice().reverse().map(i=>fx(i).toFixed(1)+' '+y(lo[i]).toFixed(1)).join('L')+'Z').join('')};
const fxs=k=>k*SW+SW/2,fxd=d=>(d+.5)*PW;
const fan=(q,fx,y,c)=>`<path d="${area(q[0],q[4],fx,y)}" fill="var(${c})" fill-opacity=".16"/><path d="${area(q[1],q[3],fx,y)}" fill="var(${c})" fill-opacity=".3"/><path d="${path(q[2],fx,y)}" fill="none" stroke="var(${c})" stroke-width="1.8" stroke-linejoin="round"/>`;
const conf=b=>Math.round(50*(M.cal.a_rain[b]+M.cal.a_temp[b]));
function panel(title,c,h,lo,hi,unit,body){
const W=PW*ND,y=v=>h-(v-lo)/(hi-lo)*h,tk=ticks(lo,hi,4);let bg='',s=0;
for(let d=1;d<=ND;d++)if(d==ND||bandOf(d)!=bandOf(s)){bg+=`<rect x="${s*PW}" y="0" width="${(d-s)*PW}" height="${h}" class="bd" fill-opacity="${[0,.025,.05,.075,.1,.13][bandOf(s)]}"/>`;s=d}
let g='';for(let d=0;d<=ND;d++)g+=`<line x1="${d*PW}" x2="${d*PW}" y1="0" y2="${h}" class="${d%7==0?'wl':'dl'}"/>`;
tk.forEach(t=>{g+=`<line x1="0" x2="${W}" y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}" class="gl"/>`});
const lab=tk.map(t=>`<text x="40" y="${(y(t)+3).toFixed(1)}" class="yl">${t}</text>`).join('');
return `<div class="pt" style="--c:var(${c})">${title} <small>${unit}</small></div><div class="prow" style="height:${h}px"><svg class="yax" width="44" height="${h}">${lab}</svg><svg class="plot" width="${W}" height="${h}">${bg}${g}${body(y,h)}</svg></div>`}
function axis(L){const W=PW*ND;let t='',r='',s=0;
const sk=PW>=16?1:PW>=9?2:7;for(let d=0;d<ND;d++){const n=fmt(dayMs(d),{day:'numeric'}),w=fmt(dayMs(d),{weekday:'narrow'}),first=fmt(dayMs(d),{day:'numeric'})=='1';
if(d%sk==0)t+=`<text x="${fxd(d)}" y="30">${w}</text><text x="${fxd(d)}" y="42" style="font-weight:600;fill:var(--ink)">${n}</text>`;if(first||d==0)t+=`<text x="${d*PW+2}" y="58" style="text-anchor:start;fill:var(--acc);font-weight:600">${fmt(dayMs(d),{month:'long'})}</text>`}
for(let d=1;d<=ND;d++)if(d==ND||bandOf(d)!=bandOf(s)){const b=bandOf(s),w=(d-s)*PW;r+=`<rect x="${s*PW}" y="2" width="${w}" height="18" fill="var(--acc)" fill-opacity="${(.12+.6*conf(b)/100).toFixed(2)}" stroke="var(--paper)"/><text class="rb" x="${s*PW+w/2}" y="15">${w>70?'ثقة '+conf(b)+'٪':''}</text>`;s=d}
return `<div class="prow"><svg class="yax" width="44" height="62"><text x="40" y="15" class="yl">الثقة</text></svg><svg class="ax" width="${W}" height="62">${r}${t}</svg></div>`}
function bars(arr,h,y,c,thr){return arr.map((v,d)=>v==null?'':`<rect x="${(d*PW+2).toFixed(1)}" y="${y(v).toFixed(1)}" width="${PW-4}" height="${(h-y(v)).toFixed(1)}" fill="var(${c})" fill-opacity="${(.25+.75*v/100).toFixed(2)}"/>`).join('')}
const TCOL=['#0e5a35','#1f8f3a','#8cc63f','#e3ef4b','#ffd400','#ff9a00','#f0501e','#b3121a'],tc=t=>TCOL[Math.max(0,Math.min(7,Math.floor((t-2)/6)))];
const ic=(d,i)=>{const p=d.p[0][i],c=d.cloud[2][i];return d.th[i]>=50&&d.ths[i]?'⛈️':p>=50?'🌧️':p>=25?'🌦️':c>=70?'☁️':c>=35?'⛅':'☀️'};
const rh=(t,td)=>t==null||td==null?null:Math.min(100,100*Math.exp(17.625*td/(243.04+td)-17.625*t/(243.04+t)));
function build(L){const s=L.s,d=L.d,c=d.c;let h='';
const A=(k,fn)=>{if(S.lay[k])h+=fn()};
A('temp',()=>{const lo=Math.floor(mn(s.temp[0],c.tnm)-2),hi=Math.ceil(mx(s.temp[4],c.txm)+6);return panel('الحرارة على ارتفاع 2 م — الخط الأحمر الوسيط والظل نطاق 10–90٪','--hot',210,lo,hi,'°م',(y,H)=>{
let st='';for(let t=hi;t>=lo;t--)st+=`<stop offset="${(y(t)/H*100).toFixed(1)}%" stop-color="${tc(t)}"/>`;
const m=s.temp[2];let tx='';
if(PW>=20)for(let i=0;i<ND;i++){const a=d.tmax[2][i],b=d.tmin[2][i];tx+=`<text x="${fxd(i)}" y="15" text-anchor="middle" font-size="14">${ic(d,i)}</text>`;if(a!=null)tx+=`<text x="${fxd(i)}" y="${(y(a)-6).toFixed(1)}" class="tl">${Math.round(a)}</text>`;if(b!=null)tx+=`<text x="${fxd(i)}" y="${(y(b)+14).toFixed(1)}" class="tl lo">${Math.round(b)}</text>`}
return `<defs><linearGradient id="tg" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2="${H}">${st}</linearGradient></defs><path d="${area(s.temp[0],s.temp[4],fxs,y)}" fill="url(#tg)" fill-opacity=".3"/><path d="${area(m.map(()=>lo),m,fxs,y)}" fill="url(#tg)" fill-opacity=".85"/><path d="${path(m,fxs,y)}" fill="none" stroke="#ff2d2d" stroke-width="2" stroke-linejoin="round"/><path d="${path(c.txm,fxd,y)}" class="cm"/><path d="${path(c.tnm,fxd,y)}" class="cm"/>${tx}`})});
A('rain',()=>{const top=nice(Math.max(mx(s.rain[3]),2)),pr=d.p[S.thr],sc=v=>v==null?null:v*top/100;return panel('الأمطار — أعمدة 6 ساعات · بنفسجي: احتمال يوم ممطر · أزرق: رطوبة نسبية','--rain',160,0,top,'مم/6س',(y,H)=>[3,2,1,0].map((i,j)=>s.rain[i].map((v,k)=>v>.001?`<rect x="${(k*SW+.6).toFixed(1)}" y="${y(Math.min(v,top)).toFixed(1)}" width="${(SW-1.2).toFixed(1)}" height="${(H-y(Math.min(v,top))).toFixed(1)}" fill="var(--rain)" fill-opacity="${[.14,.28,.5,.9][j]}"/>`:'').join('')).join('')+`<path d="${path(pr.map(sc),fxd,y)}" fill="none" stroke="var(--th)" stroke-width="1.8"/><path d="${path(s.temp[2].map((t,k)=>sc(rh(t,s.dew[2][k]))),fxs,y)}" fill="none" stroke="var(--pres)" stroke-width="1.4"/>`)});
A('cloud',()=>panel('الغيوم — كلما أغمق الرمادي زادت الغيوم','--cloud',40,0,100,'٪',(y,H)=>s.cloud[2].map((v,k)=>v==null?'':`<rect x="${(k*SW).toFixed(1)}" y="0" width="${(SW+.5).toFixed(1)}" height="${H}" fill="var(--ink)" fill-opacity="${(v/130).toFixed(2)}"/>`).join('')));
A('wind',()=>{const top=nice(Math.max(mx(s.gust[3]),mx(s.wind[4]),20)*1.05);return panel('الرياح (نطاق السرعة) والهبّات (الخط المنقّط = وسيط الهبّات) وأعمدة احتمال هبّات ≥ 40','--wind',150,0,top,'كم/س',(y,H)=>fan(s.wind,fxs,y,'--wind')+`<path d="${path(s.gust[2],fxs,y)}" fill="none" stroke="var(--wind)" stroke-width="1.4" stroke-dasharray="2 3"/>`+d.gp[0].map((v,i)=>v==null?'':`<rect x="${(i*PW+8).toFixed(1)}" y="${(H-v*.25).toFixed(1)}" width="${PW-16}" height="${(v*.25).toFixed(1)}" fill="var(--wind)" fill-opacity=".5"/>`).join(''))});
A('th',()=>panel('ميل العواصف الرعدية — خلايا مصمتة = مؤشر CAPE (حتى ~15 يوماً)، مخططة = مؤشر زخات فقط','--th',110,0,100,'٪',(y,H)=>`<defs><pattern id="hat" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="3" height="6" fill="var(--th)"/></pattern></defs>`+d.th.map((v,i)=>v==null?'':`<rect x="${(i*PW+2).toFixed(1)}" y="${y(v).toFixed(1)}" width="${PW-4}" height="${(H-y(v)).toFixed(1)}" fill="${d.ths[i]?'var(--th)':'url(#hat)'}" fill-opacity="${d.ths[i]?(.3+.7*v/100).toFixed(2):(.35+.5*v/100).toFixed(2)}"/>`).join('')));
A('prob',()=>panel('احتمال يوم ممطر ≥ '+M.rain_t[S.thr]+' مم (الخط المتقطع = المناخ)','--rain',110,0,100,'٪',(y,H)=>bars(d.p[S.thr],H,y,'--rain')+`<path d="${path(c.f[S.thr],fxd,y)}" fill="none" stroke="var(--ink)" stroke-width="1.4" stroke-dasharray="4 3"/>`));
A('cum',()=>{const top=nice(Math.max(mx(d.cum[2]),mx(c.cum),1));return panel('المطر التراكمي (10–90٪ والوسيط) مقابل المناخ','--rain',110,0,top,'مم',(y,H)=>`<path d="${area(d.cum[0],d.cum[2],fxd,y)}" fill="var(--rain)" fill-opacity=".22"/><path d="${path(d.cum[1],fxd,y)}" fill="none" stroke="var(--rain)" stroke-width="2"/><path d="${path(c.cum,fxd,y)}" fill="none" stroke="var(--ink)" stroke-width="1.4" stroke-dasharray="4 3"/>`)});
A('dew',()=>panel('نقطة الندى (مؤشر رطوبة الكتل الهوائية)','--dew',110,Math.floor(mn(s.dew[0])-1),Math.ceil(mx(s.dew[4])+1),'°م',y=>fan(s.dew,fxs,y,'--dew')));
A('pres',()=>panel('الضغط عند سطح البحر','--pres',110,Math.floor(mn(s.pres[0])-1),Math.ceil(mx(s.pres[4])+1),'هكتوباسكال',y=>fan(s.pres,fxs,y,'--pres')));
return h}
function ro(L,k){const s=L.s,d=L.d,dy=Math.floor(k/4),q=a=>a[2][k];
$('#ro').innerHTML=`<b>${fmt(stepMs(k),{weekday:'long',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})}</b><br>
<span style="color:var(--hot)">حرارة ${f1(q(s.temp))}° (${f1(s.temp[0][k])}–${f1(s.temp[4][k])})</span><span style="color:var(--rain)">مطر/6س: وسيط ${f1(s.rain[0][k])} · 90٪ ${f1(s.rain[2][k])} مم</span><span style="color:var(--rain)">احتمال يوم ≥${M.rain_t[S.thr]} مم: ${f0(d.p[S.thr][dy])}٪ (مناخ ${f0(d.c.f[S.thr][dy])}٪)</span><span style="color:var(--wind)">رياح ${f0(q(s.wind))} · هبّات ${f0(s.gust[2][k])} كم/س</span><span style="color:var(--cloud)">غيوم ${f0(q(s.cloud))}٪</span><span style="color:var(--th)">رعد ${f0(d.th[dy])}٪${d.ths[dy]?'':' (مؤشر زخات)'}</span><span style="color:var(--dew)">ندى ${f1(q(s.dew))}°</span><span style="color:var(--pres)">ضغط ${f0(q(s.pres))}</span>`}
function mgView(L){setScale();const b=LAY.map(([k,n,c])=>`<button data-l="${k}" class="${S.lay[k]?'on':''}" style="--c:var(${c})">${n}</button>`).join('');
const th=M.rain_t.map((t,i)=>`<button data-t="${i}" class="${S.thr==i?'on':''}">≥ ${t} مم</button>`).join('');
const z=[7,15,45].map(n=>`<button data-z="${n}" class="${S.z==n?'on':''}">${n} يوماً</button>`).join('');
return `<div class="bar"><span class="mut" style="align-self:center">المعروض:</span>${z}</div><div class="bar lay">${b}</div><div class="bar"><span class="mut" style="align-self:center">عتبة المطر اليومي:</span>${th}</div>
<div id="ro">المس المخطط لقراءة القيم، واسحب أفقياً للتنقل بين الأيام.</div>
<div id="sc"><div id="plots">${axis(L)}${build(L)}<div id="cur"></div></div></div>
<p class="mut" style="margin:6px 0">الظل الفاتح = 10–90٪ من الأعضاء والداكن = 25–75٪. تزداد عتمة الخلفية مع بُعد المدى، والنسبة أعلى الجدول هي ثقة النظام المقاسة ذاتياً.</p>`}
function wkView(L){return `<div class="cards">`+L.wk.map(w=>{const b=bandOf(7*w.i+3),T=w.t,R=w.r,young=(M.cal.n_a[b]||0)<40;
const tc=T&&T.terc?`<div class="terc"><i style="width:${T.terc[0]}%;background:var(--cold)">${f0(T.terc[0])}</i><i style="width:${T.terc[1]}%;background:var(--cloud)">${f0(T.terc[1])}</i><i style="width:${T.terc[2]}%;background:var(--hot)">${f0(T.terc[2])}</i></div><div class="mut" style="display:flex;justify-content:space-between;direction:ltr"><span>أبرد من المعتاد</span><span>قريب</span><span>أدفأ</span></div>`:'';
const an=T&&T.clim!=null?T.m-T.clim:null;
const pb=R?[1,10,25].map((t,i)=>`<div class="pb"><span>≥ ${t} مم</span><div><i style="width:${R.p[i]||0}%"></i>${R.c[i]!=null?`<u style="left:${R.c[i]}%"></u>`:''}</div><span>${f0(R.p[i])}٪</span></div>`).join(''):'';
return `<div class="card"><h2>${fmt(iso(w.d0),{day:'numeric',month:'short'})} – ${fmt(iso(w.d1),{day:'numeric',month:'short'})}</h2>
<span class="tag">ثقة ${Math.round(w.a*100)}٪</span> ${young?'<span class="tag">معايرة أولية</span>':''}
${T?`<p style="margin:8px 0 0"><span class="big" style="color:${an>0?'var(--hot)':'var(--cold)'}">${an==null?'':(an>0?'+':'')+an.toFixed(1)+'°'}</span> <span class="mut">عن معدل الأسبوع (متوسط ${f1(T.m)}° · نطاق ${f1(T.lo)}–${f1(T.hi)})</span></p>${tc}`:''}
${R?`<p style="margin:10px 0 2px"><b>المطر:</b> وسيط ${f1(R.q[1])} مم (10–90٪: ${f1(R.q[0])}–${f1(R.q[2])}) · المناخ ${f1(R.clim)} مم</p>${pb}<div class="mut">العلامة السوداء = احتمال المناخ</div>`:''}
<p class="mut" style="margin:8px 0 0">رياح ${f0(w.wd)} كم/س · غيوم ${f0(w.cl)}٪ · ندى ${f1(w.dw)}° · أيام ميل رعدي متوقعة ${f1(w.th)}</p></div>`}).join('')+`</div>`}
function modView(L){const ks=Object.keys(L.mod),sel={p1:['احتمال مطر ≥ 1 مم (٪)',0,100],rm:['متوسط المطر اليومي (مم)',0,10],tx:['متوسط الحرارة العظمى (°م)',null,null]};
const chips=Object.entries(sel).map(([k,v])=>`<button data-m="${k}" class="${S.mv==k?'on':''}">${v[0]}</button>`).join('');
const all=ks.flatMap(k=>L.mod[k][S.mv]),lo=S.mv=='tx'?mn(all):0,hi=S.mv=='tx'?mx(all):sel[S.mv][2];
const rows=ks.map(k=>{const m=L.mod[k];return `<div class="hr"><span>${m.label}<br><small class="mut">${m.n} عضواً · ${m.days} يوماً</small></span><div>${m[S.mv].map((v,d)=>{const t=v==null?0:Math.max(0,Math.min(1,(v-lo)/((hi-lo)||1)))*100;const col=S.mv=='tx'?`color-mix(in srgb,var(--hot) ${t}%,var(--cold))`:`color-mix(in srgb,var(--rain) ${t}%,transparent)`;return `<i title="${fmt(dayMs(d),{weekday:'short',day:'numeric',month:'short'})}: ${f1(v)}" style="background:${v==null?'transparent':col};${v==null?'outline:1px dashed var(--grid)':''}"></i>`}).join('')}</div></div>`}).join('');
let ax='';for(let d=0;d<ND;d++)ax+=`<i style="height:auto;font-size:9px;text-align:center;color:var(--mut)">${fmt(dayMs(d),{day:'numeric'})}</i>`;
return `<div class="bar">${chips}</div><div class="hm"><div class="hr"><span></span><div>${ax}</div></div>${rows}</div>
<p class="mut">تظهر هنا التوقعات الخام لكل نموذج قبل الدمج، فالتباين بين الصفوف هو مقياس عدم اليقين الحقيقي. الخلايا الفارغة = خارج مدى النموذج.</p>`}
function calView(){const c=M.cal,ks=Object.keys(M.models),n=c.n_a.reduce((a,b)=>a+b,0);
const head=BUB.map((u,i)=>`<th>${M.band[i]} يوم</th>`).join('');
const row=(t,a,fn)=>`<tr><td>${t}</td>${a.map((_,i)=>`<td>${fn(i)}</td>`).join('')}</tr>`;
return `${n<200?'<div class="warn">المعايرة لا تزال في بدايتها: يحتاج النظام إلى أسابيع من التوقعات المتحقَّق منها كي تصبح الأوزان والانحيازات موثوقة. حتى ذلك الحين تُستخدم أوزان أولية متحفظة.</div>':''}
<div class="tw"><table><tr><th>المؤشر</th>${head}</tr>
${row('ثقة احتمالات المطر',BUB,i=>Math.round(100*c.a_rain[i])+'٪')}${row('ثقة الحرارة والفئات',BUB,i=>Math.round(100*c.a_temp[i])+'٪')}${row('عينات التحقق',BUB,i=>c.n_a[i])}
${ks.map(k=>row('وزن '+M.models[k].label+' (× معايرة)',BUB,i=>c.w[k][i]==1&&!c.n[k][i]?'—':'×'+c.w[k][i])).join('')}
${ks.map(k=>row('مهارة '+M.models[k].label+' مقابل المناخ',BUB,i=>c.ss[k][i]==null?'—':(c.ss[k][i]>=0?'+':'')+Math.round(100*c.ss[k][i])+'٪')).join('')}
${ks.map(k=>row('انحياز حرارة '+M.models[k].label,BUB,i=>c.n[k][i]?(c.bias[k][i]>0?'+':'')+c.bias[k][i]+'°':'—')).join('')}</table></div>
<p class="mut">آخر معايرة: ${c.updated||'لم تبدأ'}. كيف يتعلّم النظام: كل تشغيل يسجّل توقع كل نموذج لكل يوم مقبل، وبعد مرور 6 أيام على اليوم المستهدف تُقارن التوقعات بتحليل ERA5 المناخي. من ذلك يُقدَّر (1) وزن كل نموذج لكل نطاق مدى بحسب مهارته مقابل المناخ، (2) انحياز الحرارة، (3) معامل «الثقة» الذي يقرّب الاحتمالات من المناخ حيث لا مهارة فعلية. النتائج تُطبَّق تلقائياً في التشغيل التالي.</p>
${M.fails.length?`<div class="warn">تعذّر جلب: ${M.fails.join('، ')}</div>`:''}`}
function render(){const L=DATA.locs[S.loc];
$('#locs').innerHTML=Object.entries(DATA.locs).map(([k,v])=>`<button data-k="${k}" class="${k==S.loc?'on':''}">${v.ar}</button>`).join('');
$('#tabs').innerHTML=[['mg','الميتوغرام'],['wk','الأسابيع'],['md','النماذج'],['cal','المعايرة والمهارة']].map(([k,n])=>`<button data-v="${k}" class="${S.view==k?'on':''}">${n}</button>`).join('');
$('#view').innerHTML=S.view=='mg'?mgView(L):S.view=='wk'?wkView(L):S.view=='md'?modView(L):calView();
if(S.view=='mg'){const sc=$('#sc'),cur=$('#cur'),pl=$('#plots');
const mv=e=>{const t=e.touches?e.touches[0]:e,r=pl.getBoundingClientRect(),k=Math.max(0,Math.min(NS-1,Math.floor((t.clientX-r.left-44)/SW)));if(t.clientX-r.left<44)return;cur.style.display='block';cur.style.left=(44+k*SW+SW/2)+'px';ro(L,k)};
sc.addEventListener('pointermove',mv);sc.addEventListener('pointerdown',mv);}
save()}
document.addEventListener('click',e=>{const t=e.target.closest('button');if(!t)return;const d=t.dataset;
if(d.k)S.loc=d.k;else if(d.v)S.view=d.v;else if(d.l)S.lay[d.l]=S.lay[d.l]?0:1;else if(d.t!=null)S.thr=+d.t;else if(d.m)S.mv=d.m;else if(d.z)S.z=+d.z;else return;
const y=window.scrollY,sx=d.z?0:($('#sc')||{}).scrollLeft||0;render();window.scrollTo(0,y);if($('#sc'))$('#sc').scrollLeft=sx});
$('#sub').textContent='آخر تحديث '+M.updated+' بتوقيت العراق · '+Object.keys(M.models).length+' أنظمة تنبؤ جماعي · ابتداءً من '+fmt(T0,{day:'numeric',month:'long'})+' لمدة '+ND+' يوماً';
$('#foot').innerHTML='المصدر: بيانات ECMWF (EC46 وIFS وAIFS) وNOAA GFS وEnvironment Canada GEM عبر Open-Meteo، والتحقق بتحليل ERA5. بعد نحو 10–15 يوماً لا يوجد تنبؤ يومي موثوق: ما يُعرض من ذلك الحد هو ميل احتمالي (توزيع الأعضاء) قد يقارب المناخ، ولذلك تُقرَّب الاحتمالات من المناخ بمقدار «الثقة» المقاسة. الرعد بعد ~15 يوماً مؤشر زخات تقريبي وليس تنبؤاً بالبرق. القيم على شبكة 25–36 كم (لا تحل التفاصيل المحلية) والحرارة العظمى/الصغرى مستنتجة من عيّنات كل 6 ساعات. هذا عمل هواة وليس تحذيراً رسمياً.';
let lw=innerWidth;addEventListener('resize',()=>{if(innerWidth!=lw){lw=innerWidth;render()}});
render();
</script></body></html>"""


# ---------------------------------------------------------------- التشغيل
def jload(p, default):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def main():
    global DEMO
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state"); ap.add_argument("--out", default="site")
    ap.add_argument("--demo", action="store_true"); ap.add_argument("--now", default="")
    a = ap.parse_args(); DEMO = a.demo
    os.makedirs(a.state, exist_ok=True); os.makedirs(a.out, exist_ok=True)
    now = datetime.now(timezone.utc) if not a.now else datetime.fromisoformat(a.now).replace(tzinfo=timezone.utc)
    T0 = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    CTX["T0"] = T0; ep0 = int(T0.timestamp() // 60)
    extra = jload("locations.json", None)
    if extra:
        LOCS.clear(); LOCS.update({k: (float(v[0]), float(v[1]), v[2] if len(v) > 2 else k) for k, v in extra.items()})

    # المناخ (يُجدَّد كل 60 يوماً)
    cp = os.path.join(a.state, "clim.json"); CL = jload(cp, {})
    def stale(k):
        try:
            return (now.date() - datetime.strptime(CL[k]["made"], "%Y-%m-%d").date()).days > 60 or "off" not in CL[k]
        except Exception:
            return True
    need = [k for k in LOCS if k not in CL or stale(k)]
    def bc(k):
        try:
            return k, build_clim(LOCS[k][0], LOCS[k][1], now)
        except Exception as e:
            print("! مناخ", k, e); return k, None
    if need:
        print("… حساب المناخ لـ", len(need), "مواقع")
        with ThreadPoolExecutor(3) as ex:
            for k, c in ex.map(bc, need):
                if c:
                    CL[k] = c

    # التعلّم الذاتي من السجل
    lp = os.path.join(a.state, "log.csv.gz"); log = read_log(lp)
    try:
        cal = learn(log, CL, now)
    except Exception as e:
        print("! تعذّر التعلّم:", e); cal = prior_cal()
    WT = {m: [MODELS[m]["prior"][b] * cal["w"][m][b] for b in range(6)] for m in MODELS}
    print("✓ سجل:", len(log), "صف | معايرة:", cal["updated"] or "أولية")

    # جلب النماذج
    tasks = [(k, m) for k in LOCS for m in MODELS]
    def job(t):
        k, m = t
        try:
            return k, m, fetch_model(m, LOCS[k][0], LOCS[k][1], ep0), ""
        except Exception as e:
            return k, m, None, str(e)[:120]
    PM = {k: {} for k in LOCS}; fails = []
    with ThreadPoolExecutor(4) as ex:
        for k, m, r, err in ex.map(job, tasks):
            if r:
                PM[k][m] = r
            else:
                fails.append(f"{MODELS[m]['label']} ({k})"); print("!", k, m, err)
    locs = {}; newrows = []
    for k in LOCS:
        if not PM[k]:
            continue
        try:
            o, rows = build_loc(PM[k], CL.get(k, {}), WT, cal, T0, now)
        except Exception as e:
            print("! بناء", k, repr(e)); fails.append(k); continue
        o["ar"] = LOCS[k][2]; locs[k] = o
        newrows += [(now.strftime("%Y-%m-%d"), k) + r for r in rows]
        print("✓", k, "|", ", ".join(f"{m}:{len(v.get('precipitation', []))}" for m, v in PM[k].items()))
    if not locs:
        print("✗ لا بيانات إطلاقاً"); sys.exit(1)
    meta = {"updated": (now + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M"), "t0": T0.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "D": D, "N": N, "band": BAND_AR, "rain_t": RAIN_T, "cal": cal, "fails": sorted(set(fails)),
            "models": {m: {"label": v["label"], "days": v["days"]} for m, v in MODELS.items()}}
    html = json.dumps({"meta": meta, "locs": locs}, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(a.out, "long.json"), "w", encoding="utf-8") as f:
        f.write(html)
    print("✓ الصفحة:", round(len(html) / 1024), "KB")

    # حفظ الحالة (سجل 100 يوم فقط)
    today = now.strftime("%Y-%m-%d"); keep_from = (now - timedelta(days=100)).strftime("%Y-%m-%d")
    old = [[r[h] for h in LOGH] for r in log if r["run"] != today and r["run"] >= keep_from]
    write_log(lp, old + [list(r) for r in newrows])
    with open(cp, "w", encoding="utf-8") as f:
        json.dump(CL, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(a.state, "calib.json"), "w", encoding="utf-8") as f:
        json.dump(cal, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
