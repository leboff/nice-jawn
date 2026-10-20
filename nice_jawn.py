"""Nice Jawn: posts South Philly's Nice score (Better Weather) to Bluesky.

  07:00  today's outlook        (sensor.better_weather_nice_today, all forecast hours at that time)
  20:30  tomorrow's forecast    (sensor.better_weather_nice_tomorrow)
         + how today's forecast did (sensor.better_weather_forecast_skill last_10, graded at 20:00)

Text is a fixed template over real sensor numbers, no generated prose. Every post carries a card image
(factor bars + hourly strip) with alt text.

Usage:
  python nice_jawn.py morning|evening [--dry-run] [--out DIR]   one post
  python nice_jawn.py serve                                       scheduler loop (container entrypoint)

Env: HA_URL, HA_TOKEN (read-only use), BSKY_HANDLE, BSKY_APP_PASSWORD, BSKY_PDS (default https://bsky.social),
     NJ_STATE (state dir, default /data), NJ_PLACE (default "South Philly"), NJ_TZ (default America/New_York),
     NJ_MORNING (default 07:00), NJ_EVENING (default 20:30), NJ_DRY_RUN=1 to never post.
"""

import io
import json
import logging
import os
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("nice_jawn")

TZ = ZoneInfo(os.environ.get("NJ_TZ", "America/New_York"))
PLACE = os.environ.get("NJ_PLACE", "South Philly")
STATE_DIR = Path(os.environ.get("NJ_STATE", "/data"))
NOW_ID = "sensor.better_weather_nice_right_now"
TODAY_ID = "sensor.better_weather_nice_today"
TMRW_ID = "sensor.better_weather_nice_tomorrow"
SKILL_ID = "sensor.better_weather_forecast_skill"
DAILY_ID = "sensor.better_weather_daily_forecast"
MAX_GRAPHEMES = 300


