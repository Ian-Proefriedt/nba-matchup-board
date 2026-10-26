"""Build site/data.json for the NBA matchup board.

Pulls tonight's lineups, injuries, odds and refs from Rotowire's public
lineups page, then each relevant player's last 10 games from StatMuse.
"""

import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
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


def main():
    print("Fetching Rotowire lineups...")
    slate_date, games = parse_lineups(fetch(LINEUPS_URL))
    print(f"  {len(games)} games for {slate_date}")
    if not games:
        sys.exit("No games parsed from Rotowire; page layout may have changed.")

    players = {}
    for g in games:
        for side in ("away", "home"):
            for p in g[side]["starters"] + g[side]["mayNotPlay"]:
                if p["status"] not in SKIP_STATS_FOR:
                    players[p["name"]] = None

    print(f"Fetching StatMuse for {len(players)} players...")
    failed = []
    for i, name in enumerate(players, 1):
        try:
            players[name] = parse_statmuse(fetch(STATMUSE_URL.format(slug=slugify(name))), name)
        except Exception as err:  # keep going; one bad page shouldn't sink the board
            print(f"  {name}: {err}", file=sys.stderr)
        if players[name] is None:
            failed.append(name)
        if i % 20 == 0:
            print(f"  {i}/{len(players)}")
        time.sleep(STATMUSE_DELAY)

    for g in games:
        for side in ("away", "home"):
            for p in g[side]["starters"] + g[side]["mayNotPlay"]:
                p["last10"] = players.get(p["name"])

    data = {
        "slateDate": slate_date,
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "games": games,
        "missingStats": failed,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, indent=1), encoding="utf-8")
    print(f"Wrote {OUT_PATH} ({len(failed)} players without StatMuse data)")


if __name__ == "__main__":
    main()
