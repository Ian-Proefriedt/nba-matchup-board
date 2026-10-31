"""Build site/data.json for the NBA matchup board.

Pulls tonight's lineups, injuries, odds and refs from Rotowire's public
lineups page, player points/rebounds/assists/threes lines from Rotowire's props page,
projected minutes from FanDuel Research, then each relevant player's last
10 games from StatMuse. Each prop then gets a suggested side (see suggest()),
and, when ODDS_API_KEY is set, exact FanDuel/DraftKings bet slip links from The Odds API.
"""

import json
import math
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from statistics import mean, pstdev
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

LINEUPS_URL = "https://www.rotowire.com/basketball/nba-lineups.php"
STATMUSE_URL = "https://www.statmuse.com/nba/ask/{slug}-last-10-games"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
STATMUSE_DELAY = 1.5  # seconds between StatMuse requests
SKIP_STATS_FOR = {"Out", "OFS"}  # no point pulling stats for players ruled out
BOOKS = ["fanduel", "draftkings", "betmgm", "composite"]

OUT_PATH = Path(__file__).parent / "site" / "data.json"
CACHE_PATH = Path(__file__).parent / "cache" / "statmuse.json"


def fetch(url, retries=2):
    for attempt in range(retries + 1):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
            with urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError) as err:
            if attempt == retries:
                raise
            print(f"  retry {url}: {err}", file=sys.stderr)
            time.sleep(3 * (attempt + 1))


def text(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def plain(s):
    """Lowercase and strip accents, so 'Dončić' matches 'Doncic'."""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).lower()


def slugify(name):
    name = plain(name).replace(".", "").replace("'", "")
    return re.sub(r"[^a-z0-9]+", "-", name).strip("-")


# ---------- Rotowire ----------

def parse_player(li):
    link = li.find("a")
    pct = re.search(r"is-pct-play-(\d+)", " ".join(li.get("class", [])))
    return {
        "pos": text(li.find(class_="lineup__pos")),
        "name": link.get("title") or text(link),
        "status": text(li.find(class_="lineup__inj")) or None,
        "playPct": int(pct.group(1)) if pct else None,
    }


def parse_side(ul):
    status_li = ul.find(class_="lineup__status")
    side = {
        "lineupStatus": text(status_li) or None,
        "starters": [],
        "mayNotPlay": [],
    }
    bucket = side["starters"]
    for li in ul.find_all("li", recursive=False):
        classes = li.get("class", [])
        if "lineup__title" in classes:
            bucket = side["mayNotPlay"]
        elif "lineup__player" in classes and li.find("a"):
            bucket.append(parse_player(li))
    return side


def parse_odds(box):
    odds = {}
    for item in box.select(".lineup__odds-item"):
        label = text(item.find("b")).replace("\xa0", " ").strip().upper()
        value = None
        for book in BOOKS:
            span = item.find("span", class_=book)
            val = text(span)
            if val and val not in ("–", "-"):
                value = val
                break
        key = {"LINE": "moneyline", "SPREAD": "spread", "O/U": "total"}.get(label)
        if key:
            odds[key] = value
    return odds


def parse_lineups(html):
    soup = BeautifulSoup(html, "html.parser")
    date_el = soup.find(class_="page-title__secondary")
    slate_date = re.sub(r"^Starting lineups for\s*", "", text(date_el)) or None

    games = []
    for game in soup.select("div.lineup.is-nba"):
        box = game.find(class_="lineup__box")
        if not box:
            continue  # ad / tools tiles
        teams = box.select(".lineup__team")
        mteams = box.select(".lineup__mteam")
        lists = box.select("ul.lineup__list")
        if len(teams) != 2 or len(lists) != 2:
            continue

        def team(i):
            nickname = text(mteams[i]).split("(")[0].strip() if i < len(mteams) else ""
            record = text(mteams[i].find(class_="lineup__wl")).strip("()") if i < len(mteams) else ""
            return {"abbr": text(teams[i].find(class_="lineup__abbr")), "name": nickname, "record": record}

        refs = [text(a) for a in box.select(".lineup__umpire a")]
        games.append({
            "time": text(game.find(class_="lineup__time")),
            "away": {**team(0), **parse_side(lists[0])},
            "home": {**team(1), **parse_side(lists[1])},
            "odds": parse_odds(box),
            "refs": refs,
        })
    return slate_date, games