# ---------------------------------------------------------------- data
def ha_state(entity_id):
    r = requests.get(
        f"{os.environ['HA_URL'].rstrip('/')}/api/states/{entity_id}",
        headers={"Authorization": f"Bearer {os.environ['HA_TOKEN']}"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def number(state):
    try:
        return float(state["state"])
    except (TypeError, ValueError, KeyError):
        return None


def word(v):
    return "—" if v is None else "Gorgeous" if v >= 90 else "Nice" if v >= 80 else "Decent" if v >= 65 else "Meh" if v >= 45 else "Rough"


def hour_label(h, *, end=False):
    h = h % 24
    return ("midnight" if end else "12am") if h == 0 else "noon" if h == 12 else f"{h % 12}{'am' if h < 12 else 'pm'}"


def local_hours(hourly):
    return [(datetime.fromisoformat(h["hour"]).astimezone(TZ), round(h["score"]), h["source"]) for h in hourly]


def best_window(hours):
    """Longest run of consecutive hours >= 90 (else >= 80); falls back to the single best hour."""
    for cut in (90, 80):
        best, run = [], []
        for t, s, _ in hours:
            if s >= cut and (not run or t - run[-1][0] == timedelta(hours=1)):
                run.append((t, s))
            elif s >= cut:
                run = [(t, s)]
            else:
                run = []
            if len(run) > len(best):
                best = list(run)
        if best:
            return best[0][0].hour, best[-1][0].hour + 1, cut
    if not hours:
        return None
    t, s, _ = max(hours, key=lambda x: x[1])
    return t.hour, t.hour + 1, s


def window_text(win):
    if not win:
        return None
    a, b, _ = win
    if b - a == 1:
        return f"around {hour_label(a)}"
    return f"{hour_label(a)}–{hour_label(b, end=True)}"


def day_forecast(day):
    items = (ha_state(DAILY_ID).get("attributes") or {}).get("forecast") or []
    for it in items:
        if datetime.fromisoformat(it["datetime"]).date() == day:
            return it
    return {}


def gather(slot):
    now = datetime.now(TZ)
    sid = TODAY_ID if slot == "morning" else TMRW_ID
    day = now.date() if slot == "morning" else now.date() + timedelta(days=1)
    st = ha_state(sid)
    score = number(st)
    a = st.get("attributes") or {}
    if score is None or not a.get("hourly"):
        raise RuntimeError(f"{sid} unavailable ({st.get('state')})")
    d = {
        "slot": slot, "day": day, "score": round(score), "word": word(score),
        "hours": local_hours(a["hourly"]), "rain_hours": a.get("rain_hours") or 0,
        "breakdown": a.get("breakdown") or [], "why": a.get("why"),
        "high": a.get("high_f"), "low": a.get("low_f"), "daily": day_forecast(day),
    }
    if slot == "evening":
        d["lookback"] = lookback(now.date())
    return d


def lookback(today):
    """Today's forecast (snapshotted at 8pm last night) vs. what the station measured."""
    try:
        attrs = ha_state(SKILL_ID).get("attributes") or {}
    except requests.RequestException:
        attrs = {}
    rec = next((r for r in attrs.get("last_10") or [] if r.get("date") == today.isoformat()), None)
    if rec and rec.get("forecast") is not None and rec.get("observed") is not None:
        return {"forecast": round(rec["forecast"]), "observed": round(rec["observed"]), "graded": True}
    # Not graded yet (recorder hour not published): fall back to our own saved forecast + today's measured score.
    saved = load_state().get("forecasts", {}).get(today.isoformat())
    measured = number(ha_state(TODAY_ID))
    if saved is not None and measured is not None:
        return {"forecast": round(saved), "observed": round(measured), "graded": False}
    return None


# ---------------------------------------------------------------- text
def factor_line(rows, limit=2):
    if not rows:
        return None      # Better Weather < 0.3.1: no day breakdown, say nothing rather than guess
    lost = [r for r in rows if r.get("lost", 0) >= 1]
    if not lost:
        return "Nothing holding it back."
    return "Docked for: " + "; ".join(f"{r['label'].lower()} −{round(r['lost'])} ({short_detail(r)})" for r in lost[:limit])


def short_detail(r):
    d = r.get("detail") or ""
    if r["factor"] == "temp" and ", " in d:
        return d.split(", ", 1)[1]          # "too cool 7 of 12 hours"
    if r["factor"] == "sun":
        return d.replace(" on average", "").replace(" at this sun angle", "")
    return d


def conditions_line(d):
    f = d["daily"]
    hi = f.get("temperature", d.get("high"))
    lo = f.get("templow", d.get("low"))
    bits = []
    if hi is not None and lo is not None:
        bits.append(f"High {round(hi)}°, low {round(lo)}°")
    if d["rain_hours"]:
        bits.append(f"rain {d['rain_hours']} waking hr{'s' if d['rain_hours'] != 1 else ''}")
    elif (f.get("precipitation_probability") or 0) >= 30:
        bits.append(f"{round(f['precipitation_probability'])}% chance of rain")
    else:
        bits.append("dry")
    if f.get("wind_gust_speed") is not None and f["wind_gust_speed"] >= 25:
        bits.append(f"gusts to {round(f['wind_gust_speed'])} mph")
    return ", ".join(bits) + "."


def post_text(d):
    when = "Today" if d["slot"] == "morning" else "Tomorrow"
    dow = d["day"].strftime("%a %b ") + str(d["day"].day)
    lines = [f"{when} in {PLACE} ({dow}): {d['score']}/100, {d['word']}.", conditions_line(d)]
    win = window_text(best_window(d["hours"]))
    if win:
        lines.append(f"Best stretch: {win}.")
    if factor_line(d["breakdown"]):
        lines.append(factor_line(d["breakdown"]))
    lb = d.get("lookback")
    if lb:
        diff = lb["observed"] - lb["forecast"]
        verdict = "called it" if abs(diff) <= 3 else f"{'+' if diff > 0 else '−'}{abs(diff)}"
        lines.append(f"Today: forecast {lb['forecast']}, measured {lb['observed']} ({verdict}).")
    text = "\n".join(lines)
    while len(text) > MAX_GRAPHEMES and d["breakdown"]:
        lines = [ln for ln in lines if not ln.startswith("Docked")] + [factor_line(d["breakdown"], limit=1)]
        text = "\n".join(lines)
        if len(text) > MAX_GRAPHEMES:
            text = "\n".join(ln for ln in lines if not ln.startswith(("Docked", "Best")))
        break
    return text[:MAX_GRAPHEMES]


def alt_text(d):
    hrs = ", ".join(f"{hour_label(t.hour)} {s}" for t, s, _ in d["hours"])
    rows = "; ".join(f"{r['label']} {round(r['points'])} of {round(r['max'])} ({r.get('detail', '')})" for r in d["breakdown"])
    return (f"Nice score card for {d['day'].strftime('%A %B')} {d['day'].day} in {PLACE}: {d['score']} out of 100, {d['word']}. "
            f"Factors: {rows}. Hourly scores: {hrs}.")[:2000]


# ---------------------------------------------------------------- image
W, Hh = 1200, 675
BG, FG, MUTED, TRACK = (250, 250, 247), (24, 24, 27), (113, 113, 122), (228, 228, 231)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def col(v):
    return (21, 128, 61) if v >= 85 else (77, 124, 15) if v >= 65 else (217, 119, 6) if v >= 45 else (220, 38, 38)


def font(size, bold=False):
    return ImageFont.truetype(FONT_B if bold else FONT, size)


def ellipsize(draw, text, f, width):
    if draw.textlength(text, font=f) <= width:
        return text
    while text and draw.textlength(text + "…", font=f) > width:
        text = text[:-1]
    return text + "…"


def card(d):
    im = Image.new("RGB", (W, Hh), BG)
    g = ImageDraw.Draw(im)
    M = 56
    when = "Today" if d["slot"] == "morning" else "Tomorrow"
    g.text((M, 44), "NICE JAWN", font=font(22, True), fill=MUTED)
    g.text((M, 74), f"{when} · {d['day'].strftime('%A, %B')} {d['day'].day} · {PLACE}", font=font(30, True), fill=FG)
    # big score
    sc = str(d["score"])
    g.text((W - M, 40), sc, font=font(110, True), fill=col(d["score"]), anchor="ra")
    g.text((W - M, 168), d["word"], font=font(30, True), fill=FG, anchor="ra")
    # factor bars
    y, bar_x, bar_w = 200, M + 190, 560
    for r in d["breakdown"][:4]:
        pct = 0 if not r["max"] else max(0.0, min(1.0, r["points"] / r["max"]))
        c = col(100 * pct) if pct < 0.97 else (21, 128, 61)
        g.text((M, y), r["label"], font=font(28, True), fill=FG)
        g.rounded_rectangle((bar_x, y + 8, bar_x + bar_w, y + 26), radius=9, fill=TRACK)
        if pct > 0:
            g.rounded_rectangle((bar_x, y + 8, bar_x + max(18, int(bar_w * pct)), y + 26), radius=9, fill=c)
        lost = round(r["lost"])
        if lost >= 1:
            g.text((bar_x + bar_w + 24, y), f"−{lost}", font=font(28, True), fill=c)
        else:
            cx, cy = bar_x + bar_w + 38, y + 17
            g.line([(cx - 11, cy), (cx - 3, cy + 8), (cx + 12, cy - 9)], fill=(21, 128, 61), width=5, joint="curve")
        g.text((M, y + 38), ellipsize(g, r.get("detail") or "", font(20), W - 2 * M), font=font(20), fill=MUTED)
        y += 78
    # hourly strip
    hours = d["hours"]
    top, base = y + 4, Hh - 92
    n = max(1, len(hours))
    gap = 6
    bw = (W - 2 * M - gap * (n - 1)) / n
    for i, (t, s, src) in enumerate(hours):
        x0 = M + i * (bw + gap)
        h = max(6, (base - top) * s / 100)
        c = col(s)
        if src != "station":
            c = tuple(int(v + (255 - v) * 0.35) for v in c)
        g.rounded_rectangle((x0, base - h, x0 + bw, base), radius=5, fill=c)
        if i % 2 == 0 or n <= 8:
            g.text((x0 + bw / 2, base + 8), hour_label(t.hour), font=font(19), fill=MUTED, anchor="ma")
    g.text((M, Hh - 18), "0–100 for temp, humidity, wind & sun, each waking hour · backyard station + GEM/ICON/ECMWF blend",
           font=font(17), fill=MUTED, anchor="lb")
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------- bluesky
def bsky_post(text, png, alt):
    pds = os.environ.get("BSKY_PDS", "https://bsky.social").rstrip("/")
    s = requests.post(f"{pds}/xrpc/com.atproto.server.createSession",
                      json={"identifier": os.environ["BSKY_HANDLE"], "password": os.environ["BSKY_APP_PASSWORD"]}, timeout=30)
    s.raise_for_status()
    sess = s.json()
    auth = {"Authorization": f"Bearer {sess['accessJwt']}"}
    b = requests.post(f"{pds}/xrpc/com.atproto.repo.uploadBlob", data=png, headers=auth | {"Content-Type": "image/png"}, timeout=60)
    b.raise_for_status()
    record = {
        "$type": "app.bsky.feed.post", "text": text, "langs": ["en"],
        "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "embed": {"$type": "app.bsky.embed.images",
                  "images": [{"alt": alt, "image": b.json()["blob"], "aspectRatio": {"width": W, "height": Hh}}]},
    }
    r = requests.post(f"{pds}/xrpc/com.atproto.repo.createRecord",
                      json={"repo": sess["did"], "collection": "app.bsky.feed.post", "record": record}, headers=auth, timeout=30)
    r.raise_for_status()
    uri = r.json()["uri"]
    handle = os.environ["BSKY_HANDLE"]
    return f"https://bsky.app/profile/{handle}/post/{uri.rsplit('/', 1)[-1]}"


# ---------------------------------------------------------------- state + run
def load_state():
    try:
        return json.loads((STATE_DIR / "state.json").read_text())
    except (OSError, ValueError):
        return {}


def save_state(st):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_DIR / "state.json.tmp"
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(STATE_DIR / "state.json")


def run(slot, *, dry_run=False, out=None):
    d = gather(slot)
    text, png, alt = post_text(d), card(d), alt_text(d)
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / f"{slot}.png").write_bytes(png)
        (Path(out) / f"{slot}.txt").write_text(text + "\n\n---alt---\n" + alt)
    log.info("%s post (%d chars):\n%s", slot, len(text), text)
    url = None
    if not dry_run and os.environ.get("NJ_DRY_RUN") != "1":
        url = bsky_post(text, png, alt)
        log.info("posted %s", url)
    if slot == "evening" and not dry_run:
        st = load_state()
        f = st.setdefault("forecasts", {})
        f[d["day"].isoformat()] = d["score"]
        st["forecasts"] = dict(sorted(f.items())[-14:])
        save_state(st)
    return text, url


