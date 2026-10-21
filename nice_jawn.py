"""Nice Jawn: posts South Philly's Nice score (Better Weather) to Bluesky.

  07:00  today's outlook        (sensor.better_weather_nice_today, all forecast hours at that time)
  20:30  recap of today         measured score, all-time rank, forecast vs measured; card = where today falls
                                 in every measured day + the days ranked around it (sensor.better_weather_nice_day_rank)
  20:31  tomorrow's forecast    (sensor.better_weather_nice_tomorrow)

Text is a fixed template over real sensor numbers, no generated prose. Every post carries a card image
(factor bars + hourly strip) with alt text.

Usage:
  python nice_jawn.py morning|recap|evening [--dry-run] [--out DIR]   one post
  python nice_jawn.py now [--out DIR]                             render the on-demand "right now" reply (never posts)
  python nice_jawn.py serve                                       scheduler + mention loop (container entrypoint)

On demand: mention the account ("hey @nice.bof.fish how are we looking") and it replies in-thread with the
live score + a card (factor bars, today's hours: solid = measured, faded = forecast). "tomorrow" in the
mention gets tomorrow's forecast instead. NJ_REPLIES=live|dry|off (default dry).

Env: HA_URL, HA_TOKEN (read-only use), BSKY_HANDLE, BSKY_APP_PASSWORD, BSKY_PDS (default https://bsky.social),
     NJ_STATE (state dir, default /data), NJ_PLACE (default "South Philly"), NJ_TZ (default America/New_York),
     NJ_MORNING (default 07:00), NJ_EVENING (default 20:30), NJ_DRY_RUN=1 to never post.
"""

import io
import json
import logging
import os
import re
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
RANK_ID = "sensor.better_weather_nice_day_rank"
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
    return d


class NotReady(Exception):
    """Today's data isn't final yet; the scheduler tries again shortly."""


RECAP_WAIT_MIN = int(os.environ.get("NJ_RECAP_WAIT_MIN", "20"))


def gather_recap(*, force=False):
    """Today, measured. Waits for Better Weather to rank today (after 8pm, once the recorder has the last hour)."""
    now = datetime.now(TZ)
    today = now.date()
    st = ha_state(RANK_ID)
    a = st.get("attributes") or {}
    latest = a.get("latest") or {}
    ranked = latest.get("date") == today.isoformat()
    if not ranked and not force:
        raise NotReady(f"{RANK_ID} hasn't ranked {today} yet (latest ranked day {latest.get('date')})")
    td = ha_state(TODAY_ID)
    ta = td.get("attributes") or {}
    score = latest["score"] if ranked else number(td)
    if score is None:
        raise RuntimeError("no measured score for today")
    return {
        "slot": "recap", "day": today, "score": round(score), "exact": score, "word": word(score),
        "hours": local_hours(ta.get("hourly") or []), "rain": latest.get("rain") if ranked else bool(ta.get("rain_hours")),
        "rank": day_rank(today) if ranked else None,
        # chart data needs Better Weather >= 0.3.4; without it the recap uses the hourly card
        "neighbors": a.get("neighbors") if ranked else None, "histogram": a.get("histogram") if ranked else None,
        "lookback": lookback(today),
    }


def short_date(iso, today=None):
    d = date.fromisoformat(iso)
    y = "" if today and d.year == today.year else f", {d.year}"
    return f"{d.strftime('%b')} {d.day}{y}"


def recap_text(d):
    dow = d["day"].strftime("%a %b ") + str(d["day"].day)
    lines = [f"Today in {PLACE} ({dow}): {d['score']}/100, {d['word']}" + (" (rain)." if d["rain"] else ".")]
    rk = d.get("rank")
    if rk:
        lines.append(rank_line(rk))
        nb = d.get("neighbors") or []
        me = next((i for i, n in enumerate(nb) if n["date"] == d["day"].isoformat()), None)
        if me is not None:
            above = [n for n in nb[:me] if n["score"] > d["exact"]]
            below = nb[me + 1:]
            if rk["rank"] == 2 and above:
                lines.append(f"Only {short_date(above[-1]['date'], d['day'])} ({above[-1]['score']:g}) was better.")
            elif rk["rank"] > 2 and above and below:
                lines.append(f"Between {short_date(above[-1]['date'], d['day'])} ({above[-1]['score']:g}) "
                             f"and {short_date(below[0]['date'], d['day'])} ({below[0]['score']:g}).")
            elif rk["rank"] == 1 and below:
                lines.append(f"Next best: {short_date(below[0]['date'], d['day'])} ({below[0]['score']:g}).")
    lb = d.get("lookback")
    if lb:
        diff = lb["observed"] - lb["forecast"]
        verdict = "called it" if abs(diff) <= 3 else f"{'+' if diff > 0 else '−'}{abs(diff)}"
        lines.append(f"Last night's forecast: {lb['forecast']} ({verdict}).")
    win = window_text(best_window([h for h in d["hours"] if h[2] == "station"]))
    if win:
        lines.append(f"Best stretch: {win}.")
    for drop in (None, "Best stretch", "Between", "Only", "Next best"):
        if drop:
            lines = [ln for ln in lines if not ln.startswith(drop)]
        text = "\n".join(lines)
        if len(text) <= MAX_GRAPHEMES:
            return text
    return text[:MAX_GRAPHEMES]