# ---------- FanDuel Research projections ----------

FD_GRAPHQL = "https://www.fanduel.com/research/api/graphql"
FD_SLATES = "query GetSlates($sport: ProjectionSports!) { getSlates(sport: $sport) { id name } }"
FD_PROJECTIONS = """query GetProjections($input: ProjectionsInput!) { getProjections(input: $input) {
  ... on NbaPlayer { player { name position } team { abbreviation }
    minutes points rebounds assists threePointsMade } } }"""
BENCH_MIN_MINUTES = 10  # bench players projected under this are left off the board
BENCH_MAX = 5

# Rotowire, FanDuel and others disagree on a few team codes
TEAM_ALIASES = {"UTAH": "UTA", "NY": "NYK", "GS": "GSW", "SA": "SAS", "NO": "NOP", "WSH": "WAS",
                "PHO": "PHX", "BRK": "BKN", "CHO": "CHA"}


def team_code(abbr):
    return TEAM_ALIASES.get(abbr.upper(), abbr.upper())


def fd_query(query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = Request(FD_GRAPHQL, data=body, headers={
        "User-Agent": USER_AGENT, "Content-Type": "application/json",
        "Origin": "https://www.fanduel.com", "Referer": "https://www.fanduel.com/research/nba/fantasy/dfs-projections",
    })
    with urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    if data.get("errors"):
        raise RuntimeError(data["errors"][0].get("message"))
    return data["data"]


def fetch_fanduel(teams_needed):
    """Projected minutes by team code, from the fewest FanDuel slates that cover tonight's teams."""
    slates = fd_query(FD_SLATES, {"sport": "NBA"})["getSlates"] or []
    # 'All Day' usually covers every game; fall back to the others, snake drafts last
    slates.sort(key=lambda s: (0 if "all day" in s["name"].lower() else 2 if "snake" in s["name"].lower() else 1))
    by_team, seen = {}, set()
    for slate in slates[:8]:
        rows = fd_query(FD_PROJECTIONS, {"input": {"type": "DAILY", "position": "NBA_PLAYER", "sport": "NBA",
                                                   "slateId": slate["id"]}})["getProjections"] or []
        for r in rows:
            team = team_code(r["team"]["abbreviation"])
            if (team, r["player"]["name"]) in seen:
                continue
            seen.add((team, r["player"]["name"]))
            by_team.setdefault(team, []).append({
                "name": r["player"]["name"], "pos": r["player"]["position"], "min": r["minutes"],
                "pts": r["points"], "reb": r["rebounds"], "ast": r["assists"], "threes": r["threePointsMade"],
            })
        if teams_needed <= by_team.keys():
            break
        time.sleep(1)
    return by_team


def name_key(name):
    words = re.sub(r"[^a-z ]", "", plain(name).replace("-", " ")).split()
    return " ".join(w for w in words if w not in {"jr", "sr", "ii", "iii", "iv"})


def find_projection(name, rows):
    key = name_key(name)
    for r in rows:
        if name_key(r["name"]) == key:
            return r
    # 'Ronald Holland' vs 'Ron Holland II': fall back to a unique last name on the team
    last = key.split()[-1] if key else ""
    same_last = [r for r in rows if name_key(r["name"]).split()[-1:] == [last]]
    return same_last[0] if len(same_last) == 1 else None


def round_proj(r):
    return {k: (round(r[k], 1) if r[k] is not None else None) for k in ("min", "pts", "reb", "ast", "threes")}


def attach_projections(side, rows):
    used = set()
    for p in side["starters"] + side["mayNotPlay"]:
        r = find_projection(p["name"], rows)
        p["proj"] = round_proj(r) if r else None
        if r:
            used.add(r["name"])
    listed = {name_key(p["name"]) for p in side["mayNotPlay"]}
    bench = [r for r in rows if r["name"] not in used and name_key(r["name"]) not in listed
             and (r["min"] or 0) >= BENCH_MIN_MINUTES]
    bench.sort(key=lambda r: -(r["min"] or 0))
    side["bench"] = [{"pos": r["pos"], "name": r["name"], "status": None, "playPct": None, "proj": round_proj(r)}
                     for r in bench[:BENCH_MAX]]


# ---------- Rotowire player props ----------

PROPS_URL = "https://www.rotowire.com/betting/nba/player-props.php"
PROP_TYPES = ["pts", "reb", "ast", "threes"]
PROP_BOOKS = ["fanduel", "draftkings", "mgm", "caesars", "betrivers", "hardrock", "thescore", "betr"]  # first with both sides wins


LINK_BOOKS = ["fanduel", "draftkings"]  # books the board links out to


def parse_props(html):
    """Lines by (team, player key) -> {"pts": {...}, "reb": {...}, ...} from the tables embedded in the props page.

    The top-level line/over/under is the first book in PROP_BOOKS with both sides; "books" keeps
    every FanDuel and DraftKings line so the board can compare prices and link to either.
    """
    props = {}
    for m in re.finditer(r"data:\s*(\[.*?\])\s*,\s*\n", html, flags=re.S):
        try:
            rows = json.loads(m.group(1))
        except ValueError:
            continue
        if not rows:
            continue
        # single-prop tables only: they carry over/under prices, the combined tables just repeat the lines
        kinds = [t for t in PROP_TYPES if f"fanduel_{t}Over" in rows[0]]
        if len(kinds) != 1:
            continue
        kind = kinds[0]
        for r in rows:
            found, books = None, {}
            for book in PROP_BOOKS:
                line, over, under = r.get(f"{book}_{kind}"), r.get(f"{book}_{kind}Over"), r.get(f"{book}_{kind}Under")
                if not (line and over and under):
                    continue
                entry = {"line": float(line), "over": int(over), "under": int(under)}
                if found is None:
                    found = {**entry, "book": book}
                if book in LINK_BOOKS:
                    books[book] = entry
            if found:
                key = (team_code(r.get("team", "")), name_key(r.get("name", "")))
                props.setdefault(key, {})[kind] = {**found, "books": books}
    return props


def attach_props(team, side, props):
    team_props = {k[1]: v for k, v in props.items() if k[0] == team}
    for p in side["starters"] + side["mayNotPlay"] + side["bench"]:
        key = name_key(p["name"])
        found = team_props.get(key)
        if found is None:  # same last-name fallback as projections
            last = key.split()[-1:] if key else []
            same = [v for k, v in team_props.items() if k.split()[-1:] == last]
            found = same[0] if len(same) == 1 else None
        p["props"] = found


# ---------- The Odds API: exact bet slip links (optional) ----------

ODDS_API = "https://api.the-odds-api.com/v4/sports/basketball_nba"
ODDS_API_MARKETS = {"player_points": "pts", "player_rebounds": "reb", "player_assists": "ast", "player_threes": "threes"}
ODDS_API_CACHE = Path(__file__).parent / "cache" / "oddsapi.json"
# Each game costs one credit per market returned (4 per game), so don't re-pull on every 30-minute run
ODDS_API_REFRESH_MIN = int(os.environ.get("ODDS_API_REFRESH_MIN") or 180)


def odds_api_get(path, **params):
    url = f"{ODDS_API}{path}?{urlencode({**params, 'apiKey': os.environ['ODDS_API_KEY']})}"
    with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=30) as resp:
        left = resp.headers.get("x-requests-remaining")
        if left is not None:
            print(f"  Odds API credits left: {left}")
        return json.load(resp)