def slot_times(day):
    def at(env, default):
        hh, mm = map(int, os.environ.get(env, default).split(":"))
        return datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)
    return {"morning": at("NJ_MORNING", "07:00"), "evening": at("NJ_EVENING", "20:30")}


def serve():
    """Post each slot once per day. Catch up a missed slot for up to 45 min (restart/redeploy), never later."""
    log.info("serving: %s", {k: v.strftime("%H:%M") for k, v in slot_times(date.today()).items()})
    while True:
        now = datetime.now(TZ)
        st = load_state()
        posted = set(st.get("posted", []))
        for slot, at in slot_times(now.date()).items():
            key = f"{now.date().isoformat()}:{slot}"
            if key in posted or not at <= now < at + timedelta(minutes=45):
                continue
            for attempt in range(3):
                try:
                    run(slot)
                    st = load_state()
                    st["posted"] = sorted(set(st.get("posted", [])) | {key})[-30:]
                    save_state(st)
                    break
                except Exception:  # noqa: BLE001 - log and retry; never crash the loop
                    log.exception("%s attempt %d failed", slot, attempt + 1)
                    time.sleep(120)
        time.sleep(30)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    args = sys.argv[1:]
    if not args or args[0] == "serve":
        serve()
    else:
        out = args[args.index("--out") + 1] if "--out" in args else None
        run(args[0], dry_run="--dry-run" in args, out=out)