def recap_alt(d):
    out = f"Recap card for {d['day'].strftime('%A %B')} {d['day'].day} in {PLACE}: measured {d['exact']:g} out of 100, {d['word']}."
    rk, h = d.get("rank"), d.get("histogram")
    if rk and h:
        n = sum(h)
        below = sum(h[: int(d["exact"])])
        out += (f" {rank_line(rk)} Chart: how many of the {n} measured days scored each value from 0 to 100; "
                f"today's marker sits above {round(100 * below / n)}% of them. ")
        out += "Days ranked around it: " + "; ".join(
            f"#{x['rank']} {short_date(x['date'])} {x['score']:g}" + (" (today)" if x["date"] == d["day"].isoformat() else "")
            for x in d.get("neighbors") or []) + "."
    else:
        out += " Hourly scores: " + ", ".join(f"{hour_label(t.hour)} {s}" for t, s, _ in d["hours"]) + "."
    return out[:2000]


def day_rank(today):
    """Today's place among every measured day (Better Weather >= 0.3.3), only once today itself is ranked."""
    try:
        st = ha_state(RANK_ID)
    except requests.RequestException:
        return None
    a = st.get("attributes") or {}
    latest = a.get("latest") or {}
    if latest.get("date") != today.isoformat() or not latest.get("rank") or not latest.get("of"):
        return None
    return {"rank": latest["rank"], "of": latest["of"], "tied": latest.get("tied") or 0,
            "score": latest.get("score"), "since": a.get("since")}


def ordinal(n):
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def rank_line(rk):
    since = ""
    if rk.get("since"):
        s = date.fromisoformat(rk["since"])
        since = f" since {s.strftime('%b')} {s.year}"
    if rk["rank"] == 1 and not rk["tied"]:
        return f"Best day measured{since} ({rk['of']} days)."
    place = "best" if rk["rank"] == 1 else f"{ordinal(rk['rank'])} best"
    if rk["rank"] > rk["of"] / 2:
        worst = rk["of"] - rk["rank"] - rk["tied"] + 1
        place = "worst" if worst == 1 else f"{ordinal(worst)} worst"
    return f"Today {'tied for' if rk['tied'] else 'was the'} {place} of {rk['of']} days measured{since}."


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


def gather_now():
    now = datetime.now(TZ)
    st = ha_state(NOW_ID)
    score = number(st)
    if score is None:
        raise RuntimeError(f"{NOW_ID} unavailable ({st.get('state')})")
    a = st.get("attributes") or {}
    td = ha_state(TODAY_ID)
    ta = td.get("attributes") or {}
    today = number(td)
    return {
        "slot": "now", "day": now.date(), "at": now, "score": round(score), "word": word(score),
        "breakdown": a.get("breakdown") or [], "why": a.get("why") or "", "inputs": a.get("inputs") or {},
        "sun_counted": a.get("sun_counted"), "hours": local_hours(ta.get("hourly") or []),
        "today": None if today is None else round(today), "rain_hours": 0, "daily": {},
    }


def clock(t):
    return t.strftime("%I:%M%p").lstrip("0").lower()