def main_line(pairs):
    """Of several (over, under) pairs at different lines, the one priced closest to even is the main line."""
    best = None
    for line, sides in pairs.items():
        if "over" in sides and "under" in sides:
            gap = abs(implied(sides["over"]["price"]) - implied(sides["under"]["price"]))
            if best is None or gap < best[0]:
                best = (gap, line)
    return best and best[1]


def fetch_odds_api(games):
    """{"TEAM|player key": {kind: {book: {line, over, under, links, sids}}}} for FanDuel and DraftKings.

    sids are the books' own outcome ids: DraftKings fills a whole parlay onto the bet slip from
    sportsbook.draftkings.com/?outcomes=<id>+<id> (undocumented, but works), so the board needs them.
    """
    events = odds_api_get("/events")  # free: doesn't count against the quota
    now = datetime.now(timezone.utc)
    by_nick = {}
    for g in games:
        for side in ("away", "home"):
            by_nick[g[side]["name"].lower()] = team_code(g[side]["abbr"])
    out = {}
    for ev in events:
        teams = {}
        for full in (ev["home_team"], ev["away_team"]):
            code = next((c for nick, c in by_nick.items() if nick and full.lower().endswith(nick)), None)
            if code:
                teams[full] = code
        if len(teams) != 2:
            continue  # not on tonight's board
        if datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")) <= now:
            continue  # started: live lines, and not worth the credits
        data = odds_api_get(f"/events/{ev['id']}/odds", bookmakers=",".join(LINK_BOOKS),
                            markets=",".join(ODDS_API_MARKETS), oddsFormat="american",
                            includeLinks="true", includeSids="true")
        for bm in data.get("bookmakers", []):
            for mk in bm.get("markets", []):
                kind = ODDS_API_MARKETS.get(mk["key"])
                if not kind:
                    continue
                by_player = {}
                for o in mk.get("outcomes", []):
                    side = (o.get("name") or "").lower()
                    if side not in ("over", "under") or o.get("point") is None or not o.get("description"):
                        continue
                    link = o.get("link") or mk.get("link") or bm.get("link")
                    by_player.setdefault(o["description"], {}).setdefault(o["point"], {})[side] = {
                        "price": o["price"], "link": link, "sid": o.get("sid")}
                for player, pairs in by_player.items():
                    line = main_line(pairs)
                    if line is None:
                        continue
                    sides = pairs[line]
                    entry = {"line": float(line), "over": sides["over"]["price"], "under": sides["under"]["price"],
                             "links": {"over": sides["over"]["link"], "under": sides["under"]["link"]},
                             "sids": {"over": sides["over"]["sid"], "under": sides["under"]["sid"]}}
                    # outcomes don't name a team; the player is on one of the two in this game
                    for code in teams.values():
                        out.setdefault(f"{code}|{name_key(player)}", {}).setdefault(kind, {})[bm["key"]] = entry
        time.sleep(0.5)
    return out


