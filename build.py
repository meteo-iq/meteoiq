#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MeteoIQ — منسّق البناء الموحّد (كل 6 ساعات)
python build.py            # يشغّل التوقع القصير + المحطات، والبعيد إن مرّ عليه > 20 ساعة
python build.py --long yes # فرض تحديث المدى البعيد
"""
import argparse, csv, gzip, json, os, shutil, subprocess, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
P = lambda *a: os.path.join(ROOT, *a)
UA = "MeteoIQ/1.0 (volunteer project)"


def run(*a):
    print("$", " ".join(a), flush=True)
    return subprocess.call([sys.executable, *a], cwd=ROOT)


def jl(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def num(x):
    try:
        return float(x)
    except Exception:
        return None


def stations():
    """كل محطات METAR العراقية (رمز ICAO يبدأ بـ OR) + محطات المتطوعين من my_station.csv."""
    url = "https://aviationweather.gov/api/data/metar?" + urllib.parse.urlencode(
        {"bbox": "28.5,38.5,37.6,49.2", "format": "json", "hours": 24})
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=40) as r:
        raw = json.loads(r.read() or "[]")
    by = {}
    for m in raw:
        if str(m.get("icaoId", "")).startswith("OR"):
            by.setdefault(m["icaoId"], []).append(m)
    out = []
    for k, v in by.items():
        v.sort(key=lambda m: m["obsTime"])
        m = v[-1]
        vis = num(str(m.get("visib", "")).replace("+", ""))
        kn = lambda x: None if x is None else round(x * 1.852)
        t = lambda e: (datetime.fromtimestamp(e, timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
        out.append({"id": k, "name": m.get("name") or k, "lat": m["lat"], "lon": m["lon"], "kind": "metar",
                    "t": t(m["obsTime"]), "temp": m.get("temp"), "dew": m.get("dewp"), "wind": kn(m.get("wspd")),
                    "gust": kn(m.get("wgst")), "vis": None if vis is None else round(vis * 1.609, 1),
                    "wx": m.get("wxString") or "", "raw": m.get("rawOb", ""),
                    "series": [[t(x["obsTime"]), x.get("temp")] for x in v]})
    mp = P("my_station.csv")
    if os.path.exists(mp):
        last = {}
        with open(mp, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                if num(r.get("lat")) is not None and r.get("time_local"):
                    last[r.get("station") or "محطتي"] = r
        for n, r in last.items():
            out.append({"id": "V-" + n, "name": n, "lat": num(r["lat"]), "lon": num(r["lon"]), "kind": "volunteer",
                        "t": r["time_local"][:16].replace(" ", "T"), "temp": num(r.get("temp_c")), "dew": None,
                        "wind": None, "gust": num(r.get("gust_kmh")), "vis": None, "wx": r.get("wx") or "",
                        "raw": "", "series": []})
    out.sort(key=lambda s: (s["kind"], s["name"]))
    return out


def long_age_h(p):
    j = jl(p)
    try:
        d = datetime.strptime(j["meta"]["updated"], "%Y-%m-%d %H:%M") - timedelta(hours=3)
        return (datetime.utcnow() - d).total_seconds() / 3600
    except Exception:
        return 1e9


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--long", default="auto", choices=["auto", "yes", "no"])
    a = ap.parse_args()
    for d in ("site/data", "state"):
        os.makedirs(P(d), exist_ok=True)
    gz, csvp = P("state/weather_log.csv.gz"), P("state/weather_log.csv")
    if os.path.exists(gz):
        with gzip.open(gz, "rb") as i, open(csvp, "wb") as o:
            shutil.copyfileobj(i, o)

    # 1) التوقع القصير (10 نماذج، كل المدن)
    ok = run("early_forecast.py", "--csv", "state/weather_log.csv", "--manual", "my_station.csv",
             "--out", "site/data/short.json") == 0
    if ok and jl(P("site/data/short.json")):
        shutil.copy(P("site/data/short.json"), P("state/short.json"))
    elif os.path.exists(P("state/short.json")):
        print("! فشل القصير، نستخدم النسخة السابقة")
        shutil.copy(P("state/short.json"), P("site/data/short.json"))
    if os.path.exists(csvp):
        with open(csvp, "rb") as i, gzip.open(gz, "wb") as o:
            shutil.copyfileobj(i, o)
        os.remove(csvp)

    # 2) المدى البعيد (45 يوماً) — مرة كل ~20 ساعة
    sl = P("state/long.json")
    if a.long == "yes" or (a.long == "auto" and long_age_h(sl) > 20):
        if run("longrange.py", "--state", "state", "--out", "site/data") == 0 and jl(P("site/data/long.json")):
            shutil.copy(P("site/data/long.json"), sl)
    if os.path.exists(sl) and not os.path.exists(P("site/data/long.json")):
        shutil.copy(sl, P("site/data/long.json"))

    # 3) المحطات
    try:
        st = stations()
        json.dump({"updated": (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M"), "st": st},
                  open(P("site/data/stations.json"), "w", encoding="utf-8"), ensure_ascii=False)
        print("✓ محطات:", len(st))
    except Exception as e:
        print("! المحطات:", e)
        if os.path.exists(P("state/stations.json")):
            shutil.copy(P("state/stations.json"), P("site/data/stations.json"))
    if os.path.exists(P("site/data/stations.json")):
        shutil.copy(P("site/data/stations.json"), P("state/stations.json"))

    # 4) الواجهة
    shutil.copy(P("web/index.html"), P("site/index.html"))
    shutil.copy(P("cities.json"), P("site/data/cities.json"))
    print("✓ اكتمل البناء")


if __name__ == "__main__":
    main()