def now_text(d):
    t, i = d["at"], d["inputs"]
    lines = [f"Right now in {PLACE} ({clock(t)}): {d['score']}/100, {d['word']}."]
    if d["why"].startswith("Raining"):
        lines.append("Raining, so it's capped.")
    bits = []
    if i.get("feels_f") is not None:
        bits.append(f"feels {round(i['feels_f'])}°")
    if i.get("dew_f") is not None:
        bits.append(f"dew point {round(i['dew_f'])}°")
    if i.get("wind_mph") is not None:
        g = i.get("gust_mph")
        bits.append(f"wind {round(i['wind_mph'])} mph" + (f", gusts {round(g)}" if g is not None and g >= 15 else ""))
    if d["sun_counted"] and i.get("sun_pct") is not None:
        bits.append(f"sun {round(i['sun_pct'])}% of a clear sky")
    if bits:
        ln = ", ".join(bits) + "."
        lines.append(ln[0].upper() + ln[1:])
    fl = factor_line(d["breakdown"])
    if fl and fl.startswith("Docked"):
        lines.append(fl)
    rest = [h for h in d["hours"] if h[0] > t]
    tail = []
    if rest:
        lo, hi = min(s for _, s, _ in rest), max(s for _, s, _ in rest)
        rng = f"{lo}" if lo == hi else f"{lo}–{hi}"
        if lo >= 80:
            tail.append(f"Rest of today: {rng}, nice through {hour_label(rest[-1][0].hour + 1, end=True)}.")
        else:
            tail.append(f"Rest of today: {rng}, best {window_text(best_window(rest))}.")
    if d["today"] is not None:
        tail.append(f"Day avg {d['today']}.")
    if tail:
        lines.append(" ".join(tail))
    # Trim whole lines, least important first, rather than cutting mid-sentence.
    for drop in (None, "Docked", "Rest of today"):
        if drop:
            lines = [ln for ln in lines if not ln.startswith(drop)]
        text = "\n".join(lines)
        if len(text) <= MAX_GRAPHEMES:
            return text
    return text[:MAX_GRAPHEMES]