def load_odds_api(games):
    """Bet slip lines/links, re-pulled at most every ODDS_API_REFRESH_MIN minutes. Empty without a key."""
    if not os.environ.get("ODDS_API_KEY"):
        return {}
    try:
        saved = json.loads(ODDS_API_CACHE.read_text(encoding="utf-8"))
        age = datetime.now(timezone.utc) - datetime.fromisoformat(saved["fetchedAt"])
        if saved.get("day") == cache_day() and age < timedelta(minutes=ODDS_API_REFRESH_MIN):
            print(f"  using Odds API pull from {int(age.total_seconds() // 60)} min ago")
            return saved["players"]
    except (OSError, ValueError, KeyError):
        pass
    players = fetch_odds_api(games)
    ODDS_API_CACHE.parent.mkdir(parents=True, exist_ok=True)
    ODDS_API_CACHE.write_text(json.dumps({"day": cache_day(), "fetchedAt": datetime.now(timezone.utc).isoformat(),
                                          "players": players}), encoding="utf-8")
    return players


def merge_odds_api(team, side, linked):
    """Overlay The Odds API's FanDuel/DraftKings lines (they carry bet slip links) onto Rotowire's."""
    for p in side["starters"] + side["mayNotPlay"] + side["bench"]:
        found = linked.get(f"{team}|{name_key(p['name'])}")
        if not found:
            continue
        p["props"] = p["props"] or {}
        for kind, books in found.items():
            if kind not in p["props"]:
                first = next(iter(books.items()))
                p["props"][kind] = {**{k: first[1][k] for k in ("line", "over", "under")}, "book": first[0], "books": {}}
            p["props"][kind]["books"].update(books)


