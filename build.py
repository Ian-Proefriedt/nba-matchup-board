"""Build site/data.json for the NBA matchup board.

Pulls tonight's lineups, injuries, odds and refs from Rotowire's public
lineups page, player points/rebounds/assists/threes lines from Rotowire's props page,
projected minutes from FanDuel Research, then each relevant player's last
10 games from StatMuse.
"""

import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from statistics import mean
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


def parse_props(html):
    """Lines by (team, player key) -> {"pts": {...}, "reb": {...}, ...} from the tables embedded in the props page."""
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
            for book in PROP_BOOKS:
                line, over, under = r.get(f"{book}_{kind}"), r.get(f"{book}_{kind}Over"), r.get(f"{book}_{kind}Under")
                if line and over and under:
                    key = (team_code(r.get("team", "")), name_key(r.get("name", "")))
                    props.setdefault(key, {})[kind] = {"line": float(line), "over": int(over), "under": int(under), "book": book}
                    break
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

    data = {
        "slateDate": slate_date,
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "games": games,
        "missingStats": failed,
        "hasProjections": bool(projections),
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, indent=1), encoding="utf-8")
    print(f"Wrote {OUT_PATH} ({len(failed)} players without StatMuse data)")


if __name__ == "__main__":
    main()