def now_alt(d):
    hrs = ", ".join(f"{hour_label(t.hour)} {s} ({'measured' if src == 'station' else 'forecast'})" for t, s, src in d["hours"])
    rows = "; ".join(f"{r['label']} {round(r['points'])} of {round(r['max'])} ({r.get('detail', '')})" for r in d["breakdown"])
    return (f"Nice score right now ({clock(d['at'])}, {d['day'].strftime('%A %B')} {d['day'].day}) in {PLACE}: "
            f"{d['score']} out of 100, {d['word']}. Factors: {rows}. Today's hours: {hrs}.")[:2000]


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
    is_now = d["slot"] == "now"
    when = "Right now" if is_now else "Today" if d["slot"] == "morning" else "Tomorrow"
    g.text((M, 44), "NICE JAWN", font=font(22, True), fill=MUTED)
    sub = f"{clock(d['at'])} · {PLACE}" if is_now else f"{d['day'].strftime('%A, %B')} {d['day'].day} · {PLACE}"
    g.text((M, 74), f"{when} · {sub}", font=font(30, True), fill=FG)
    if is_now and d.get("today") is not None:
        g.text((M, 116), f"Today's day average: {d['today']}", font=font(22), fill=MUTED)
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
        if src != "station":   # forecast hours faded, like the HA card (solid = measured)
            fade = 0.6 if is_now else 0.35
            c = tuple(int(v + (b - v) * fade) for v, b in zip(c, BG))
        g.rounded_rectangle((x0, base - h, x0 + bw, base), radius=5, fill=c)
        cur = is_now and t.hour == d["at"].hour
        if cur:
            g.text((x0 + bw / 2, base + 6), "now", font=font(20, True), fill=FG, anchor="ma")
        elif (i % 2 == 0 or n <= 8) and not (is_now and abs(t.hour - d["at"].hour) == 1):
            g.text((x0 + bw / 2, base + 8), hour_label(t.hour), font=font(19), fill=MUTED, anchor="ma")
    foot = ("Today's hours: solid = measured in the backyard · faded = forecast" if is_now else
            "0–100 for temp, humidity, wind & sun, each waking hour · backyard station + GEM/ICON/ECMWF blend")
    g.text((M, Hh - 18), foot, font=font(17), fill=MUTED, anchor="lb")
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def recap_card(d):
    """Where today falls among every measured day: distribution with a marker + the days ranked around it."""
    im = Image.new("RGB", (W, Hh), BG)
    g = ImageDraw.Draw(im)
    M = 56
    rk, hist, nb = d["rank"], d["histogram"], d["neighbors"]
    today_iso = d["day"].isoformat()
    g.text((M, 44), "NICE JAWN · TODAY'S RECAP", font=font(22, True), fill=MUTED)
    g.text((M, 74), f"{d['day'].strftime('%A, %B')} {d['day'].day} · {PLACE}", font=font(30, True), fill=FG)
    place = f"#{rk['rank']}" + (" (tie)" if rk["tied"] else "")
    since = date.fromisoformat(rk["since"]) if rk.get("since") else None
    g.text((M, 116), f"{place} of {rk['of']} days measured" + (f" since {since.strftime('%b')} {since.year}" if since else ""),
           font=font(24), fill=MUTED)
    hue = col(d["exact"])     # one colour for today everywhere on the card
    g.text((W - M, 40), f"{d['exact']:.1f}", font=font(100, True), fill=hue, anchor="ra")
    g.text((W - M, 158), d["word"], font=font(28, True), fill=FG, anchor="ra")

    # distribution: 2-point bins, today's bin in full color, the rest faded
    top, base, x0, x1 = 214, 372, M, W - M
    bins = [sum(hist[i:i + 2]) for i in range(0, 100, 2)]
    bins[-1] += hist[100]
    peak = max(bins) or 1
    bw = (x1 - x0) / len(bins)
    tb = min(int(d["exact"]) // 2, len(bins) - 1)
    today_top = base
    for i, c in enumerate(bins):
        if not c:
            continue
        h = max(4, (base - top - 26) * c / peak)
        if i == tb:
            h = max(h, 12)                  # today's bin always visible, even when it's a lonely top day
            colr = hue
            today_top = base - h
        else:
            colr = tuple(int(v + (b - v) * 0.62) for v, b in zip(col(i * 2 + 1), BG))
        g.rounded_rectangle((x0 + i * bw + 1.5, base - h, x0 + (i + 1) * bw - 1.5, base), radius=3, fill=colr)
        if i == 17 and c == peak:           # the 34-36 bin: every rainy day is capped at 35
            g.text((x0 + (i + 1) * bw + 8, base - h + 2), "rain days", font=font(17), fill=MUTED, anchor="la")
    g.line([(x0, base + 1), (x1, base + 1)], fill=TRACK, width=2)
    for v in (0, 25, 50, 75, 100):
        g.text((x0 + (x1 - x0) * v / 100, base + 8), str(v), font=font(18), fill=MUTED,
               anchor="la" if v == 0 else "ra" if v == 100 else "ma")
    mx = x0 + (tb + 0.5) * bw                 # pointer over the centre of today's bin, stopping at its top
    g.line([(mx, top + 22), (mx, today_top - 4)], fill=FG, width=3)
    g.polygon([(mx - 7, today_top - 12), (mx + 7, today_top - 12), (mx, today_top - 3)], fill=FG)
    lbl = f"today {d['exact']:.1f}"
    anchor = "ra" if mx > x1 - 120 else "la" if mx < x0 + 120 else "ma"
    g.text((mx + (-8 if anchor == "ra" else 8 if anchor == "la" else 0), top - 4), lbl, font=font(20, True), fill=FG, anchor=anchor)
    g.text((x0, top - 4), "Every measured day", font=font(20, True), fill=MUTED, anchor="la") if anchor != "la" else None

    # the days ranked around it
    y = 418
    g.text((M, y), "Ranked around it", font=font(20, True), fill=MUTED)
    y += 32
    lo = min(n["score"] for n in nb)
    hi = max(n["score"] for n in nb)
    span = max(hi - lo, 1.0)
    for n in nb:
        me = n["date"] == today_iso
        f = font(23, me)
        colr = FG if me else MUTED
        dd = date.fromisoformat(n["date"])
        g.text((M, y), f"#{n['rank']}", font=f, fill=colr)
        g.text((M + 76, y), ("Today · " if me else "") + f"{dd.strftime('%a %b')} {dd.day}, {dd.year}", font=f, fill=colr)
        bar_x, bar_w = M + 470, 470
        # bars scaled to the window so a 0.1-point gap is visible; the number carries the real value
        frac = 0.35 + 0.65 * (n["score"] - lo) / span
        bc = hue if me else tuple(int(v + (b - v) * 0.5) for v, b in zip(col(n["score"]), BG))
        g.rounded_rectangle((bar_x, y + 6, bar_x + bar_w * frac, y + 22), radius=8, fill=bc)
        g.text((W - M, y), f"{n['score']:.1f}" + (" ☂" if n.get("rain") else ""), font=f, fill=colr, anchor="ra")
        y += 34
    g.text((M, Hh - 14), "Measured in a South Philly backyard, 8am–8pm · rain caps a day at 35 · bars zoomed to these five",
           font=font(16), fill=MUTED, anchor="lb")
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------- bluesky
class Bsky:
    """One login, reused: refreshes on expiry instead of calling createSession every poll (that's rate-limited)."""

    def __init__(self):
        self.pds = os.environ.get("BSKY_PDS", "https://bsky.social").rstrip("/")
        self.sess = None

    def _login(self):
        r = requests.post(f"{self.pds}/xrpc/com.atproto.server.createSession",
                          json={"identifier": os.environ["BSKY_HANDLE"], "password": os.environ["BSKY_APP_PASSWORD"]},
                          timeout=30)
        r.raise_for_status()
        self.sess = r.json()

    def _refresh(self):
        r = requests.post(f"{self.pds}/xrpc/com.atproto.server.refreshSession",
                          headers={"Authorization": f"Bearer {self.sess['refreshJwt']}"}, timeout=30)
        if r.ok:
            self.sess.update(r.json())
        else:
            self._login()

    @property
    def did(self):
        if not self.sess:
            self._login()
        return self.sess["did"]

    def call(self, method, nsid, headers=None, **kw):
        if not self.sess:
            self._login()
        for attempt in (0, 1):
            h = {"Authorization": f"Bearer {self.sess['accessJwt']}"} | (headers or {})
            r = requests.request(method, f"{self.pds}/xrpc/{nsid}", headers=h, timeout=60, **kw)
            if attempt == 0 and r.status_code in (400, 401):
                try:
                    err = r.json().get("error")
                except ValueError:
                    err = None
                if r.status_code == 401 or err in ("ExpiredToken", "InvalidToken"):
                    self._refresh()
                    continue
            r.raise_for_status()
            return r.json() if r.content else {}

    def post(self, text, png, alt, reply=None):
        blob = self.call("POST", "com.atproto.repo.uploadBlob", data=png, headers={"Content-Type": "image/png"})["blob"]
        record = {
            "$type": "app.bsky.feed.post", "text": text, "langs": ["en"],
            "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "embed": {"$type": "app.bsky.embed.images",
                      "images": [{"alt": alt, "image": blob, "aspectRatio": {"width": W, "height": Hh}}]},
        }
        if reply:
            record["reply"] = reply
        r = self.call("POST", "com.atproto.repo.createRecord",
                      json={"repo": self.did, "collection": "app.bsky.feed.post", "record": record})
        return f"https://bsky.app/profile/{self.did}/post/{r['uri'].rsplit('/', 1)[-1]}", r