# ---------- Suggested bets ----------

STRONG_EV, LEAN_EV = 0.10, 0.04  # expected profit per $1 staked
MIN_GAMES = 5
MARKET_WEIGHT = 0.6  # books are sharp: the model only gets to move their no-vig price part of the way
MAX_DISAGREE = 0.22  # a raw model this far from the book usually means a live/stale line or bad data, not an edge
SHAKY_STATUS = {"GTD", "Ques", "Doubt", "Prob"}
KIND_NAME = {"pts": "points", "reb": "rebounds", "ast": "assists", "threes": "threes"}


def implied(price):
    return 100 / (price + 100) if price > 0 else -price / (-price + 100)


def payout(price):
    """Profit on a $1 win."""
    return price / 100 if price > 0 else 100 / -price


def american(prob):
    prob = min(max(prob, 0.01), 0.99)
    return round(-100 * prob / (1 - prob)) if prob >= 0.5 else round(100 * (1 - prob) / prob)


def p_over_normal(mu, sigma, line):
    return 0.5 * math.erfc((line - mu) / (sigma * math.sqrt(2)))


def p_over_poisson(mu, line):
    # P(X > line) for a count: 1 - P(X <= floor(line))
    mu = max(mu, 0.05)
    term = cdf = math.exp(-mu)
    for k in range(1, int(math.floor(line)) + 1):
        term *= mu / k
        cdf += term
    return max(0.0, 1 - cdf)


