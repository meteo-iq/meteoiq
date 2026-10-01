#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""التوقع المبكر
python early_forecast.py                       # جلب + حفظ CSV + بناء التقرير
python early_forecast.py --offline             # بناء التقرير من CSV فقط
python early_forecast.py --add "Al-Sulaiman=31.2,46.3"   # إضافة موقع
"""
import argparse, csv, json, math, os, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

UA = "EarlyForecast/1.0 (personal use)"
TZ = "Asia/Baghdad"
LOCS = {  # الاسم في CSV: (عرض، طول، الاسم العربي)
    "Al-Shatra": (31.4142, 46.1653, "الشطرة"),
    "Nasiriyah (Dhi Qar centre)": (31.05, 46.2667, "الناصرية"),
    "Suq al-Shuyukh (Dhi Qar south)": (30.8878, 46.6306, "سوق الشيوخ"),
    "Qalat Sukkar (Dhi Qar north)": (31.8503, 46.0761, "قلعة سكر"),
    "Al-Sulaiman": (31.3927, 46.3346, "آل سليمان"),
}
# أوزان افتراضية (أولية عامة، غير معايرة على العراق). الأعلى = اعتماد أكبر.
W = {"ecmwf_ifs025": 1.0, "ecmwf_aifs025": .9, "icon_global": .85,
     "ukmo_global_deterministic_10km": .8, "gfs_global": .75,
     "meteofrance_arpege_world": .7, "gem_global": .6, "jma_gsm": .55,
     "bom_access_global": .5, "cma_grapes_global": .45}
V = {"temperature_2m": "temp_c", "dew_point_2m": "dewpoint_c",
     "relative_humidity_2m": "rh_pct", "precipitation": "precip_mm", "cape": "cape_jkg",
     "wind_speed_10m": "wind_kmh", "wind_gusts_10m": "gust_kmh",
     "wind_direction_10m": "wind_dir", "visibility": "visibility_m",
     "cloud_cover": "cloud_pct", "relative_humidity_700hPa": "rh700_pct",
     "wind_speed_700hPa": "wind700_kmh", "lifted_index": "li",
     "convective_inhibition": "cin", "wind_speed_500hPa": "wind500_kmh"}
BASE = list(V)[:12]
COLS = ["fetched_at_utc", "location", "lat", "lon", "source", "model", "time_local"] + list(V.values())


def get(url, tries=3, to=30):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=to) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                raise
        except Exception:
            pass
        time.sleep(2 * (i + 1))
    raise RuntimeError("network")


def om(lat, lon, model, vs):
    q = urllib.parse.urlencode({"latitude": lat, "longitude": lon, "hourly": ",".join(vs), "models": model,
                                "timezone": TZ, "forecast_days": 7, "wind_speed_unit": "kmh"})
    return get("https://api.open-meteo.com/v1/forecast?" + q)


def job(a):
    name, lat, lon, model, ts = a
    try:
        try:
            d = om(lat, lon, model, list(V))
        except urllib.error.HTTPError:
            d = om(lat, lon, model, BASE)
    except Exception as e:
        return name, model, [], str(e)
    h, rows = d.get("hourly", {}), []
    for i, t in enumerate(h.get("time", [])):
        r = {"fetched_at_utc": ts, "location": name, "lat": lat, "lon": lon,
             "source": "open-meteo", "model": model, "time_local": t}
        for v, c in V.items():
            s = h.get(v) or h.get(v + "_" + model)
            x = s[i] if s and i < len(s) else None
            r[c] = "" if x is None else x
        rows.append(r)
    return name, model, rows, ""


def ens(lat, lon):  # احتمال المطر (>=0.2 مم/س) من أعضاء ECMWF ENS
    try:
        q = urllib.parse.urlencode({"latitude": lat, "longitude": lon, "hourly": "precipitation",
                                    "models": "ecmwf_ifs025", "timezone": TZ, "forecast_days": 7})
        h = get("https://ensemble-api.open-meteo.com/v1/ensemble?" + q).get("hourly", {})
        m = [v for k, v in h.items() if k.startswith("precipitation")]
        return {t: round(100 * sum(1 for s in m if s[i] is not None and s[i] >= .2) / len(m))
                for i, t in enumerate(h["time"])}
    except Exception:
        return {}


def load(path):
    if not os.path.exists(path) or not os.path.getsize(path):
        return [], None
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        return list(rd), rd.fieldnames


CAL = {"Baghdad", "Basra", "Mosul", "Erbil", "Nasiriyah", "Najaf"}  # سجل 8 أيام للمعايرة؛ بقية المدن آخر تحديث فقط


def save(path, new):
    old, _ = load(path)
    cut = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    keep = [r for r in old if r["fetched_at_utc"] >= cut and r["location"] in CAL and r["fetched_at_utc"] != new[0]["fetched_at_utc"]]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, COLS, restval="")
        w.writeheader(); w.writerows(keep + new)


def archive(lat, lon, d0, d1):
    try:
        q = urllib.parse.urlencode({"latitude": lat, "longitude": lon, "start_date": d0, "end_date": d1,
                                    "hourly": "precipitation", "timezone": TZ})
        h = get("https://archive-api.open-meteo.com/v1/archive?" + q).get("hourly", {})
        o = {}
        for t, x in zip(h.get("time", []), h.get("precipitation", [])):
            o[t[:10]] = o.get(t[:10], 0) + (x or 0)
        return o
    except Exception:
        return {}


def calibrate(rows, locs):
    """معايرة ذاتية: يضرب الوزن الأولي بعامل مهارة (0.5–1.5) من خطأ المطر اليومي مقابل ERA5،
    ويزداد أثر العامل مع عدد العينات (n/(n+30)). قبل توفر عينات تبقى الأوزان الأولية."""
    lim = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d")
    sm = {}
    for r in rows:
        d, p = r["time_local"][:10], fl(r["precip_mm"])
        if d <= lim and r["fetched_at_utc"][:10] < d and p is not None:  # توقع صدر قبل اليوم المستهدف
            k = (r["location"], r["model"], r["fetched_at_utc"], d)
            sm[k] = sm.get(k, 0) + p
    obs, err = {}, {}
    for n in {k[0] for k in sm if k[0] in locs}:
        ds = sorted(k[3] for k in sm if k[0] == n)
        obs[n] = archive(locs[n][0], locs[n][1], ds[0], ds[-1])
    for (n, m, _, d), v in sm.items():
        if d in obs.get(n, {}):
            err.setdefault(m, []).append(abs(v - obs[n][d]))
    if not err:
        return dict(W), {}
    mae = {m: max(sum(v) / len(v), .05) for m, v in err.items()}
    ref = sorted(mae.values())[len(mae) // 2]
    out, cnt = {}, {}
    for m, p in W.items():
        n = len(err.get(m, []))
        f = min(max(ref / mae[m], .5), 1.5) if n else 1
        out[m], cnt[m] = round(p * (1 + n / (n + 30) * (f - 1)), 3), n
    return out, cnt


def fl(x):
    try:
        return float(x)
    except Exception:
        return None


def idx(r):  # مؤشر عواصف تقديري 0-100 لكل نموذج/ساعة
    c, d, p, g, li = fl(r["cape_jkg"]), fl(r["dewpoint_c"]), fl(r["precip_mm"]), fl(r["gust_kmh"]), fl(r.get("li", ""))
    if c is None:
        return None
    s = 40 * min(c / 2500, 1) + 20 * min(max(((d or 0) - 6) / 12, 0), 1)
    s += 10 * min(max(-li / 6, 0), 1) if li is not None else 0
    return s + 20 * min((p or 0) / 2, 1) + 10 * min(max(((g or 0) - 30) / 30, 0), 1)


def agg(vals):  # متوسط موزون، أدنى، أعلى
    v = [(W.get(m, .4), x) for m, x in vals if x is not None]
    if not v:
        return None
    tw = sum(w for w, _ in v)
    return sum(w * x for w, x in v) / tw, min(x for _, x in v), max(x for _, x in v)


def hourly(rows):
    by = {}
    for r in rows:
        by.setdefault(r["time_local"], {})[r["model"]] = r
    return [(t, [(m, idx(r)) for m, r in by[t].items()], [(m, fl(r["precip_mm"])) for m, r in by[t].items()],
             [(m, fl(r["gust_kmh"])) for m, r in by[t].items()]) for t in sorted(by)]


def peaks(rows):
    out = {}
    for t, im, _, _ in hourly(rows):
        a = agg(im)
        if a:
            out[t[:10]] = max(out.get(t[:10], 0), a[0])
    return out


def direction(v):
    v = [x for x in v if x is not None]
    if len(v) < 2:
        return "new"
    prev = v[-4:-1]
    d = v[-1] - sum(prev) / len(prev)
    return "up" if d >= 8 else "down" if d <= -8 else "same"


def level(p):
    return "مرتفع" if p >= 60 else "متوسط" if p >= 35 else "ضعيف" if p >= 20 else "هادئ"


def build(runs, ens_map):
    keys = sorted(runs)[-12:]
    H = hourly(runs[keys[-1]])
    t = [h[0] for h in H]
    A = [agg(h[1]) for h in H]
    rn = [agg(h[2]) for h in H]
    gs = [agg(h[3]) for h in H]
    mean = [round(a[0], 1) if a else None for a in A]
    rain = [round(r[0], 2) if r else 0 for r in rn]
    gust = [round(g[0]) if g else 0 for g in gs]
    models = {}
    for i, h in enumerate(H):
        for m, x in h[1]:
            models.setdefault(m, [None] * len(H))[i] = None if x is None else round(x, 1)
    P = [peaks(runs[k]) for k in keys]
    dates = sorted({x[:10] for x in t})
    trend = {d: [round(p[d], 1) if d in p else None for p in P] for d in dates}
    days = []
    for d in dates:
        ix = [i for i, x in enumerate(t) if x[:10] == d and mean[i] is not None]
        if not ix:
            continue
        p = max(ix, key=lambda i: mean[i])
        tot = {}
        for i in ix:
            for m, x in H[i][2]:
                if x is not None:
                    a = tot.setdefault(m, [0, 0]); a[0] += x; a[1] += 1
        full = [v[0] for v in tot.values() if v[1] >= 20] or [0]
        ok = [(W.get(m, .4), x) for m, x in H[p][1] if x is not None]
        agree = round(100 * sum(w for w, x in ok if x >= 35) / sum(w for w, _ in ok))
        days.append({"date": d, "peak": round(mean[p]), "peak_t": t[p][11:16], "rain": round(sum(rain[i] for i in ix), 1),
                     "rain_lo": round(min(full), 1), "rain_hi": round(max(full), 1), "gust": max(gust[i] for i in ix),
                     "agree": agree, "n": len(ok), "level": level(mean[p]), "trend": direction(trend[d])})
    wins, cur = [], None
    for i, m in enumerate(mean):
        if m is not None and m >= 35:
            if cur and i - cur[1] <= 2:
                cur[1] = i; cur[2] = max(cur[2], m)
            else:
                cur = [i, i, m]; wins.append(cur)
    e = ens_map or {}
    return {"t": t, "mean": mean, "min": [round(a[1], 1) if a else None for a in A],
            "max": [round(a[2], 1) if a else None for a in A], "rain": rain, "gust": gust,
            "ens": [e.get(x) for x in t], "models": models, "days": days,
            "wins": [[t[a], t[b], round(c)] for a, b, c in wins],
            "runs": [(datetime.strptime(k, "%Y-%m-%dT%H:%M:%SZ") + timedelta(hours=3)).strftime("%d/%m %H:%M") for k in keys],
            "trend": trend}


ICAO = ["ORTL", "ORMM", "ORNI", "ORBI", "OKBK"]  # مطارات قريبة: طليل، البصرة، النجف، بغداد، الكويت
MCOLS = ["station", "lat", "lon", "time_local", "precip_mm", "temp_c", "gust_kmh", "wx"]


def km(a, b, c, d):
    p = math.pi / 180
    x = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(x))


def metar():  # رصد المطارات الرسمية (METAR) لآخر 72 ساعة، مجاني بلا مفتاح
    try:
        d = get("https://aviationweather.gov/api/data/metar?" + urllib.parse.urlencode(
            {"bbox": "28.5,38.5,37.6,49.2", "format": "json", "hours": 72}))
    except Exception as e:
        print("METAR FAIL", e)
        return []
    out = []
    for m in d or []:
        try:
            t = datetime.fromtimestamp(m["obsTime"], timezone.utc) + timedelta(hours=3)
            v = fl(str(m.get("visib", "")).replace("+", ""))
            kt = lambda x: None if x is None else round(x * 1.852)
            out.append({"st": m["icaoId"], "name": m.get("name") or m["icaoId"], "lat": m["lat"], "lon": m["lon"],
                        "t": t.strftime("%Y-%m-%dT%H:%M"), "temp": m.get("temp"), "dew": m.get("dewp"),
                        "wind": kt(m.get("wspd")), "gust": kt(m.get("wgst")),
                        "vis": None if v is None else round(v * 1.609, 1), "wx": m.get("wxString") or "", "pr": None})
        except Exception:
            pass
    print("METAR:", len(out), "تقرير")
    return out


def manual(path):  # محطتك/محطات محلية: سجّل صفوفاً في my_station.csv
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerow(MCOLS)
        return []
    out = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            la, lo = fl(r.get("lat")), fl(r.get("lon"))
            if la is None or lo is None or not r.get("time_local"):
                continue
            out.append({"st": r.get("station") or "محطتي", "name": r.get("station") or "محطتي", "lat": la, "lon": lo,
                        "t": r["time_local"][:16].replace(" ", "T"), "temp": fl(r.get("temp_c")), "dew": None,
                        "wind": None, "gust": fl(r.get("gust_kmh")), "vis": None, "wx": r.get("wx") or "",
                        "pr": fl(r.get("precip_mm"))})
    return out


def obs_for(lat, lon, obs, t):
    by = {}
    for o in obs:
        by.setdefault(o["st"], []).append(o)
    d = lambda v: km(lat, lon, v[0]["lat"], v[0]["lon"])
    sts = []
    for v in sorted(by.values(), key=d)[:3]:
        v.sort(key=lambda o: o["t"])
        sts.append({"name": v[0]["name"], "km": round(d(v)),
                    "last": {k: v[-1][k] for k in ("t", "temp", "dew", "wind", "gust", "vis", "wx", "pr")},
                    "ev": [[o["t"], o["wx"]] for o in v if o["wx"]][-6:]})
    marks = {}
    for v in by.values():
        if d(v) <= 200:
            for o in v:
                w, h = o["wx"].upper(), o["t"][:13] + ":00"
                val = 100 if "TS" in w else 92 if any(x in w for x in ("RA", "SH", "DZ")) or (o["pr"] or 0) >= .2 else None
                if val and val > marks.get(h, 0):
                    marks[h] = val
    return {"st": sts, "marks": [marks.get(x) for x in t]}


CDN = "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"


def chartjs():  # يُخزَّن محلياً مرة واحدة ويُدمج في الصفحة لتعمل الرسوم بلا إنترنت لاحقاً
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chart.umd.min.js")
    try:
        if not os.path.exists(p):
            with urllib.request.urlopen(urllib.request.Request(CDN, headers={"User-Agent": UA}), timeout=30) as r:
                open(p, "wb").write(r.read())
        return "<script>" + open(p, encoding="utf-8").read() + "</script>"
    except Exception:
        return '<script src="%s"></script>' % CDN


def extra(rows):  # حرارة ورياح ورطوبة وغيوم ورؤية: متوسط موزون + نطاق النماذج
    by = {}
    for r in rows:
        by.setdefault(r["time_local"], {})[r["model"]] = r
    T = sorted(by)
    o = {k: [] for k in ("temp", "tlo", "thi", "wind", "rh", "cloud", "vis")}
    mt = {}
    for i, t in enumerate(T):
        for k, c in (("temp", "temp_c"), ("wind", "wind_kmh"), ("rh", "rh_pct"), ("cloud", "cloud_pct"), ("vis", "visibility_m")):
            a = agg([(m, fl(r[c])) for m, r in by[t].items()])
            o[k].append(None if not a else round(a[0], 1))
            if k == "temp":
                o["tlo"].append(None if not a else round(a[1], 1)); o["thi"].append(None if not a else round(a[2], 1))
        for m, r in by[t].items():
            mt.setdefault(m, [None] * len(T))[i] = fl(r["temp_c"])
    o["tm"] = mt
    return o


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="weather_log.csv")
    ap.add_argument("--out", default="early_forecast.html")
    ap.add_argument("--add", action="append", default=[])
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--manual", default="my_station.csv")
    a = ap.parse_args()
    locs = {c["id"]: (c["lat"], c["lon"], c["ar"]) for c in json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cities.json"), encoding="utf-8"))}
    for s in a.add:
        n, c = s.split("=")
        la, lo = c.split(",")
        locs[n] = (float(la), float(lo), n)
    ens_maps = {}
    if not a.offline:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        jobs = [(n, v[0], v[1], m, ts) for n, v in locs.items() for m in W]
        new = []
        with ThreadPoolExecutor(6) as ex:
            for n, m, rows, err in ex.map(job, jobs):
                print("OK  " if rows else "FAIL", n, m, len(rows) or err, flush=True)
                new += rows
            ens_maps = dict(zip(locs, ex.map(lambda n: ens(*locs[n][:2]), locs)))
        if new:
            save(a.csv, new)
    rows, _ = load(a.csv)
    if not rows:
        return print("لا توجد بيانات.")
    wt, cnt = calibrate(rows, locs) if not a.offline else (dict(W), {})
    W.update(wt)
    G = {}
    for r in rows:
        G.setdefault(r["location"], {}).setdefault(r["fetched_at_utc"], []).append(r)
    obs = ([] if a.offline else metar()) + manual(a.manual)
    out = {}
    for n, runs in G.items():
        lab = n
        out[lab] = res = build(runs, ens_maps.get(n))
        res.update(extra(runs[sorted(runs)[-1]]))
        r0 = next(iter(runs.values()))[0]
        res["obs"] = obs_for(float(r0["lat"]), float(r0["lon"]), obs, res["t"])
    upd = max(r["fetched_at_utc"] for r in rows)
    upd = (datetime.strptime(upd, "%Y-%m-%dT%H:%M:%SZ") + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
    data = json.dumps({"locs": out, "updated": upd, "w": {m: [W[m], cnt.get(m, 0)] for m in W}}, ensure_ascii=False).replace("</", "<\\/")
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(data)
    print("تم إنشاء:", os.path.abspath(a.out))


HTML = r"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>التوقع المبكر</title>
<link href="https://fonts.googleapis.com/css2?family=Aref+Ruqaa:wght@700&family=IBM+Plex+Sans+Arabic:wght@400;600&display=swap" rel="stylesheet">
__CHART__
<style>
:root{--ink:#0c1524;--card:#15253f;--line:#2b3f63;--sand:#f0e4c8;--mu:#a4b0c4;--saf:#e9b44c;--ter:#e2703a;--tur:#3fb8a6;--cri:#c8324f}
*{box-sizing:border-box}
body{margin:0;padding:16px;color:var(--sand);font:400 15px/1.7 "IBM Plex Sans Arabic",Tahoma,sans-serif;background:radial-gradient(900px 420px at 85% -5%,#2f4a7a66,transparent),linear-gradient(45deg,#ffffff07 25%,transparent 25%) 0 0/38px 38px,linear-gradient(-45deg,#ffffff07 25%,transparent 25%) 0 0/38px 38px,var(--ink)}
main{max-width:1180px;margin:auto}
header{display:flex;align-items:center;gap:14px;margin:4px 0 6px}
header svg{width:54px;height:54px;flex:none}h1{font:700 44px/1.1 "Aref Ruqaa",serif;margin:0;color:var(--saf);text-shadow:0 2px 18px #e9b44c44}
#sub{color:var(--mu);margin:0 0 14px;font-size:13px}
h2{font:700 21px "Aref Ruqaa",serif;color:var(--saf);margin:0 0 10px}
nav{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
nav button{background:transparent;color:var(--sand);border:1px solid var(--line);border-radius:4px 16px 4px 16px;padding:7px 16px;font:inherit;cursor:pointer}
nav button.on{background:var(--saf);color:#1b1204;border-color:var(--saf);font-weight:600}
.days{display:flex;gap:12px;overflow-x:auto;padding:4px 2px 14px}
.day{min-width:168px;background:linear-gradient(180deg,color-mix(in srgb,var(--c) 22%,var(--card)),var(--card) 60%);border:1px solid color-mix(in srgb,var(--c) 55%,transparent);border-radius:84px 84px 16px 16px;padding:20px 12px 14px;text-align:center}
.ring{width:78px;height:78px;border-radius:50%;margin:0 auto 8px;display:grid;place-items:center;background:conic-gradient(var(--c) calc(var(--p)*1%),#ffffff1c 0)}
.ring b{width:62px;height:62px;border-radius:50%;background:var(--card);display:grid;place-items:center;font:700 24px "Aref Ruqaa"}
.day h3{margin:0;font-size:15px}.lv{display:inline-block;background:var(--c);color:#170a06;border-radius:10px;padding:0 10px;font-size:12px;font-weight:600;margin:3px 0}
.day p{margin:2px 0;font-size:12.5px;color:var(--mu)}.day p.tr{color:var(--sand);font-weight:600}
.card{background:color-mix(in srgb,var(--card) 92%,transparent);border:1px solid var(--line);border-radius:6px 26px 6px 26px;padding:16px;margin-bottom:14px}
.two{display:grid;grid-template-columns:1.3fr 1fr;gap:14px}@media(max-width:820px){.two{grid-template-columns:1fr}}
.sc{overflow-x:auto}.box{position:relative;height:300px}.sc .box{min-width:860px}
.hm{min-width:900px}.hr{display:grid;grid-template-columns:96px 1fr;align-items:center;margin:2px 0;font-size:11px;color:var(--mu)}
.g{display:grid;grid-template-columns:repeat(168,1fr);height:15px;gap:0}.g i{display:block}.hr.m .g{height:24px}.hr.m{color:var(--saf);font-weight:600}
.hd{display:grid;text-align:center;font-size:11px;color:var(--mu);border-bottom:1px solid var(--line);margin-bottom:4px}
.wt{width:100%;border-collapse:collapse;font-size:13px}.wt td{padding:3px 4px}.bar{height:8px;border-radius:4px;background:linear-gradient(90deg,var(--tur),var(--saf))}
.st{border-bottom:1px solid var(--line);padding:6px 0}.st p{margin:2px 0;font-size:12.5px;color:var(--mu)}p.wx{color:var(--saf);font-weight:600}
ul{margin:0;padding:0 20px;line-height:2}footer{color:var(--mu);font-size:12px;line-height:1.8;padding:6px 2px 20px}
</style></head><body><main>
<header><svg viewBox="0 0 100 100"><g fill="none" stroke="#e9b44c" stroke-width="3"><rect x="20" y="20" width="60" height="60"/><rect x="20" y="20" width="60" height="60" transform="rotate(45 50 50)"/><circle cx="50" cy="50" r="12" fill="#e2703a44"/></g></svg><div><h1>التوقع المبكر</h1></div></header>
<p id="sub"></p><nav id="chips"></nav><div class="days" id="days"></div>
<section class="card"><h2>المخطط الموحّد للأيام السبعة</h2><div class="sc"><div class="box"><canvas id="c1"></canvas></div></div></section>
<section class="card"><h2>خريطة الساعات — كل نموذج على حدة</h2><div class="sc"><div class="hm" id="hm"></div></div></section>
<div class="two"><section class="card"><h2>الحصيلة اليومية</h2><div class="box"><canvas id="c3"></canvas></div></section>
<section class="card"><h2>تطور التوقع عبر التحديثات</h2><div class="box"><canvas id="c4"></canvas></div><p id="tn" style="color:var(--mu);font-size:12.5px;margin:6px 0 0"></p></section></div>
<section class="card"><h2>الرصد الفعلي من المحطات القريبة</h2><div id="obs"></div></section>
<div class="two"><section class="card"><h2>نوافذ الخلايا المحتملة</h2><ul id="wins"></ul></section>
<section class="card"><h2>أوزان النماذج</h2><table class="wt" id="wt"></table></section></div>
<footer>المؤشر تقديري (0–100) يجمع CAPE والرطوبة ومؤشر الاستقرار والأمطار والهبّات وليس تحذيراً رسمياً. تبدأ الأوزان بقيم أولية عامة، ثم تتعدّل تلقائياً كلما تراكمت أيام مُتحقَّق منها مقابل الأمطار الفعلية المقدّرة من ERA5 (تحليل مناخي وليس محطات قياس، ويتأخر نحو 5 أيام). CAPE يُحسب بطرق مختلفة بين النماذج فقارن اتجاه كل نموذج أكثر من أرقامه. النماذج العالمية لا تحدد موضع الخلية؛ قبل الحدث بساعات اعتمد على القمر الصناعي والبرق.</footer>
<script>
const D=__DATA__,$=s=>document.querySelector(s),LC='ar-IQ-u-nu-latn',fmt=(t,o)=>new Date(t).toLocaleString(LC,o),dd=(d,o)=>fmt(d+'T12:00',o);
const LV={'مرتفع':'#c8324f','متوسط':'#e2703a','ضعيف':'#e9b44c','هادئ':'#3fb8a6'};
const TR={up:'↑ يتصاعد',down:'↓ يتراجع',same:'→ مستقر',new:'• أول رصد'};
const PAL=['#e9b44c','#3fb8a6','#e2703a','#c8324f','#8fa8ff','#f0e4c8','#b98cff','#7fd1ff','#a3e635','#94a3b8'];
const ST=[[0,[21,37,63]],[20,[63,184,166]],[40,[233,180,76]],[65,[226,112,58]],[100,[200,50,79]]];
const col=v=>{if(v==null)return'#ffffff0a';for(let i=1;i<5;i++)if(v<=ST[i][0]){const[a,A]=ST[i-1],[b,B]=ST[i],f=(v-a)/(b-a);return`rgb(${A.map((x,k)=>Math.round(x+(B[k]-x)*f))})`}return'rgb(200,50,79)'};
Chart.defaults.color='#a4b0c4';Chart.defaults.font.family='"IBM Plex Sans Arabic",sans-serif';Chart.defaults.borderColor='#2b3f6380';
let cur=Object.keys(D.locs)[0],ch={};
const mk=(id,c)=>{ch[id]&&ch[id].destroy();c.options=Object.assign({responsive:true,maintainAspectRatio:false},c.options);ch[id]=new Chart($('#'+id),c)};
const dayTicks={autoSkip:false,maxRotation:0,callback:function(v){const t=this.getLabelForValue(v);return t.endsWith('T00:00')?fmt(t,{weekday:'short',day:'numeric'}):''}};
$('#sub').textContent='آخر تحديث للبيانات: '+D.updated+' بتوقيت العراق · '+Object.keys(D.w).length+' نماذج';
$('#chips').innerHTML=Object.keys(D.locs).map(k=>`<button data-k="${k}">${k}</button>`).join('');
$('#chips').onclick=e=>{if(e.target.dataset.k){cur=e.target.dataset.k;render()}};
const short=m=>m.replace(/_(global|ifs025|aifs025|deterministic_10km|world)$/,m=>m.includes('aifs')?'-AI':'');
function render(){
const L=D.locs[cur];document.querySelectorAll('#chips button').forEach(b=>b.classList.toggle('on',b.dataset.k==cur));
$('#days').innerHTML=L.days.map(d=>`<div class="day" style="--c:${LV[d.level]};--p:${d.peak}"><div class="ring"><b>${d.peak}</b></div><h3>${dd(d.date,{weekday:'long',day:'numeric',month:'short'})}</h3><span class="lv">${d.level}</span><p>الذروة عند ${d.peak_t}</p><p>مطر ${d.rain} مم (${d.rain_lo}–${d.rain_hi})</p><p>هبّات حتى ${d.gust} كم/س</p><p>اتفاق ${d.agree}٪ من ${d.n} نماذج</p><p class="tr">${TR[d.trend]}</p></div>`).join('');
mk('c1',{data:{labels:L.t,datasets:[
{type:'bar',label:'مطر (مم/س)',data:L.rain,yAxisID:'y2',backgroundColor:'#3fb8a6aa',order:3},
{type:'line',label:'min',data:L.min,borderWidth:0,pointRadius:0,order:2},
{type:'line',label:'نطاق النماذج',data:L.max,borderWidth:0,pointRadius:0,fill:'-1',backgroundColor:'#e2703a33',order:2},
{type:'line',label:'مؤشر العواصف (موزون)',data:L.mean,borderColor:'#e9b44c',borderWidth:2.6,pointRadius:0,tension:.3,order:1},
{type:'line',label:'احتمال المطر ٪ (ECMWF ENS)',data:L.ens,borderColor:'#f0e4c8',borderDash:[4,4],borderWidth:1.4,pointRadius:0,order:1},
{type:'line',label:'رصد فعلي (نجمة حمراء رعد، خضراء مطر)',data:L.obs.marks,showLine:false,pointStyle:'star',pointRadius:8,borderColor:'#c8324f',pointBackgroundColor:c=>c.raw==100?'#c8324f':'#3fb8a6',pointBorderColor:c=>c.raw==100?'#c8324f':'#3fb8a6',order:0}]},
options:{interaction:{mode:'index',intersect:false},plugins:{legend:{labels:{filter:i=>i.text!='min'}},tooltip:{callbacks:{title:i=>fmt(L.t[i[0].dataIndex],{weekday:'long',hour:'2-digit',minute:'2-digit'})}}},
scales:{x:{ticks:dayTicks},y:{min:0,max:100,title:{display:true,text:'مؤشر / احتمال'}},y2:{position:'left',grid:{display:false},min:0,title:{display:true,text:'مم/س'}}}}});
const nd=L.days.length,rows=[['الموزون',L.mean]].concat(Object.entries(L.models).sort((a,b)=>(D.w[b[0]]||[0])[0]-(D.w[a[0]]||[0])[0]).map(([m,v])=>[short(m),v]));
$('#hm').innerHTML=`<div class="hr"><span></span><div class="hd" style="grid-template-columns:repeat(${nd},1fr)">${L.days.map(d=>`<span>${dd(d.date,{weekday:'short',day:'numeric'})}</span>`).join('')}</div></div>`+rows.map(([n,v],i)=>`<div class="hr${i?'':' m'}"><span>${n}</span><div class="g">${v.map((x,j)=>`<i style="background:${col(x)}" title="${fmt(L.t[j],{weekday:'short',hour:'2-digit'})} · ${x==null?'—':x}"></i>`).join('')}</div></div>`).join('');
mk('c3',{data:{labels:L.days.map(d=>dd(d.date,{weekday:'short',day:'numeric'})),datasets:[
{type:'bar',label:'مطر (مم)',data:L.days.map(d=>d.rain),backgroundColor:'#3fb8a6cc',yAxisID:'y2',borderRadius:6},
{type:'line',label:'ذروة المؤشر',data:L.days.map(d=>d.peak),borderColor:'#e9b44c',backgroundColor:'#e9b44c',tension:.3},
{type:'line',label:'اتفاق النماذج ٪',data:L.days.map(d=>d.agree),borderColor:'#f0e4c8',backgroundColor:'#f0e4c8',borderDash:[5,4],tension:.3}]},
options:{scales:{y:{min:0,max:100},y2:{position:'left',grid:{display:false},min:0}}}});
mk('c4',{type:'line',data:{labels:L.runs,datasets:Object.entries(L.trend).map(([d,v],i)=>({label:dd(d,{weekday:'short',day:'numeric'}),data:v,borderColor:PAL[i%10],backgroundColor:PAL[i%10],borderWidth:2,tension:.25,spanGaps:true}))},
options:{scales:{y:{min:0,max:100}}}});
$('#tn').textContent=L.runs.length<2?'يلزم تحديثان على الأقل لإظهار الاتجاه؛ شغّل السكربت بعد ساعات.':'كل خط يوم مستهدف، وكل نقطة ذروة مؤشره في تحديث. صعود الخط = زيادة توقع العواصف.';
$('#wins').innerHTML=L.wins.length?L.wins.map(w=>`<li>${fmt(w[0],{weekday:'long',hour:'2-digit',minute:'2-digit'})} ← ${fmt(w[1],{hour:'2-digit',minute:'2-digit'})} · ذروة ${w[2]}</li>`).join(''):'<li>لا نوافذ تتجاوز عتبة الخطر المتوسط (35) حالياً.</li>';
$('#obs').innerHTML=L.obs.st.length?L.obs.st.map(s=>{const l=s.last,f=x=>x==null?'—':x;return`<div class="st"><b>${s.name}</b> <small style="color:var(--mu)">على بعد ${s.km} كم</small><p>${fmt(l.t,{weekday:'short',hour:'2-digit',minute:'2-digit'})} · حرارة ${f(l.temp)}° · ندى ${f(l.dew)}° · رياح ${f(l.wind)} كم/س${l.gust?' (هبّات '+l.gust+')':''} · رؤية ${f(l.vis)} كم${l.pr!=null?' · مطر '+l.pr+' مم':''}</p>${l.wx?`<p class="wx">${l.wx}</p>`:''}${s.ev.length?`<p>آخر الظواهر: ${s.ev.map(e=>fmt(e[0],{weekday:'short',hour:'2-digit'})+' '+e[1]).join(' · ')}</p>`:''}</div>`}).join(''):'<p style="color:var(--mu)">لا بيانات رصد بعد. شغّل السكربت مع إنترنت، أو سجّل قراءات محطتك في my_station.csv.</p>';
$('#wt').innerHTML=Object.entries(D.w).sort((a,b)=>b[1][0]-a[1][0]).map(([m,[w,n]])=>`<tr><td>${short(m)||m}</td><td style="width:38%"><div class="bar" style="width:${Math.min(w,1.5)/1.5*100}%"></div></td><td>${w}</td><td style="color:var(--mu)">${n?n+' عينة':'أولي'}</td></tr>`).join('');
}
render();
</script></main></body></html>"""

if __name__ == "__main__":
    main()