_CLIENT = None


def client():
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = Bsky()
    return _CLIENT


def bsky_post(text, png, alt):
    return client().post(text, png, alt)[0]


# ---------------------------------------------------------------- mentions (on-demand replies)
TOMORROW_RE = re.compile(r"\b(tomorrow|tmrw|tmr|tomorow)\b", re.I)
REPLY_COOLDOWN_S = int(os.environ.get("NJ_REPLY_COOLDOWN_S", "120"))   # per asker
REPLY_HOURLY_CAP = int(os.environ.get("NJ_REPLY_HOURLY_CAP", "20"))    # whole account
MENTION_BACKLOG_MIN = 30    # on first start, answer mentions at most this old


def mentions_me(record, did):
    for f in record.get("facets") or []:
        for feat in f.get("features") or []:
            if feat.get("$type") == "app.bsky.richtext.facet#mention" and feat.get("did") == did:
                return True
    return False


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def build_reply(text_in):
    if TOMORROW_RE.search(text_in or ""):
        d = gather("evening")
        return post_text(d), card(d), alt_text(d), "tomorrow"
    d = gather_now()
    return now_text(d), card(d), now_alt(d), "now"


def check_mentions(bs, mode):
    """Answer new @mentions (and replies that @mention us) in-thread. Returns how many were answered."""
    st = load_state()
    m = st.setdefault("mentions", {})
    now = datetime.now(UTC)
    since = ts(m["since"]) if m.get("since") else now - timedelta(minutes=MENTION_BACKLOG_MIN)
    notes = bs.call("GET", "app.bsky.notification.listNotifications", params={"limit": 40}).get("notifications") or []
    handled = set(m.get("handled", []))
    last_by = m.setdefault("last_by", {})
    sent = [t for t in m.get("sent", []) if now - ts(t) < timedelta(hours=1)]
    newest, answered = since, 0
    for n in sorted(notes, key=lambda x: x["indexedAt"]):
        at = ts(n["indexedAt"])
        if at <= since or n["uri"] in handled:
            continue
        newest = max(newest, at)
        rec, author = n.get("record") or {}, (n.get("author") or {}).get("did")
        if author == bs.did or n["reason"] not in ("mention", "reply"):
            continue
        if n["reason"] == "reply" and not mentions_me(rec, bs.did):
            continue        # a plain reply to one of our posts ("lol") isn't a request
        handled.add(n["uri"])
        who = (n.get("author") or {}).get("handle", author)
        if author in last_by and (now - ts(last_by[author])).total_seconds() < REPLY_COOLDOWN_S:
            log.info("mention from %s skipped: cooldown", who)
            continue
        if len(sent) >= REPLY_HOURLY_CAP:
            log.warning("mention from %s skipped: hourly cap", who)
            continue
        text, png, alt, kind = build_reply(rec.get("text", ""))
        parent = {"uri": n["uri"], "cid": n["cid"]}
        root = (rec.get("reply") or {}).get("root") or parent
        log.info("mention from %s (%s) -> %s reply%s:\n%s", who, n["uri"], kind, "" if mode == "live" else " [dry]", text)
        if mode == "live":
            url, _ = bs.post(text, png, alt, reply={"root": root, "parent": parent})
            log.info("replied %s", url)
        last_by[author] = now.isoformat()
        sent.append(now.isoformat())
        answered += 1
    m["since"] = newest.isoformat()
    m["handled"] = sorted(handled)[-300:]
    m["sent"] = sent
    m["last_by"] = {k: v for k, v in last_by.items() if now - ts(v) < timedelta(days=1)}
    st["mentions"] = m
    save_state(st)
    return answered


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