def suggest(p, kind, started=False):
    """Model probability of the over for one prop, and the best FanDuel/DraftKings side to bet if there's an edge.

    The expected stat blends three estimates: FanDuel Research's projection, his per-minute rate over the
    last 10 times tonight's projected minutes, and a recency-weighted average (last 5 count double).
    That mean gives a distribution probability (normal, or Poisson for threes); it's averaged with a
    recency-weighted hit rate shrunk toward 50%, then pulled most of the way back to each book's no-vig price.
    No pick once the game has started (Rotowire then shows live lines) or when the raw model and the book
    disagree by more than MAX_DISAGREE.
    """
    prop = p["props"][kind]
    games = [g for g in (p.get("last10") or {}).get("games", []) if g.get(kind) is not None and g.get("min")]
    books = prop.get("books") or {}
    if len(games) < MIN_GAMES or not books or p.get("status") in SKIP_STATS_FOR:
        return None
    vals = [g[kind] for g in games]
    weights = [2.0 if i >= len(games) - 5 else 1.0 for i in range(len(games))]
    wavg = sum(v * w for v, w in zip(vals, weights)) / sum(weights)
    recent_min = mean(g["min"] for g in games)
    proj = p.get("proj") or {}
    proj_min = proj.get("min") or recent_min
    per_min = sum(vals) / sum(g["min"] for g in games)
    min_adj = per_min * proj_min
    if proj.get(kind) is not None:
        mu = 0.4 * proj[kind] + 0.35 * min_adj + 0.25 * wavg
    else:
        mu = 0.6 * min_adj + 0.4 * wavg
    sigma = max(pstdev(vals), math.sqrt(max(mu, 0.5)), 0.8)

    best, by_book = None, {}
    for book, b in books.items():
        line = b["line"]
        p_dist = p_over_poisson(mu, line) if kind == "threes" else p_over_normal(mu, sigma, line)
        w_hits = sum(w for v, w in zip(vals, weights) if v > line)
        p_hist = (w_hits + 2) / (sum(weights) + 4)
        fair_over = implied(b["over"]) / (implied(b["over"]) + implied(b["under"]))
        raw = 0.6 * p_dist + 0.4 * p_hist
        p_over = (1 - MARKET_WEIGHT) * raw + MARKET_WEIGHT * fair_over
        by_book[book] = round(p_over, 3)  # parlays price each leg at the book they're sent to
        for side, prob, fair, price in (("over", p_over, fair_over, b["over"]), ("under", 1 - p_over, 1 - fair_over, b["under"])):
            ev = prob * payout(price) - (1 - prob)
            if best is None or ev > best["ev"]:
                best = {"side": side, "book": book, "line": line, "price": price, "prob": prob, "fair": fair, "ev": ev,
                        "pOver": p_over, "off": abs(raw - fair_over) > MAX_DISAGREE}

    ev = best["ev"]
    grade = "strong" if ev >= STRONG_EV else "lean" if ev >= LEAN_EV else None
    if started or best["off"]:
        grade = None
    shaky = p.get("status") in SHAKY_STATUS
    if grade == "strong" and shaky:
        grade = "lean"
    line, side, over = best["line"], best["side"], best["side"] == "over"
    hits10 = sum(v > line for v in vals)
    hits5 = sum(v > line for v in vals[-5:])
    n5 = len(vals[-5:])
    avg5 = mean(vals[-5:])
    avg10 = mean(vals)
    label = KIND_NAME[kind]

    reasons = []
    went = hits10 if over else len(vals) - hits10
    went5 = hits5 if over else n5 - hits5
    if len(vals) > n5:
        reasons.append(f"Went {side} {fmt_line(line)} in {went} of last {len(vals)} ({went5} of last {n5})")
        reasons.append(f"Averaging {avg10:.1f} {label} over his last {len(vals)}, {avg5:.1f} over his last {n5}")
    else:
        reasons.append(f"Went {side} {fmt_line(line)} in {went} of last {len(vals)}")
        reasons.append(f"Averaging {avg10:.1f} {label} over his last {len(vals)}")
    if proj.get(kind) is not None:
        reasons.append(f"FanDuel Research projects {proj[kind]:.1f}")
    if proj.get("min") and abs(proj["min"] - recent_min) >= 2:
        more = proj["min"] > recent_min
        reasons.append(f"Projected {proj['min']:.0f} min, {'up' if more else 'down'} from {recent_min:.1f} recently"
                       + ("" if more == over else " (works against this pick)"))
    reasons.append(f"Model has the {side} at {best['prob'] * 100:.0f}%; the {book_name(best['book'])} price implies "
                   f"{best['fair'] * 100:.0f}% (fair price {fmt_price(american(best['prob']))})")
    for bk, b in books.items():
        if bk == best["book"]:
            continue
        if b["line"] == line:
            if b[side] != best["price"]:
                reasons.append(f"Better price than {book_name(bk)} ({fmt_price(best['price'])} vs {fmt_price(b[side])})")
        elif (b["line"] < line) == over:  # the other book's number is easier, but its price is worse
            reasons.append(f"{book_name(bk)} has an easier {fmt_line(b['line'])} at {fmt_price(b[side])}, "
                           f"but {possessive(book_name(best['book']))} price is the better value")
        else:
            reasons.append(f"Better number than {book_name(bk)} ({fmt_line(line)} vs {fmt_line(b['line'])})")
    cautions = []
    if started:
        cautions.append("Game has started: these are live lines, so no pick")
    elif best["off"]:
        cautions.append("History and the book disagree too much to trust: the line may have moved or be live, so no pick")
    if shaky:
        cautions.append(f"Listed {p['status']}: check he's playing before you bet")
    if len(vals) < 10:
        cautions.append(f"Only {len(vals)} games of history")

    return {
        "side": side, "book": best["book"], "line": line, "price": best["price"],
        "prob": round(best["prob"], 3), "fair": round(best["fair"], 3), "ev": round(ev, 3),
        "pOver": round(best["pOver"], 3), "pOverByBook": by_book, "mean": round(mu, 1), "grade": grade,
        "reasons": reasons, "cautions": cautions,
    }


def fmt_line(x):
    return f"{x:g}"


def fmt_price(n):
    return f"+{n}" if n > 0 else str(n)


def book_name(book):
    return {"fanduel": "FanDuel", "draftkings": "DraftKings"}.get(book, book)


def possessive(name):
    return name + ("'" if name.endswith("s") else "'s")


def tipoff(slate_date, game_time):
    """Tip-off as an aware datetime, from 'October 30, 2026' and '7:30 PM ET'; None if either won't parse."""
    try:
        naive = datetime.strptime(f"{slate_date} {game_time.replace('ET', '').strip()}", "%B %d, %Y %I:%M %p")
    except (TypeError, ValueError):
        return None
    return naive.replace(tzinfo=ZoneInfo("America/New_York"))


def attach_suggestions(side, started):
    for p in side["starters"] + side["mayNotPlay"] + side["bench"]:
        for kind in (p.get("props") or {}):
            p["props"][kind]["model"] = suggest(p, kind, started)


# ---------- StatMuse ----------

def parse_statmuse(html, name):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return None
    header = [text(th).upper() for th in table.find("thead").find_all("th")] if table.find("thead") else []
    if not header:
        first = table.find("tr")
        header = [text(c).upper() for c in first.find_all(["th", "td"])]

    def col(label):
        return header.index(label) if label in header else None

    idx = {k: col(k) for k in ["NAME", "DATE", "TM", "OPP", "MIN", "PTS", "REB", "AST", "3PM", "STL", "BLK", "TOV", "+/-"]}
    if idx["MIN"] is None or idx["DATE"] is None:
        return None
    ha_idx = idx["OPP"] - 1 if idx["OPP"] else None

    games = []
    last_name = plain(name.split()[-1])
    for tr in table.find("tbody").find_all("tr") if table.find("tbody") else table.find_all("tr")[1:]:
        cells = [text(td) for td in tr.find_all(["td", "th"])]
        if len(cells) < len(header):
            continue
        if not re.match(r"\d+/\d+/\d{4}", cells[idx["DATE"]]):
            continue  # Average / Total rows
        if idx["NAME"] is not None and last_name not in plain(cells[idx["NAME"]]):
            return None  # StatMuse answered about someone else

        def num(key):
            i = idx[key]
            if i is None:
                return None
            try:
                return float(cells[i].replace("+", ""))
            except ValueError:
                return None

        games.append({
            "date": cells[idx["DATE"]],
            "opp": cells[idx["OPP"]] if idx["OPP"] is not None else "",
            "homeAway": cells[ha_idx] if ha_idx is not None else "",
            "min": num("MIN"), "pts": num("PTS"), "reb": num("REB"), "ast": num("AST"),
            "threes": num("3PM"), "stl": num("STL"), "blk": num("BLK"), "tov": num("TOV"),
            "plusMinus": num("+/-"),
        })
    if not games:
        return None

    def avg(key, rows):
        vals = [g[key] for g in rows if g[key] is not None]
        return round(mean(vals), 1) if vals else None

    last5 = games[-5:]
    return {
        "games": games,
        "avg": {k: avg(k, games) for k in ["min", "pts", "reb", "ast", "threes"]},
        "avgLast5": {k: avg(k, last5) for k in ["min", "pts", "reb", "ast", "threes"]},
    }


def cache_day():
    # A 'day' rolls over at 6 AM ET, after the previous night's games are final
    return (datetime.now(ZoneInfo("America/New_York")) - timedelta(hours=6)).date().isoformat()