def run(slot, *, dry_run=False, out=None, force=False):
    if slot == "now":
        d = gather_now()
        text, png, alt = now_text(d), card(d), now_alt(d)
        dry_run = True      # "now" is only ever a reply; rendering it standalone never posts
    elif slot == "recap":
        d = gather_recap(force=force)
        text, png, alt = recap_text(d), recap_card(d) if d.get("histogram") else card(d | {"slot": "morning", "breakdown": []}), recap_alt(d)
    else:
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
    return {"morning": at("NJ_MORNING", "07:00"), "recap": at("NJ_RECAP", "20:30"), "evening": at("NJ_EVENING", "20:31")}


def serve():
    """Post each slot once per day (catch up a missed slot for up to 45 min, never later) and answer mentions."""
    replies = os.environ.get("NJ_REPLIES", "dry")
    log.info("serving: %s, dry_run=%s, replies=%s", {k: v.strftime("%H:%M") for k, v in slot_times(date.today()).items()},
             os.environ.get("NJ_DRY_RUN") == "1", replies)
    # Startup self-check: render every post type without posting, so a deploy proves HA + rendering work.
    for slot in ("morning", "recap", "evening", "now"):
        try:
            run(slot, dry_run=True, force=True)
        except Exception:  # noqa: BLE001
            log.exception("startup self-check failed for %s", slot)
    poll = int(os.environ.get("NJ_MENTION_POLL_S", "20"))
    backoff = 0
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
                    # Recap waits for today's rank; past the wait it goes out without the rank rather than not at all.
                    run(slot, force=slot == "recap" and now >= at + timedelta(minutes=RECAP_WAIT_MIN))
                    st = load_state()
                    st["posted"] = sorted(set(st.get("posted", [])) | {key})[-30:]
                    save_state(st)
                    break
                except NotReady as e:
                    log.info("%s: %s; retrying", slot, e)
                    break
                except Exception:  # noqa: BLE001 - log and retry; never crash the loop
                    log.exception("%s attempt %d failed", slot, attempt + 1)
                    time.sleep(120)
        if replies in ("live", "dry"):
            try:
                check_mentions(client(), replies)
                backoff = 0
            except Exception:  # noqa: BLE001
                backoff = min(300, max(60, backoff * 2))
                log.exception("mention check failed; backing off %ds", backoff)
                time.sleep(backoff)
        time.sleep(poll)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    args = sys.argv[1:]
    if not args or args[0] == "serve":
        serve()
    else:
        out = args[args.index("--out") + 1] if "--out" in args else None
        run(args[0], dry_run="--dry-run" in args, out=out, force="--force" in args)