def load_cache():
    try:
        saved = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return saved["players"] if saved.get("day") == cache_day() else {}
    except (OSError, ValueError, KeyError):
        return {}


def save_cache(players):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps({"day": cache_day(), "players": players}), encoding="utf-8")


def main():
    print("Fetching Rotowire lineups...")
    slate_date, games = parse_lineups(fetch(LINEUPS_URL))
    print(f"  {len(games)} games for {slate_date}")
    if not games:
        sys.exit("No games parsed from Rotowire; page layout may have changed.")

    teams = {team_code(g[side]["abbr"]) for g in games for side in ("away", "home")}
    print("Fetching FanDuel Research projections...")
    try:
        projections = fetch_fanduel(teams)
        print(f"  {sum(len(v) for v in projections.values())} players across {len(projections)} teams")
    except Exception as err:  # the board still works without projections
        print(f"  FanDuel projections unavailable: {err}", file=sys.stderr)
        projections = {}
    for g in games:
        for side in ("away", "home"):
            attach_projections(g[side], projections.get(team_code(g[side]["abbr"]), []))

    print("Fetching Rotowire player props...")
    try:
        props = parse_props(fetch(PROPS_URL))
        print(f"  lines for {len(props)} players")
    except Exception as err:  # the board still works without props
        print(f"  player props unavailable: {err}", file=sys.stderr)
        props = {}
    for g in games:
        for side in ("away", "home"):
            attach_props(team_code(g[side]["abbr"]), g[side], props)

    players = {}
    for g in games:
        for side in ("away", "home"):
            for p in g[side]["starters"] + g[side]["mayNotPlay"] + g[side]["bench"]:
                if p["status"] not in SKIP_STATS_FOR:
                    players[p["name"]] = None

    # Game logs only change once games finish, so reuse today's cached pages and fetch only new names
    cache = load_cache()
    todo = [n for n in players if n not in cache]
    print(f"StatMuse: {len(players) - len(todo)} cached, fetching {len(todo)}...")
    for i, name in enumerate(todo, 1):
        try:
            cache[name] = parse_statmuse(fetch(STATMUSE_URL.format(slug=slugify(name))), name)
        except Exception as err:  # keep going; one bad page shouldn't sink the board
            print(f"  {name}: {err}", file=sys.stderr)
            cache[name] = None
        if i % 20 == 0:
            print(f"  {i}/{len(todo)}")
        time.sleep(STATMUSE_DELAY)
    save_cache(cache)
    for name in players:
        players[name] = cache.get(name)
    failed = [n for n in players if players[n] is None]

    for g in games:
        for side in ("away", "home"):
            for p in g[side]["starters"] + g[side]["mayNotPlay"] + g[side]["bench"]:
                p["last10"] = players.get(p["name"])

    linked = {}
    if os.environ.get("ODDS_API_KEY"):
        print("Fetching FanDuel/DraftKings bet slip links from The Odds API...")
        try:
            linked = load_odds_api(games)
            print(f"  links for {len(linked)} players")
        except Exception as err:  # the board falls back to each book's prop pages
            print(f"  Odds API unavailable: {err}", file=sys.stderr)
    now = datetime.now(timezone.utc)
    for g in games:
        start = tipoff(slate_date, g["time"])
        g["tipoff"] = start.isoformat() if start else None
        g["started"] = bool(start and now >= start)
        for side in ("away", "home"):
            merge_odds_api(team_code(g[side]["abbr"]), g[side], linked)
            attach_suggestions(g[side], g["started"])

    data = {
        "slateDate": slate_date,
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "games": games,
        "missingStats": failed,
        "hasProjections": bool(projections),
        "hasBetLinks": bool(linked),
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, indent=1), encoding="utf-8")
    print(f"Wrote {OUT_PATH} ({len(failed)} players without StatMuse data)")


if __name__ == "__main__":
    main()
