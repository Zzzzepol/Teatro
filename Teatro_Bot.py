"""Teatro is a Discord bot for movie recommendations and movie nights."""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import random
import re
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Optional
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()




TOKEN = os.getenv("DISCORD_TOKEN", "")
TMDB_KEY = os.getenv("TMDB_API_KEY", "")
OMDB_KEY = os.getenv("OMDB_API_KEY", "")
REGION = os.getenv("REGION", "PH").upper()
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Manila"))
POST_HOUR, POST_MIN = (int(x) for x in os.getenv("DAILY_TIME", "09:00").split(":"))
DB_PATH = os.getenv("DB_PATH", "filmclub.db")
DEV_GUILD_ID = os.getenv("GUILD_ID")

MIN_RATING = 7.0
MIN_VOTES = 1000


ROTATION = {0: "streaming", 1: "hidden", 2: "streaming", 3: "classic",
            4: "cinema", 5: "streaming", 6: "filipino"}

MODE_LABELS = {"streaming": "Streaming pick", "cinema": "In cinemas", "hidden": "Hidden gem",
               "classic": "Classic", "filipino": "Filipino spotlight", "any": "Random pick"}

GENRES = {"Action": 28, "Adventure": 12, "Animation": 16, "Comedy": 35, "Crime": 80,
          "Documentary": 99, "Drama": 18, "Family": 10751, "Fantasy": 14, "History": 36,
          "Horror": 27, "Music": 10402, "Mystery": 9648, "Romance": 10749, "Sci-Fi": 878,
          "Thriller": 53, "War": 10752, "Western": 37}

MOODS = {
    "date_night": ("Date night", {"with_genres": "10749|35"}),
    "short": ("Something short (under 100 min)", {"with_runtime.lte": 100}),
    "not_too_scary": ("Thrilling, but not too scary", {"with_genres": "9648|53", "without_genres": "27"}),
    "feel_good": ("Feel-good", {"with_genres": "35|10751|10402"}),
    "mind_bending": ("Mind-bending", {"with_genres": "878|9648"}),
    "family": ("Family night", {"with_genres": "10751|16"}),
}

LANGS = {"English": "en", "Filipino": "tl", "Korean": "ko", "Japanese": "ja",
         "French": "fr", "Spanish": "es", "Hindi": "hi", "Thai": "th"}


CINEMAS = ["SM Cinema", "Ayala Malls Cinemas", "Robinsons Movieworld",
           "Megaworld Cinemas", "Gateway Cineplex", "Any cinema near me"]

VOTES = {"up": ("👍", "Loved it"), "meh": ("😐", "Meh"),
         "down": ("👎", "Not for me"), "seen": ("✅", "Seen it")}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("filmclub")





def today() -> date:
    return datetime.now(TZ).date()


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def trunc(s: Optional[str], n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def ts(d: date, style: str = "D") -> str:
    """Turn a date into a Discord timestamp."""
    return f"<t:{int(datetime.combine(d, dtime(0, 0), tzinfo=TZ).timestamp())}:{style}>"


def year_of(d: dict) -> str:
    return (d.get("release_date") or "")[:4]


def runtime_str(minutes: Optional[int]) -> str:
    if not minutes:
        return "N/A"
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if h else f"{m}m"





DB: aiosqlite.Connection
WRITE_LOCK = asyncio.Lock()
RATING_LOCK = asyncio.Lock()
DAILY_LOCK = asyncio.Lock()
ALLOWED_GUILD_ID = os.getenv("ALLOWED_GUILD_ID")

SCHEMA = """
CREATE TABLE IF NOT EXISTS job_parts(guild_id INTEGER,day TEXT,kind TEXT,PRIMARY KEY(guild_id,day,kind));
CREATE TABLE IF NOT EXISTS settings(guild_id INTEGER PRIMARY KEY, channel_id INTEGER, role_id INTEGER);
CREATE TABLE IF NOT EXISTS posts(message_id INTEGER PRIMARY KEY, guild_id INTEGER, channel_id INTEGER,
    tmdb_id INTEGER, title TEXT, links TEXT, cinema INTEGER, kind TEXT, posted_at TEXT);
CREATE TABLE IF NOT EXISTS votes(message_id INTEGER, user_id INTEGER, guild_id INTEGER, tmdb_id INTEGER,
    vote TEXT, created_at TEXT, PRIMARY KEY(message_id, user_id));
CREATE TABLE IF NOT EXISTS watchlist(guild_id INTEGER, user_id INTEGER, tmdb_id INTEGER, title TEXT,
    added_at TEXT, PRIMARY KEY(guild_id, user_id, tmdb_id));
CREATE TABLE IF NOT EXISTS notified(guild_id INTEGER, tmdb_id INTEGER, kind TEXT,
    PRIMARY KEY(guild_id, tmdb_id, kind));
CREATE TABLE IF NOT EXISTS services(user_id INTEGER PRIMARY KEY, provider_ids TEXT);
CREATE TABLE IF NOT EXISTS polls(message_id INTEGER PRIMARY KEY, options TEXT);
CREATE TABLE IF NOT EXISTS poll_votes(message_id INTEGER, user_id INTEGER, option INTEGER,
    PRIMARY KEY(message_id, user_id));
"""


async def q_all(sql: str, args: tuple = ()):
    async with DB.execute(sql, args) as cur:
        return await cur.fetchall()


async def q_one(sql: str, args: tuple = ()):
    async with DB.execute(sql, args) as cur:
        return await cur.fetchone()


async def run(sql: str, args: tuple = ()):
    async with WRITE_LOCK:
        await DB.execute(sql, args)
        await DB.commit()





class Api:
    def __init__(self, session: aiohttp.ClientSession):
        self.s = session
        self._details: dict[int, tuple[float, dict]] = {}
        self._omdb: dict[str, tuple[float, dict]] = {}
        self._providers: Optional[tuple[float, list[tuple[int, str]]]] = None

    async def _get(self, url: str, params: dict) -> dict:
        for attempt in range(3):
            try:
                async with self.s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 429 or r.status >= 500:
                        try:
                            delay = min(float(r.headers.get("Retry-After", 2 ** attempt)), 30)
                        except ValueError:
                            delay = 2 ** attempt
                        await asyncio.sleep(delay)
                        continue
                    if r.status != 200:
                        log.warning("GET %s -> %s", url, r.status)
                        return {}
                    return await r.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("GET %s failed: %s", url, type(e).__name__)
                await asyncio.sleep(2 ** attempt)
        return {}

    async def tmdb(self, path: str, params: Optional[dict] = None) -> dict:
        q = {"api_key": TMDB_KEY, **{k: str(v) for k, v in (params or {}).items()}}
        return await self._get(f"https://api.themoviedb.org/3{path}", q)

    async def details(self, movie_id: int) -> dict:
        hit = self._details.get(movie_id)
        if hit and time.time() - hit[0] < 6 * 3600:
            return hit[1]
        d = await self.tmdb(f"/movie/{movie_id}", {
            "language": "en-US",
            "append_to_response": "credits,videos,watch/providers,release_dates,external_ids,recommendations",
            "include_video_language": "en,null",
        })
        if d.get("id"):
            if len(self._details) >= 512:
                self._details.pop(next(iter(self._details)))
            self._details[movie_id] = (time.time(), d)
        return d

    async def omdb(self, imdb_id: Optional[str]) -> dict:
        if not OMDB_KEY or not imdb_id:
            return {}
        hit = self._omdb.get(imdb_id)
        if hit and time.time() - hit[0] < 24 * 3600:
            return hit[1]
        d = await self._get("https://www.omdbapi.com/", {"apikey": OMDB_KEY, "i": imdb_id})
        if d.get("Response") != "True":
            d = {}
        if len(self._omdb) >= 512:
            self._omdb.pop(next(iter(self._omdb)))
        self._omdb[imdb_id] = (time.time(), d)
        return d

    async def providers(self) -> list[tuple[int, str]]:
        if self._providers and time.time() - self._providers[0] < 24 * 3600:
            return self._providers[1]
        data = await self.tmdb("/watch/providers/movie", {"watch_region": REGION})
        res = data.get("results", [])
        res.sort(key=lambda p: p.get("display_priorities", {}).get(REGION, 999))
        lst = [(p["provider_id"], p["provider_name"]) for p in res]
        if lst:
            self._providers = (time.time(), lst)
        return lst





def analyze(d: dict) -> dict:
    """Get release and streaming details for the selected region."""
    t = today()
    cert, cdate = None, None
    for r in d.get("release_dates", {}).get("results", []):
        if r.get("iso_3166_1") != REGION:
            continue
        for rd in r.get("release_dates", []):
            if rd.get("certification") and not cert:
                cert = rd["certification"]
            if rd.get("type") in (2, 3) and rd.get("release_date"):
                try:
                    dt = date.fromisoformat(rd["release_date"][:10])
                except ValueError:
                    continue
                if cdate is None or dt < cdate:
                    cdate = dt
    status = None
    if cdate:
        if cdate > t:
            status = "soon"
        elif (t - cdate).days <= 90:
            status = "now"

    reg = d.get("watch/providers", {}).get("results", {}).get(REGION, {})
    flat = [(p["provider_id"], p["provider_name"]) for p in reg.get("flatrate", [])]
    return {
        "cert": cert, "cinema_date": cdate, "cinema_status": status,
        "flat": flat,
        "stream": [n for _, n in flat],
        "rent": [p["provider_name"] for p in reg.get("rent", [])],
        "buy": [p["provider_name"] for p in reg.get("buy", [])],
    }


def trailer_url(d: dict) -> Optional[str]:
    vids = [v for v in d.get("videos", {}).get("results", [])
            if v.get("site") == "YouTube" and v.get("type") == "Trailer"]
    vids.sort(key=lambda v: not v.get("official", False))
    return f"https://www.youtube.com/watch?v={vids[0]['key']}" if vids else None


@dataclass
class Card:
    embed: discord.Embed
    links: dict
    cinema: bool
    title: str
    tmdb_id: int


def build_embed(d: dict, o: dict, a: dict, heading: Optional[str]) -> discord.Embed:
    year = year_of(d)
    title = f"{d['title']} ({year})" if year else d["title"]
    desc = trunc(d.get("overview") or "No synopsis available.", 900)
    if d.get("tagline"):
        desc = f"*{d['tagline']}*\n\n{desc}"
    emb = discord.Embed(title=title, url=f"https://www.themoviedb.org/movie/{d['id']}",
                        description=desc, color=discord.Color.gold())
    if heading:
        emb.set_author(name=heading)
    if d.get("poster_path"):
        emb.set_thumbnail(url=f"https://image.tmdb.org/t/p/w342{d['poster_path']}")
    if d.get("backdrop_path"):
        emb.set_image(url=f"https://image.tmdb.org/t/p/w780{d['backdrop_path']}")

    credits = d.get("credits", {})
    directors = [c["name"] for c in credits.get("crew", []) if c.get("job") == "Director"][:2]
    cast = [c["name"] for c in credits.get("cast", [])[:5]]
    genres = [g["name"] for g in d.get("genres", [])]

    emb.add_field(name="Genre", value=", ".join(genres) or "N/A", inline=True)
    emb.add_field(name="Runtime", value=runtime_str(d.get("runtime")), inline=True)
    emb.add_field(name="Director", value=", ".join(directors) or "N/A", inline=True)
    emb.add_field(name="Cast", value=", ".join(cast) or "N/A", inline=False)


    ratings = {r["Source"]: r["Value"] for r in o.get("Ratings", [])}
    parts = []
    if o.get("imdbRating") not in (None, "N/A"):
        votes = f" ({o['imdbVotes']} votes)" if o.get("imdbVotes") not in (None, "N/A") else ""
        parts.append(f"⭐ **IMDb** {o['imdbRating']}/10{votes}")
    if "Rotten Tomatoes" in ratings:
        parts.append(f"🍅 **Rotten Tomatoes** {ratings['Rotten Tomatoes']}")
    if "Metacritic" in ratings:
        parts.append(f"🎯 **Metacritic** {ratings['Metacritic']}")
    if d.get("vote_average"):
        parts.append(f"🎞️ **TMDB** {d['vote_average']:.1f}/10")
    parts.append("📗 **Letterboxd** → use the button below")
    emb.add_field(name="Ratings", value="\n".join(parts), inline=False)

    age = []
    if a["cert"]:
        age.append(f"{REGION}: {a['cert']}")
    if o.get("Rated") not in (None, "N/A", "Not Rated"):
        age.append(f"US: {o['Rated']}")
    if age:
        emb.add_field(name="Age rating", value=" · ".join(age), inline=True)
    if o.get("BoxOffice") not in (None, "N/A"):
        emb.add_field(name="Box office", value=o["BoxOffice"], inline=True)
    if o.get("Awards") not in (None, "N/A"):
        emb.add_field(name="Awards", value=trunc(o["Awards"], 250), inline=False)


    if a["cinema_status"] == "now":
        emb.add_field(name="🎟️ Recent theatrical release",
                      value=f"Opened in {REGION} on {ts(a['cinema_date'])}. Use the menu below to find showtimes.",
                      inline=False)
    elif a["cinema_status"] == "soon":
        emb.add_field(name="🎟️ Coming to cinemas",
                      value=f"Opens in {REGION} on {ts(a['cinema_date'])} ({ts(a['cinema_date'], 'R')}).",
                      inline=False)


    lines = []
    if a["stream"]:
        lines.append("▶️ **Stream:** " + ", ".join(a["stream"][:8]))
    if a["rent"]:
        lines.append("💵 **Rent:** " + ", ".join(a["rent"][:6]))
    if a["buy"]:
        lines.append("🛒 **Buy:** " + ", ".join(a["buy"][:6]))
    if not lines:
        lines.append("No streaming availability found in our data for " + REGION + " right now.")
    emb.add_field(name=f"Where to watch ({REGION})", value="\n".join(lines), inline=False)

    recs = d.get("recommendations", {}).get("results", [])[:4]
    if recs:
        emb.add_field(name="If you like this, try",
                      value=" · ".join(f"{r['title']} ({(r.get('release_date') or '')[:4]})" for r in recs),
                      inline=False)

    emb.set_footer(text="Info: TMDB & OMDb · Streaming data: JustWatch")
    return emb


async def make_card(movie_id: int, heading: Optional[str] = None) -> Optional[Card]:
    d = await bot.api.details(movie_id)
    if not d.get("id"):
        return None
    imdb_id = d.get("external_ids", {}).get("imdb_id") or d.get("imdb_id")
    o = await bot.api.omdb(imdb_id)
    a = analyze(d)
    links = {
        "trailer": trailer_url(d),
        "imdb": f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else None,
        "letterboxd": f"https://letterboxd.com/tmdb/{d['id']}/",
    }
    title = f"{d['title']} ({year_of(d)})" if year_of(d) else d["title"]
    return Card(build_embed(d, o, a, heading), links, a["cinema_status"] is not None, title, d["id"])





def build_params(mode: str = "any", genre=None, decade=None, language=None,
                 max_runtime=None, platform=None, mood=None) -> dict:
    t = today()
    p: dict = {"include_adult": "false", "language": "en-US"}
    if mode == "streaming":
        p.update({"watch_region": REGION, "with_watch_monetization_types": "flatrate",
                  "vote_average.gte": MIN_RATING, "vote_count.gte": MIN_VOTES, "sort_by": "popularity.desc"})
    elif mode == "cinema":

        p.update({"region": REGION, "with_release_type": "2|3",
                  "release_date.gte": str(t - timedelta(days=60)), "release_date.lte": str(t),
                  "vote_average.gte": 6.5, "vote_count.gte": 100, "sort_by": "popularity.desc"})
    elif mode == "hidden":
        p.update({"vote_average.gte": 7.3, "vote_count.gte": 300, "vote_count.lte": 2500,
                  "sort_by": "vote_average.desc"})
    elif mode == "classic":
        p.update({"primary_release_date.lte": "1995-12-31", "vote_average.gte": 7.5,
                  "vote_count.gte": 1500, "sort_by": "vote_average.desc"})
    elif mode == "filipino":
        p.update({"with_origin_country": "PH", "vote_average.gte": 6.5, "vote_count.gte": 50,
                  "sort_by": "vote_count.desc"})
    else:
        p.update({"vote_average.gte": MIN_RATING, "vote_count.gte": MIN_VOTES, "sort_by": "popularity.desc"})

    if mood in MOODS:
        p.update(MOODS[mood][1])
    if genre:
        existing = p.get("with_genres")
        if existing:
            p["_mood_genres"] = existing
        p["with_genres"] = genre
    if decade:
        y = int(decade)
        p["primary_release_date.gte"] = f"{y}-01-01"
        p["primary_release_date.lte"] = f"{y + 9}-12-31"
    if language:
        p["with_original_language"] = language
    if max_runtime:
        p["with_runtime.lte"] = min(max_runtime, p.get("with_runtime.lte", max_runtime))
    if platform:
        p.update({"with_watch_providers": platform, "watch_region": REGION,
                  "with_watch_monetization_types": "flatrate"})
    return p


async def pick_film(guild_id: Optional[int], params: dict, exclude=()) -> Optional[int]:
    seen = set(exclude)
    if guild_id:
        rows = await q_all("SELECT tmdb_id FROM posts WHERE guild_id=? AND kind!='search' AND posted_at>=?", (guild_id, (datetime.now(TZ)-timedelta(days=180)).isoformat()))
        seen |= {r["tmdb_id"] for r in rows}
    params = dict(params)
    mood_genres = {int(x) for x in params.pop("_mood_genres", "").split("|") if x}
    first = await bot.api.tmdb("/discover/movie", {**params, "page": 1})
    if not first.get("results"):
        return None
    total = max(1, min(first.get("total_pages", 1), 20))
    for _ in range(6):
        page = random.randint(1, total)
        data = first if page == 1 else await bot.api.tmdb("/discover/movie", {**params, "page": page})
        pool = [m for m in data.get("results", []) if m["id"] not in seen and m.get("poster_path") and (not mood_genres or mood_genres.intersection(m.get("genre_ids", [])))]
        if pool:
            return random.choice(pool)["id"]
    return None





async def deliver(channel, card: Card, *, guild_id: int, kind: str, content: Optional[str] = None,
                  interaction: Optional[discord.Interaction] = None, thread: bool = True):
    view = FilmView(card.links, card.cinema)
    embed = deepcopy(card.embed)
    if kind != "daily":
        embed.description = trunc(embed.description, 300)
        embed.clear_fields()
        for field in card.embed.fields:
            if field.name.startswith("Where to watch") or field.name in ("Runtime", "Ratings", "Genre"):
                embed.add_field(name=field.name, value=field.value, inline=field.inline)
        embed.set_image(url=None)
    if interaction:
        msg = await interaction.followup.send(embed=embed, view=view, wait=True)
    else:
        msg = await channel.send(content=content, embed=embed, view=view,
                                 allowed_mentions=discord.AllowedMentions(roles=True))
    await run("INSERT OR REPLACE INTO posts VALUES(?,?,?,?,?,?,?,?,?)",
              (msg.id, guild_id, channel.id, card.tmdb_id, card.title,
               json.dumps(card.links), int(card.cinema), kind, now_iso()))
    if thread and isinstance(channel, discord.TextChannel):
        try:
            await channel.get_partial_message(msg.id).create_thread(
                name=trunc(f"💬 {card.title}", 100), auto_archive_duration=1440)
        except discord.HTTPException as e:
            log.info("Could not create thread: %s", e)
    return msg


async def post_daily(guild_id: int, channel_id: int, role_id: Optional[int], force_mode: Optional[str] = None) -> bool:
    channel = bot.get_channel(channel_id)
    if channel is None:
        return False
    mode = force_mode or ROTATION[today().weekday()]
    card = None
    for m in dict.fromkeys([mode, "streaming", "any"]):
        theme = await q_one("SELECT * FROM themes WHERE guild_id=?", (guild_id,))
        mid = await pick_film(guild_id, build_params(m, genre=theme["genre"] if theme else None))
        if mid:
            card = await make_card(mid, heading=f"🎬 Daily Film Pick · {theme['label'] if theme else MODE_LABELS[m]}")
            if card:
                break
    if not card:
        return False
    ping = f"<@&{role_id}> " if role_id else ""
    await deliver(channel, card, guild_id=guild_id, kind="daily",
                  content=f"{ping}**Today's film: {card.title}**")
    return True





async def get_post(message_id: int):
    return await q_one("SELECT * FROM posts WHERE message_id=?", (message_id,))


async def vote_counts(message_id: int) -> dict:
    post = await get_post(message_id)
    if not post:
        return {}
    args = (post["guild_id"], post["tmdb_id"])
    rows = await q_all("SELECT vote, COUNT(*) n FROM ratings WHERE guild_id=? AND tmdb_id=? GROUP BY vote", args)
    counts = {r["vote"]: r["n"] for r in rows}
    counts["seen"] = (await q_one("SELECT COUNT(*) n FROM watched WHERE guild_id=? AND tmdb_id=?", args))["n"]
    return counts


def _vote_cb(key: str):
    async def cb(interaction: discord.Interaction):
        await interaction.response.defer()
        async with RATING_LOCK:
            row = await get_post(interaction.message.id)
            if not row:
                return await interaction.followup.send("This post is no longer in my records.", ephemeral=True)
            args = (row["guild_id"], interaction.user.id, row["tmdb_id"])
            table = "watched" if key == "seen" else "ratings"
            existing = await q_one(f"SELECT * FROM {table} WHERE guild_id=? AND user_id=? AND tmdb_id=?", args)
            if existing and (key == "seen" or existing["vote"] == key):
                await run(f"DELETE FROM {table} WHERE guild_id=? AND user_id=? AND tmdb_id=?", args)
            elif key == "seen":
                await run("INSERT INTO watched VALUES(?,?,?,?)", (*args, now_iso()))
            else:
                await run("INSERT OR REPLACE INTO ratings VALUES(?,?,?,?,?)", (*args, key, now_iso()))
            await interaction.edit_original_response(view=FilmView(json.loads(row["links"]), bool(row["cinema"]), await vote_counts(row["message_id"])))
    return cb


async def mark_known_state(guild_id: int, tmdb_id: int):
    """Remember alerts that were already true when a film was added."""
    a = analyze(await bot.api.details(tmdb_id))
    if a["cinema_date"] and a["cinema_date"] <= today():
        await run("INSERT OR IGNORE INTO notified VALUES(?,?,?)", (guild_id, tmdb_id, "cinema"))
    if a["flat"]:
        await run("INSERT OR IGNORE INTO notified VALUES(?,?,?)", (guild_id, tmdb_id, "stream"))


async def add_watchlist(guild_id: int, user_id: int, tmdb_id: int, title: str) -> bool:
    existing = await q_one("SELECT 1 FROM watchlist WHERE guild_id=? AND user_id=? AND tmdb_id=?",
                           (guild_id, user_id, tmdb_id))
    if existing:
        return False
    await run("INSERT INTO watchlist VALUES(?,?,?,?,?)", (guild_id, user_id, tmdb_id, title, now_iso()))
    await mark_known_state(guild_id, tmdb_id)
    return True


async def _watch_cb(interaction: discord.Interaction):
    row = await get_post(interaction.message.id)
    if not row:
        return await interaction.response.send_message("I don't have this post in my records.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    added = await add_watchlist(row["guild_id"], interaction.user.id, row["tmdb_id"], row["title"])
    await interaction.followup.send(
        f"➕ Added **{row['title']}** to the server watchlist." if added
        else "It's already on your watchlist.", ephemeral=True)


async def _can_watch_cb(interaction: discord.Interaction):
    row = await get_post(interaction.message.id)
    if not row:
        return await interaction.response.send_message("I don't have this post in my records.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    a = analyze(await bot.api.details(row["tmdb_id"]))
    svc = await q_one("SELECT provider_ids FROM services WHERE user_id=?", (interaction.user.id,))
    if not svc:
        where = ", ".join(a["stream"]) or "no streaming service"
        return await interaction.followup.send(
            f"Set the services you subscribe to with `/myservices` and I'll tell you.\n"
            f"For now: it's on **{where}** in {REGION}.", ephemeral=True)
    mine = set(json.loads(svc["provider_ids"]))
    hits = [n for pid, n in a["flat"] if pid in mine]
    if hits:
        msg = f"✅ Yes. You can stream it on **{', '.join(hits)}**."
    elif a["stream"]:
        msg = f"❌ Not on your services. It's on **{', '.join(a['stream'])}**."
    elif a["cinema_status"]:
        msg = "🎟️ Not streaming yet, but it's in (or coming to) cinemas."
    else:
        msg = "No streaming availability found in our data for " + REGION + " right now."
    await interaction.followup.send(msg, ephemeral=True)


async def _cinema_cb(interaction: discord.Interaction):
    row = await get_post(interaction.message.id)
    if not row:
        return await interaction.response.send_message("I don't have this post in my records.", ephemeral=True)
    chain = interaction.data["values"][0]
    q = quote_plus(f"{row['title']} showtimes {chain} Philippines")
    await interaction.response.send_message(
        f"🎟️ Showtimes for **{row['title']}** at **{chain}**:\nhttps://www.google.com/search?q={q}\n"
        f"*(Cinema chains don't publish a showtimes API, so this opens a live search.)*", ephemeral=True)


async def _details_cb(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    row = await get_post(interaction.message.id)
    card = await make_card(row['tmdb_id']) if row else None
    await interaction.followup.send(embed=card.embed, ephemeral=True) if card else await interaction.followup.send("Details unavailable. Try again later.", ephemeral=True)


def _discover_cb(similar):
    async def callback(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        row = await get_post(interaction.message.id)
        if not row:
            return await interaction.followup.send("This film is no longer in my records.", ephemeral=True)
        d = await bot.api.details(row['tmdb_id'])
        genres = [str(g['id']) for g in d.get('genres', [])]
        params = build_params('any', genre=genres[0] if similar and genres else None)
        if not similar and genres:
            params['without_genres'] = ','.join(genres)
        watched = await q_all("SELECT tmdb_id FROM watched WHERE guild_id=? AND user_id=?", (interaction.guild_id, interaction.user.id))
        mid = await pick_film(interaction.guild_id, params, exclude=[row['tmdb_id'], *[r['tmdb_id'] for r in watched]])
        card = await make_card(mid, heading='A similar genre' if similar else 'A different genre') if mid else None
        if card:
            await interaction.followup.send(embed=card.embed, ephemeral=True)
        else:
            await interaction.followup.send("No matching unseen film found. Try /film with broader filters.", ephemeral=True)
    return callback


class ServerOnlyView(discord.ui.View):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if ALLOWED_GUILD_ID and interaction.guild_id != int(ALLOWED_GUILD_ID):
            await interaction.response.send_message("This bot is reserved for its home server.", ephemeral=True)
            return False
        return True


class FilmView(ServerOnlyView):
    """Build the buttons shown under a movie post."""

    def __init__(self, links: Optional[dict] = None, cinema: bool = False, counts: Optional[dict] = None):
        super().__init__(timeout=None)
        counts = counts or {}
        for key, (emoji, label) in VOTES.items():
            n = counts.get(key, 0)
            b = discord.ui.Button(style=discord.ButtonStyle.secondary, emoji=emoji,
                                  label=f"{label} · {n}" if n else label,
                                  custom_id=f"film:vote:{key}", row=0)
            b.callback = _vote_cb(key)
            self.add_item(b)
        if links:
            for label, key, emoji in (("Trailer", "trailer", "▶️"), ("IMDb", "imdb", "⭐"),
                                      ("Letterboxd", "letterboxd", "📗")):
                if links.get(key):
                    self.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label=label,
                                                    emoji=emoji, url=links[key], row=1))
        w = discord.ui.Button(style=discord.ButtonStyle.primary, label="Watchlist", emoji="➕",
                              custom_id="film:watch", row=1)
        w.callback = _watch_cb
        self.add_item(w)
        c = discord.ui.Button(style=discord.ButtonStyle.success, label="Can I watch?", emoji="📺",
                              custom_id="film:can", row=1)
        c.callback = _can_watch_cb
        self.add_item(c)
        for label, custom_id, callback in (("Details", "film:details", _details_cb),
                ("More like this", "film:similar", _discover_cb(True)),
                ("Different genre", "film:different", _discover_cb(False))):
            button = discord.ui.Button(label=label, custom_id=custom_id, row=3)
            button.callback = callback
            self.add_item(button)
        if links is None or cinema:
            sel = discord.ui.Select(custom_id="film:cinema", row=2,
                                    placeholder="🎟️ Find showtimes: pick a cinema",
                                    options=[discord.SelectOption(label=n, value=n) for n in CINEMAS])
            sel.callback = _cinema_cb
            self.add_item(sel)


class RoleView(ServerOnlyView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Toggle daily film pings", emoji="🔔", style=discord.ButtonStyle.primary,
                       custom_id="role:toggle")
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = await q_one("SELECT role_id FROM settings WHERE guild_id=?", (interaction.guild_id,))
        role = interaction.guild.get_role(s["role_id"]) if s and s["role_id"] else None
        if not role:
            return await interaction.response.send_message(
                "No ping role is set yet. An admin can set one with `/setup`.", ephemeral=True)
        try:
            if role in interaction.user.roles:
                await interaction.user.remove_roles(role)
                msg = f"🔕 You'll no longer be pinged ({role.name} removed)."
            else:
                await interaction.user.add_roles(role)
                msg = f"🔔 You'll be pinged for the daily film ({role.name} added)."
        except discord.Forbidden:
            msg = "I can't manage that role. Move my bot role above it and give me **Manage Roles**."
        await interaction.response.send_message(msg, ephemeral=True)



def poll_embed(options: list[dict], counts: dict) -> discord.Embed:
    total = sum(counts.values())
    lines = []
    for i, o in enumerate(options):
        n = counts.get(i, 0)
        bar = "▰" * n + "▱" * max(0, 8 - n) if n <= 8 else "▰" * 8 + f" +{n - 8}"
        lines.append(f"**{i + 1}.** {o['line']}\n{bar} {n} vote{'s' if n != 1 else ''}")
    emb = discord.Embed(title="🍿 Movie Night Poll", description="\n\n".join(lines), color=discord.Color.purple())
    emb.set_footer(text=f"{total} vote(s) · click a number to vote, click again to undo")
    return emb


def _poll_cb(idx: int):
    async def cb(interaction: discord.Interaction):
        return await interaction.response.send_message("This legacy poll is archived. Use /movienight create for a timed movie night.", ephemeral=True)
    return cb


class PollView(ServerOnlyView):
    def __init__(self, n: int = 5, counts: Optional[dict] = None):
        super().__init__(timeout=None)
        counts = counts or {}
        for i in range(n):
            c = counts.get(i, 0)
            b = discord.ui.Button(style=discord.ButtonStyle.primary, label=f"{i + 1} · {c}" if c else str(i + 1),
                                  custom_id=f"poll:{i}")
            b.callback = _poll_cb(i)
            self.add_item(b)





class TeatroTree(app_commands.CommandTree):
    async def interaction_check(self, interaction):
        if ALLOWED_GUILD_ID and interaction.guild_id != int(ALLOWED_GUILD_ID):
            await interaction.response.send_message("This bot is reserved for its home server.", ephemeral=True)
            return False
        return True


class FilmBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default(), tree_cls=TeatroTree, allowed_mentions=discord.AllowedMentions.none())
        self.api: Api

    async def setup_hook(self):
        global DB
        self.api = Api(aiohttp.ClientSession())
        DB = await aiosqlite.connect(DB_PATH)
        DB.row_factory = aiosqlite.Row
        await DB.execute("PRAGMA journal_mode=WAL")
        await DB.execute("PRAGMA busy_timeout=5000")
        await DB.executescript(SCHEMA)
        await DB.commit()

        from teatro_workflows import install
        await install(self, __import__(__name__))
        from teatro_members import install as install_members
        await install_members(self, __import__(__name__))
        self.add_view(FilmView())
        self.add_view(RoleView())
        self.add_view(PollView())

        if DEV_GUILD_ID:
            g = discord.Object(id=int(DEV_GUILD_ID))
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
        else:
            await self.tree.sync()
        daily_job.start()

    async def close(self):
        daily_job.cancel()
        if hasattr(self, "workflow_task"):
            self.workflow_task.cancel()
            try:
                await self.workflow_task
            except asyncio.CancelledError:
                pass
        await self.api.s.close()
        await DB.close()
        await super().close()


bot = FilmBot()


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s guilds)", bot.user, len(bot.guilds))


@bot.tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.exception("Command error", exc_info=error)
    msg = "Something went wrong. Please try again in a moment."
    if isinstance(error, app_commands.CommandOnCooldown):
        msg = f"Try again in {error.retry_after:.0f} seconds."
    if isinstance(error, app_commands.MissingPermissions):
        msg = "You need the **Manage Server** permission for that."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)





async def title_autocomplete(interaction: discord.Interaction, current: str):
    if len(current) < 2:
        return []
    data = await bot.api.tmdb("/search/movie", {"query": current, "include_adult": "false"})
    out = []
    for m in data.get("results", [])[:10]:
        y = (m.get("release_date") or "")[:4]
        out.append(app_commands.Choice(name=trunc(f"{m['title']} ({y})" if y else m["title"], 100),
                                       value=f"id:{m['id']}"))
    return out


async def resolve_movie_id(query: str) -> Optional[int]:
    if query.startswith("id:") and query[3:].isdigit():
        return int(query[3:])
    match = re.fullmatch(r"(.+?)\s*\(?((?:19|20)\d{2})\)?", query.strip())
    title, year = (match.group(1).strip(), match.group(2)) if match else (query.strip(), None)
    data = await bot.api.tmdb("/search/movie", {"query": title, "include_adult": "false"})
    results = data.get("results", [])
    if year:
        results = [m for m in results if (m.get("release_date") or "")[:4] == year]
    exact = [m for m in results if m.get("title", "").casefold() == title.casefold()]
    return (exact or results)[0]["id"] if results else None


async def platform_autocomplete(interaction: discord.Interaction, current: str):
    provs = await bot.api.providers()
    cur = current.lower()
    return [app_commands.Choice(name=n[:100], value=str(pid)) for pid, n in provs if cur in n.lower()][:25]


GENRE_CHOICES = [app_commands.Choice(name=n, value=str(i)) for n, i in GENRES.items()]
LANG_CHOICES = [app_commands.Choice(name=n, value=c) for n, c in LANGS.items()]
MOOD_CHOICES = [app_commands.Choice(name=v[0], value=k) for k, v in MOODS.items()]
DECADE_CHOICES = [app_commands.Choice(name=f"{y}s", value=str(y)) for y in range(1950, 2030, 10)]
MODE_CHOICES = [app_commands.Choice(name=v, value=k) for k, v in MODE_LABELS.items()]





@bot.tree.command(name="setup", description="Set the channel (and optional ping role) for the daily film")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(channel="Where the daily film is posted", role="Role pinged for the daily film (optional)")
async def setup_cmd(interaction: discord.Interaction, channel: discord.TextChannel, role: Optional[discord.Role] = None):
    permissions = channel.permissions_for(interaction.guild.me)
    if not permissions.send_messages or not permissions.embed_links or not permissions.view_channel:
        return await interaction.response.send_message("I need View Channel, Send Messages and Embed Links in that channel.", ephemeral=True)
    if role and (role.is_default() or role.managed or role >= interaction.guild.me.top_role or not interaction.guild.me.guild_permissions.manage_roles):
        return await interaction.response.send_message("Choose an assignable role below my highest role and grant Manage Roles.", ephemeral=True)
    await run("INSERT INTO settings VALUES(?,?,?) ON CONFLICT(guild_id) DO UPDATE SET "
              "channel_id=excluded.channel_id, role_id=excluded.role_id",
              (interaction.guild_id, channel.id, role.id if role else None))
    await interaction.response.send_message(
        f"✅ Daily film will post in {channel.mention} at **{POST_HOUR:02d}:{POST_MIN:02d}** ({TZ.key})."
        + (f" Pinging {role.mention}." if role else " No ping role set.")
        + "\nUse `/rolepanel` to post a button members can click to opt in to pings.", ephemeral=True)


@bot.tree.command(name="dailynow", description="Post the daily film right now (admin)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def dailynow(interaction: discord.Interaction):
    s = await q_one("SELECT * FROM settings WHERE guild_id=?", (interaction.guild_id,))
    if not s:
        return await interaction.response.send_message("Run `/setup` first.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    async with DAILY_LOCK:
        if await q_one("SELECT 1 FROM job_runs WHERE guild_id=? AND day=?", (interaction.guild_id, str(today()))):
            return await interaction.followup.send("Today’s daily pick has already been posted.", ephemeral=True)
        ok = await post_daily(interaction.guild_id, s["channel_id"], s["role_id"])
        if ok:
            await run("INSERT OR IGNORE INTO job_runs VALUES(?,?)", (interaction.guild_id, str(today())))
    await interaction.followup.send("✅ Posted." if ok else "⚠️ Couldn't find a film or post it. Check my channel permissions.", ephemeral=True)


@bot.tree.command(name="rolepanel", description="Post a button members can click to get daily film pings (admin)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def rolepanel(interaction: discord.Interaction):
    emb = discord.Embed(title="🎬 Daily Film Pings",
                        description="Click the button to get (or drop) the role that's pinged for the daily film.",
                        color=discord.Color.gold())
    await interaction.response.send_message(embed=emb, view=RoleView())





@bot.tree.command(name="film", description="Get a good film recommendation")
@app_commands.checks.cooldown(1, 10, key=lambda i: (i.guild_id, i.user.id))
@app_commands.guild_only()
@app_commands.describe(mode="Type of pick", genre="Genre", decade="Decade", language="Original language",
                       max_runtime="Maximum runtime in minutes", platform="Only films on this streaming service",
                       mood="What are you in the mood for?")
@app_commands.choices(mode=MODE_CHOICES, genre=GENRE_CHOICES, decade=DECADE_CHOICES,
                      language=LANG_CHOICES, mood=MOOD_CHOICES)
async def film_cmd(interaction: discord.Interaction, mode: Optional[str] = "any", genre: Optional[str] = None,
                   decade: Optional[str] = None, language: Optional[str] = None,
                   max_runtime: Optional[app_commands.Range[int, 60, 240]] = None,
                   platform: Optional[str] = None, mood: Optional[str] = None):
    await interaction.response.defer()
    mode = mode or "any"
    pref = await q_one("SELECT * FROM preferences WHERE guild_id=? AND user_id=?", (interaction.guild_id, interaction.user.id))
    if pref:
        genre, language, max_runtime = genre or pref['genres'], language or pref['language'], max_runtime or pref['max_runtime']
    watched = await q_all("SELECT tmdb_id FROM watched WHERE guild_id=? AND user_id=?", (interaction.guild_id, interaction.user.id))
    mid = await pick_film(interaction.guild_id, build_params(mode, genre, decade, language, max_runtime, platform, mood), exclude=[r["tmdb_id"] for r in watched])
    if not mid:
        return await interaction.followup.send("No new film matches those filters. Try loosening them a bit.")
    card = await make_card(mid, heading=f"🎬 {MODE_LABELS.get(mode, 'Pick')} · for {interaction.user.display_name}")
    if not card:
        return await interaction.followup.send("Couldn't load that film. Please try again.")
    reasons = [MODE_LABELS.get(mode, 'Pick')]
    if max_runtime: reasons.append(f"under {max_runtime} minutes")
    if language: reasons.append(f"language: {language}")
    if genre: reasons.append(next((name for name, gid in GENRES.items() if str(gid) == genre), 'selected genre'))
    card.embed.description = "**Why this pick:** " + "; ".join(reasons) + "\n\n" + (card.embed.description or '')
    await deliver(interaction.channel, card, guild_id=interaction.guild_id, kind="manual", interaction=interaction, thread=False)


film_cmd.autocomplete("platform")(platform_autocomplete)


@bot.tree.command(name="search", description="Look up any film and get its full card")
@app_commands.guild_only()
@app_commands.describe(title="Film title")
async def search_cmd(interaction: discord.Interaction, title: str):
    await interaction.response.defer()
    mid = await resolve_movie_id(title)
    card = await make_card(mid, heading="🔎 Film lookup") if mid else None
    if not card:
        return await interaction.followup.send(f"I couldn't find “{title}”.")
    await deliver(interaction.channel, card, guild_id=interaction.guild_id, kind="search",
                  interaction=interaction, thread=False)


search_cmd.autocomplete("title")(title_autocomplete)


def theatrical_dates(movie: dict) -> list[date]:
    """Get the movie's theatrical dates for the selected region."""
    dates = set()
    for region in movie.get("release_dates", {}).get("results", []):
        if region.get("iso_3166_1") != REGION:
            continue
        for release in region.get("release_dates", []):
            if release.get("type") not in (2, 3):
                continue
            try:
                dates.add(date.fromisoformat((release.get("release_date") or "")[:10]))
            except ValueError:
                continue
    return sorted(dates)


async def cinema_embed(title: str, gte: date, lte: date, by_date: bool) -> discord.Embed:
    params = {"region": REGION, "with_release_type": "2|3", "release_date.gte": str(gte),
              "release_date.lte": str(lte), "include_adult": "false", "sort_by": "popularity.desc"}
    if not by_date:
        params.update({"vote_count.gte": 50, "vote_average.gte": 6.0})
    res = (await bot.api.tmdb("/discover/movie", params)).get("results", [])[:12]
    dets = await asyncio.gather(*(bot.api.details(m["id"]) for m in res))
    items = []
    for d in dets:
        if not d.get("id"):
            continue
        dates = theatrical_dates(d)
        matching = [release for release in dates if gte <= release <= lte]
        if not matching:
            continue
        selected = matching[0]
        items.append((d, {"cinema_date": selected, "rerelease": dates[0] < selected}))
    if by_date:
        items.sort(key=lambda x: x[1]["cinema_date"])
    else:
        items.sort(key=lambda x: x[0].get("vote_average", 0), reverse=True)
    lines = []
    for d, a in items[:10]:
        rating = f"⭐ {d['vote_average']:.1f} · " if d.get("vote_average") and d.get("vote_count", 0) >= 50 else ""
        release = a["cinema_date"]
        label = "re-release" if a["rerelease"] else "opens" if release >= today() else "opened"


        when = f"{label} {release:%d %b %Y}"
        genres = ", ".join(g["name"] for g in d.get("genres", [])[:2])
        lines.append(f"**[{d['title']}](https://www.themoviedb.org/movie/{d['id']})** · {rating}{genres}\n{when}")
    emb = discord.Embed(title=title, description="\n\n".join(lines) or "Nothing found for this window.",
                        color=discord.Color.red())
    emb.set_footer(text=f"TMDB · {REGION} · {gte:%d %b %Y}–{lte:%d %b %Y} · Today uses {TZ.key}")
    return emb


@bot.tree.command(name="nowplaying", description="Good films currently showing in cinemas")
@app_commands.guild_only()
async def nowplaying(interaction: discord.Interaction):
    await interaction.response.defer()
    t = today()
    await interaction.followup.send(embed=await cinema_embed(f"🎟️ Recent theatrical releases in {REGION}",
                                                             t - timedelta(days=45), t, by_date=False))


def upcoming_release(movie: dict, start: date, end: date):
    """Get release details for a new movie in the given date range."""
    if movie.get('status') == 'Released' or movie.get('adult'):
        return None
    try:
        primary = date.fromisoformat(movie.get('release_date') or '')
    except ValueError:
        return None
    if not start <= primary <= end:
        return None

    for region in movie.get('release_dates', {}).get('results', []):
        for release in region.get('release_dates', []):
            if release.get('type') not in (2, 3):
                continue
            try:
                if date.fromisoformat((release.get('release_date') or '')[:10]) < start:
                    return None
            except ValueError:
                continue
    local = [d for d in theatrical_dates(movie) if start <= d <= end]
    return (local[0], f'{REGION} theatrical date') if local else (primary, f'Listed release · {REGION} date unconfirmed')


async def upcoming_embed(start: date, end: date, page: int, sort: str) -> discord.Embed:
    data = await bot.api.tmdb('/discover/movie', {
        'primary_release_date.gte': str(start), 'primary_release_date.lte': str(end),
        'include_adult': 'false', 'sort_by': sort, 'page': page,
    })
    details = await asyncio.gather(*(bot.api.details(m['id']) for m in data.get('results', [])[:20]))
    lines = []
    for movie in details:
        result = upcoming_release(movie, start, end)
        if not result or not movie.get('id'):
            continue
        release, label = result
        title = discord.utils.escape_markdown(trunc(movie.get('title'), 65))
        lines.append(f"**[{title}](https://www.themoviedb.org/movie/{movie['id']})**\n{release:%d %b %Y} · {label}")
    embed = discord.Embed(title='📅 Upcoming new movies', color=discord.Color.red(),
        description='\n\n'.join(lines) or 'No unreleased films with confirmed listed dates on this page. Try another page or a longer window.')
    embed.set_footer(text=f"TMDB · Page {page}/{max(1, data.get('total_pages', 1))} · {start:%d %b %Y}–{end:%d %b %Y} · {TZ.key} · Dates may change")
    return embed


def regional_future_screening(movie: dict, start: date, end: date):
    """Find a future local cinema date for an older movie."""
    if movie.get('adult'):
        return None
    try:
        original = date.fromisoformat(movie.get('release_date') or '')
    except ValueError:
        return None
    if original >= start:
        return None
    all_dates = theatrical_dates(movie)
    dates = [day for day in all_dates if start <= day <= end]
    if not dates:
        return None
    kind = 'Re-release' if all_dates[0] < start else f'First {REGION} theatrical listing'
    return min(dates), kind


async def rereleases_embed(start: date, end: date, page: int):
    data = await bot.api.tmdb('/discover/movie', {
        'region': REGION, 'with_release_type': '2|3',
        'release_date.gte': str(start), 'release_date.lte': str(end),
        'primary_release_date.lte': str(start - timedelta(days=1)),
        'include_adult': 'false', 'sort_by': 'popularity.desc', 'page': page,
    })
    details = await asyncio.gather(*(bot.api.details(m['id']) for m in data.get('results', [])[:20]))
    rows = []
    for movie in details:
        screening = regional_future_screening(movie, start, end)
        if screening and movie.get('id'):
            release, kind = screening
            title = discord.utils.escape_markdown(trunc(movie.get('title'), 65))
            rows.append((release, f"**{title}** · {release:%d %b %Y} · {kind}\n[TMDB release details](https://www.themoviedb.org/movie/{movie['id']}/releases)"))
    if not rows:
        return None
    rows.sort(key=lambda row: row[0])
    embed = discord.Embed(title=f'🎞️ Additional theatrical listings for {REGION}', color=discord.Color.orange(),
        description='Future regional theatrical dates recorded by TMDB. Check the cinema for confirmed screenings.\n\n' + '\n\n'.join(row[1] for row in rows[:15]))
    embed.set_footer(text=f'TMDB regional theatrical records · Page {page} · Check the cinema before planning a visit')
    return embed


@bot.tree.command(name="comingsoon", description="Upcoming new films and separately labeled regional theatrical listings")
@app_commands.guild_only()
@app_commands.describe(months="Look ahead 1–60 months (default: 24)", page="Browse more results", sort="Popular upcoming films or soonest releases")
@app_commands.choices(sort=[app_commands.Choice(name='Most anticipated', value='popularity.desc'),
                           app_commands.Choice(name='Soonest releases', value='primary_release_date.asc')])
async def comingsoon(interaction: discord.Interaction, months: app_commands.Range[int, 1, 60] = 24,
                     page: app_commands.Range[int, 1, 500] = 1, sort: str = 'popularity.desc'):
    await interaction.response.defer()
    start = today()
    import calendar
    total = start.year * 12 + start.month - 1 + months
    year, month = divmod(total, 12)
    end = date(year, month + 1, min(start.day, calendar.monthrange(year, month + 1)[1]))
    await interaction.followup.send(embed=await upcoming_embed(start, end, page, sort))
    rereleases = await rereleases_embed(start, end, page)
    if rereleases:
        await interaction.followup.send(embed=rereleases)


@bot.tree.command(name="person", description="Top films from a director or actor")
@app_commands.guild_only()
@app_commands.describe(name="Director or actor name")
async def person_cmd(interaction: discord.Interaction, name: str):
    await interaction.response.defer()
    res = (await bot.api.tmdb("/search/person", {"query": name})).get("results", [])
    if not res:
        return await interaction.followup.send(f"I couldn't find anyone called “{name}”.")
    p = res[0]
    credits = await bot.api.tmdb(f"/person/{p['id']}/movie_credits")
    if p.get("known_for_department") == "Directing":
        films, role = [c for c in credits.get("crew", []) if c.get("job") == "Director"], "Director"
    else:
        films, role = credits.get("cast", []), "Actor"
    uniq = {f["id"]: f for f in films if f.get("vote_count", 0) >= 500 and f.get("release_date")}
    top = sorted(uniq.values(), key=lambda f: f.get("vote_average", 0), reverse=True)[:10]
    lines = [f"**{f['title']}** ({f['release_date'][:4]}) · ⭐ {f['vote_average']:.1f}" for f in top]
    emb = discord.Embed(title=f"{p['name']} · top films ({role})",
                        description="\n".join(lines) or "Not enough rated films found.", color=discord.Color.blue(),
                        url=f"https://www.themoviedb.org/person/{p['id']}")
    if p.get("profile_path"):
        emb.set_thumbnail(url=f"https://image.tmdb.org/t/p/w185{p['profile_path']}")
    emb.set_footer(text="Use /search to get the full card for any of these")
    await interaction.followup.send(embed=emb)



watchlist = app_commands.Group(name="watchlist", description="The server's shared watchlist", guild_only=True)


@watchlist.command(name="add", description="Add a film to the server watchlist")
@app_commands.describe(title="Film title")
async def wl_add(interaction: discord.Interaction, title: str):
    await interaction.response.defer(ephemeral=True)
    mid = await resolve_movie_id(title)
    d = await bot.api.details(mid) if mid else {}
    if not d.get("id"):
        return await interaction.followup.send(f"I couldn't find “{title}”.", ephemeral=True)
    full = f"{d['title']} ({year_of(d)})" if year_of(d) else d["title"]
    added = await add_watchlist(interaction.guild_id, interaction.user.id, d["id"], full)
    await interaction.followup.send(f"➕ Added **{full}**." if added else "Already on your watchlist.", ephemeral=True)


@watchlist.command(name="view", description="See the server watchlist (or one member's)")
@app_commands.describe(member="Only show this member's list")
async def wl_view(interaction: discord.Interaction, member: Optional[discord.Member] = None, page: app_commands.Range[int, 1, 10000] = 1):
    if member:
        rows = await q_all("SELECT tmdb_id, title, 1 n, CAST(user_id AS TEXT) users FROM watchlist "
                           "WHERE guild_id=? AND user_id=? ORDER BY added_at DESC, tmdb_id LIMIT 15 OFFSET ?",
                           (interaction.guild_id, member.id, (page-1)*15))
    else:
        rows = await q_all("SELECT tmdb_id, title, COUNT(*) n, GROUP_CONCAT(user_id) users FROM watchlist "
                           "WHERE guild_id=? GROUP BY tmdb_id ORDER BY n DESC, title LIMIT 15 OFFSET ?", (interaction.guild_id, (page-1)*15))
    if not rows:
        return await interaction.response.send_message("The watchlist is empty. Use `/watchlist add` or the ➕ button on any film.", ephemeral=True)
    lines = []
    for r in rows:
        who = " ".join(f"<@{u}>" for u in r["users"].split(",")[:5])
        lines.append(f"**{trunc(r['title'], 100)}**" + (f" · {r['n']} want to watch: {who}" if not member else ""))
    emb = discord.Embed(title=f"📋 {member.display_name}'s watchlist" if member else "📋 Server watchlist",
                        description="\n".join(lines), color=discord.Color.teal())
    emb.set_footer(text=f"Page {page} · use the page option to browse")
    await interaction.response.send_message(embed=emb)


@watchlist.command(name="remove", description="Remove a film from your watchlist")
@app_commands.describe(title="Film to remove")
async def wl_remove(interaction: discord.Interaction, title: str):
    tid = int(title[3:]) if title.startswith("id:") and title[3:].isdigit() else None
    if tid:
        await run("DELETE FROM watchlist WHERE guild_id=? AND user_id=? AND tmdb_id=?",
                  (interaction.guild_id, interaction.user.id, tid))
    else:
        return await interaction.response.send_message("Choose an exact film from autocomplete to remove it.", ephemeral=True)
    await interaction.response.send_message("🗑️ Removed (if it was on your list).", ephemeral=True)


@wl_remove.autocomplete("title")
async def wl_remove_ac(interaction: discord.Interaction, current: str):
    rows = await q_all("SELECT tmdb_id, title FROM watchlist WHERE guild_id=? AND user_id=? AND title LIKE ? LIMIT 25",
                       (interaction.guild_id, interaction.user.id, f"%{current}%"))
    return [app_commands.Choice(name=trunc(r["title"], 100), value=f"id:{r['tmdb_id']}") for r in rows]


wl_add.autocomplete("title")(title_autocomplete)
bot.tree.add_command(watchlist)



class ServicesView(ServerOnlyView):
    def __init__(self, providers: list[tuple[int, str]], current: set[int]):
        super().__init__(timeout=300)
        self.visible = {pid for pid, _ in providers[:25]}
        self.current = current
        opts = [discord.SelectOption(label=n[:100], value=str(pid), default=pid in current)
                for pid, n in providers[:25]]
        sel = discord.ui.Select(placeholder="Pick the services you subscribe to", min_values=0,
                                max_values=len(opts), options=opts)
        sel.callback = self.on_pick
        self.sel = sel
        self.add_item(sel)

    async def on_pick(self, interaction: discord.Interaction):
        ids = (self.current - self.visible) | {int(v) for v in self.sel.values}
        await run("INSERT OR REPLACE INTO services VALUES(?,?)", (interaction.user.id, json.dumps(sorted(ids))))
        await interaction.response.edit_message(
            content=f"✅ Saved {len(ids)} service(s). The **Can I watch?** button will use them.", view=None)


@bot.tree.command(name="myservices", description="Tell me which streaming services you have")
@app_commands.describe(search="Search a streaming service, such as Disney or Max; repeat to add more")
async def myservices(interaction: discord.Interaction, search: Optional[str] = None):
    provs = await bot.api.providers()
    if not provs:
        return await interaction.response.send_message("Couldn't load the streaming service list. Try again later.", ephemeral=True)
    row = await q_one("SELECT provider_ids FROM services WHERE user_id=?", (interaction.user.id,))
    cur = set(json.loads(row["provider_ids"])) if row else set()
    if search:
        matches = [(pid, name) for pid, name in provs if search.casefold() in name.casefold()]
        if not matches:
            return await interaction.response.send_message(
                f"No movie provider matching **{trunc(search, 40)}** was returned for {REGION}. Try another name, such as Max, HBO, or Disney.", ephemeral=True)
        provs = matches
    await interaction.response.send_message(
        "Select the services you use, then submit. Use `/myservices search:Disney` or `/myservices search:Max` for providers beyond the first page; earlier selections stay saved.",
        view=ServicesView(provs, cur), ephemeral=True)




async def leaderboard_rows(guild_id: int, start: str, end: str, limit: int = 10):
    return await q_all(
        "SELECT v.tmdb_id, (SELECT title FROM posts p WHERE p.tmdb_id=v.tmdb_id LIMIT 1) AS title, "
        "SUM(vote='up') ups, SUM(vote='meh') mehs, SUM(vote='down') downs, SUM(vote='seen') seen "
        "FROM ratings v WHERE guild_id=? AND created_at>=? AND created_at<? GROUP BY v.tmdb_id "
        "HAVING ups+mehs+downs>0 ORDER BY (ups-downs) DESC, ups DESC LIMIT ?",
        (guild_id, start, end, limit))


def lb_lines(rows) -> str:
    out = []
    for i, r in enumerate(rows, 1):
        out.append(f"**{i}. {r['title']}**: 👍 {r['ups']} · 😐 {r['mehs']} · 👎 {r['downs']}")
    return "\n".join(out)


async def recap_embed(guild_id: int, start: datetime, end: datetime, title: str) -> discord.Embed:
    s, e = start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")
    rows = await leaderboard_rows(guild_id, s, e, 5)
    posted = await q_one("SELECT COUNT(*) n FROM posts WHERE guild_id=? AND kind!='search' AND posted_at>=? AND posted_at<?",
                         (guild_id, s, e))
    wanted = await q_all("SELECT title, COUNT(*) n FROM watchlist WHERE guild_id=? GROUP BY tmdb_id "
                         "ORDER BY n DESC LIMIT 3", (guild_id,))
    emb = discord.Embed(title=title, color=discord.Color.orange())
    emb.add_field(name="Top rated by this server", value=lb_lines(rows) or "No votes yet.", inline=False)
    emb.add_field(name="Films posted", value=str(posted["n"]), inline=True)
    if wanted:
        emb.add_field(name="Most wanted on the watchlist",
                      value="\n".join(f"{w['title']} ({w['n']})" for w in wanted), inline=True)
    return emb


@bot.tree.command(name="leaderboard", description="Top films by this server's votes")
@app_commands.guild_only()
async def leaderboard(interaction: discord.Interaction):
    rows = await leaderboard_rows(interaction.guild_id, "0000", "9999")
    if not rows:
        return await interaction.response.send_message("No votes yet. Use the buttons under a film post!", ephemeral=True)
    await interaction.response.send_message(embed=discord.Embed(
        title="🏆 Server leaderboard (all time)", description=lb_lines(rows), color=discord.Color.gold()))


@bot.tree.command(name="recap", description="This month's film recap")
@app_commands.guild_only()
async def recap(interaction: discord.Interaction):
    n = datetime.now(TZ)
    start = n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    await interaction.response.send_message(embed=await recap_embed(
        interaction.guild_id, start, n + timedelta(minutes=1), f"📆 {n.strftime('%B %Y')} so far"))



def mask_title(text: str, title: str) -> str:
    text = re.sub(re.escape(title), "▇▇▇", text, flags=re.I)
    for w in re.findall(r"\w{4,}", title):
        text = re.sub(re.escape(w), "▇▇▇", text, flags=re.I)
    return text


def norm(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()
    return re.sub(r"^(the|a|an) ", "", s)


class GuessModal(discord.ui.Modal, title="Guess the film"):
    guess = discord.ui.TextInput(label="Film title", max_length=100)

    def __init__(self, view: "TriviaView"):
        super().__init__()
        self.tv = view

    async def on_submit(self, interaction: discord.Interaction):
        tv = self.tv
        if tv.solved:
            return await interaction.response.send_message("Someone already got it!", ephemeral=True)
        used = tv.attempts.get(interaction.user.id, 0)
        if used >= 3:
            return await interaction.response.send_message("You're out of guesses. Let others try!", ephemeral=True)
        tv.attempts[interaction.user.id] = used + 1
        a, b = norm(self.guess.value), norm(tv.answer)
        if a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.85:
            tv.solved = True
            tv.stop()
            await run("INSERT INTO trivia_scores VALUES(?,?,?,1) ON CONFLICT(guild_id,user_id,season) DO UPDATE SET wins=wins+1", (interaction.guild_id, interaction.user.id, today().strftime("%Y-%m")))
            await interaction.response.send_message(f"🎉 {interaction.user.mention} got it! It was **{tv.full_title}**.")
            await tv.reveal(f"Solved by {interaction.user.display_name}")
        else:
            await interaction.response.send_message(f"❌ Not quite. {3 - used - 1} guess(es) left.", ephemeral=True)


class TriviaView(ServerOnlyView):
    def __init__(self, answer: str, full_title: str, poster: Optional[str]):
        super().__init__(timeout=600)
        self.answer, self.full_title, self.poster = answer, full_title, poster
        self.solved = False
        self.attempts: dict[int, int] = {}
        self.message: Optional[discord.Message] = None

    @discord.ui.button(label="Make a guess", emoji="🎯", style=discord.ButtonStyle.primary)
    async def guess_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.solved:
            return await interaction.response.send_message("Already solved!", ephemeral=True)
        await interaction.response.send_modal(GuessModal(self))

    async def reveal(self, note: str):
        if not self.message:
            return
        emb = discord.Embed(title=f"🎭 It was: {self.full_title}", description=note, color=discord.Color.green())
        if self.poster:
            emb.set_thumbnail(url=f"https://image.tmdb.org/t/p/w342{self.poster}")
        try:
            await self.message.edit(embed=emb, view=None)
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        if not self.solved:
            await self.reveal("Time's up! Nobody guessed it.")


@bot.tree.command(name="trivia", description="Guess the film from a clue")
@app_commands.checks.cooldown(1, 600, key=lambda i: i.guild_id)
@app_commands.guild_only()
async def trivia(interaction: discord.Interaction):
    await interaction.response.defer()
    params = {**build_params("any"), "vote_count.gte": 3000}
    mid = await pick_film(None, params)
    d = await bot.api.details(mid) if mid else {}
    if not d.get("id"):
        return await interaction.followup.send("Couldn't set up a round. Try again.")
    clue = mask_title(d.get("tagline") or trunc(d.get("overview"), 350), d["title"])
    genres = ", ".join(g["name"] for g in d.get("genres", [])[:3])
    emb = discord.Embed(title="🎭 Guess the film!", description=f"> {clue}", color=discord.Color.purple())
    emb.add_field(name="Genre", value=genres or "N/A", inline=True)
    emb.add_field(name="Decade", value=f"{year_of(d)[:3]}0s" if year_of(d) else "?", inline=True)
    emb.add_field(name="Runtime", value=runtime_str(d.get("runtime")), inline=True)
    emb.set_footer(text="3 guesses each · closes in 10 minutes")
    view = TriviaView(d["title"], f"{d['title']} ({year_of(d)})", d.get("poster_path"))
    view.message = await interaction.followup.send(embed=emb, view=view, wait=True)





async def run_release_alerts():
    rows = await q_all("SELECT guild_id, tmdb_id, title, GROUP_CONCAT(user_id) users FROM watchlist "
                       "GROUP BY guild_id, tmdb_id")
    for r in rows:
        s = await q_one("SELECT channel_id FROM settings WHERE guild_id=?", (r["guild_id"],))
        channel = bot.get_channel(s["channel_id"]) if s else None
        if not channel:
            continue
        details = await bot.api.details(r["tmdb_id"])
        if not details.get("id"):
            continue
        a = analyze(details)
        muted = {str(x["user_id"]) for x in await q_all("SELECT user_id FROM notifications WHERE guild_id=? AND releases=0", (r["guild_id"],))}
        who = " ".join(f"<@{u}>" for u in r["users"].split(",") if u not in muted)
        events = []
        if a["cinema_date"] and a["cinema_date"] <= today():
            events.append(("cinema", f"🎟️ **{r['title']}** has a recorded theatrical release in {REGION} ( {ts(a['cinema_date'])})."))
        if a["flat"]:
            events.append(("stream", f"📺 **{r['title']}** is now streaming on **{', '.join(a['stream'][:4])}**."))
        for kind, text in events:
            if await q_one("SELECT 1 FROM notified WHERE guild_id=? AND tmdb_id=? AND kind=?",
                           (r["guild_id"], r["tmdb_id"], kind)):
                continue
            try:
                await channel.send(f"{text}\nFrom your watchlist: {who}", allowed_mentions=discord.AllowedMentions(users=True))
                await run("INSERT OR IGNORE INTO notified VALUES(?,?,?)", (r["guild_id"], r["tmdb_id"], kind))
            except discord.HTTPException as e:
                log.warning("Alert failed: %s", e)


@tasks.loop(minutes=1)
async def daily_job():
    async with DAILY_LOCK:
        await run_daily_jobs()


async def run_daily_jobs():
    n = datetime.now(TZ)
    guilds = await q_all("SELECT * FROM settings WHERE channel_id IS NOT NULL")
    t = today()
    for s in guilds:
        try:
            control = await q_one("SELECT * FROM controls WHERE guild_id=?", (s["guild_id"],))
            hour, minute = map(int, control['daily_time'].split(':')) if control and control['daily_time'] else (POST_HOUR, POST_MIN)
            if (control and control['paused']) or (n.hour,n.minute) < (hour,minute):
                continue
            if not await q_one("SELECT 1 FROM job_runs WHERE guild_id=? AND day=?", (s["guild_id"], str(t))):
                if not await post_daily(s["guild_id"], s["channel_id"], s["role_id"]):
                    continue
                await run("INSERT OR IGNORE INTO job_runs VALUES(?,?)", (s["guild_id"], str(t)))
            channel = bot.get_channel(s["channel_id"])
            if channel and t.weekday() == 4 and not await q_one("SELECT 1 FROM job_parts WHERE guild_id=? AND day=? AND kind='cinema'", (s["guild_id"], str(t))):
                await channel.send(embed=await cinema_embed(
                    f"🎟️ New in {REGION} cinemas this week", t - timedelta(days=3), t + timedelta(days=7), by_date=True))
                await run("INSERT OR IGNORE INTO job_parts VALUES(?,?,'cinema')", (s["guild_id"], str(t)))
            if channel and t.day == 1 and not await q_one("SELECT 1 FROM job_parts WHERE guild_id=? AND day=? AND kind='recap'", (s["guild_id"], str(t))):
                end = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
                start = (end - timedelta(days=1)).replace(day=1)
                await channel.send(embed=await recap_embed(
                    s["guild_id"], start, end, f"📆 {start.strftime('%B %Y')} recap"))
                await run("INSERT OR IGNORE INTO job_parts VALUES(?,?,'recap')", (s["guild_id"], str(t)))
        except Exception:
            log.exception("Daily job failed for guild %s", s["guild_id"])
    try:
        await run_release_alerts()
    except Exception:
        log.exception("Release alerts failed")


@daily_job.before_loop
async def _before_daily():
    await bot.wait_until_ready()



if __name__ == "__main__":

    os.umask(0o077)
    if os.getenv("REQUIRE_GUILD_ALLOWLIST") == "1" and not ALLOWED_GUILD_ID:
        raise SystemExit("Set ALLOWED_GUILD_ID to your Discord server ID before cloud startup.")
    if ALLOWED_GUILD_ID and (not ALLOWED_GUILD_ID.isdigit() or int(ALLOWED_GUILD_ID) <= 0):
        raise SystemExit("ALLOWED_GUILD_ID must be a positive server ID.")
    if DEV_GUILD_ID and ALLOWED_GUILD_ID and DEV_GUILD_ID != ALLOWED_GUILD_ID:
        raise SystemExit("GUILD_ID and ALLOWED_GUILD_ID must match for this personal bot.")

    import fcntl
    database_lock = open(DB_PATH + ".lock", "a")
    try:
        fcntl.flock(database_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Another Teatro process is already using this database.")
    missing = [k for k, v in {"DISCORD_TOKEN": TOKEN, "TMDB_API_KEY": TMDB_KEY}.items() if not v]
    if missing:
        raise SystemExit(f"Missing in .env: {', '.join(missing)}")
    if not OMDB_KEY:
        log.warning("OMDB_API_KEY not set: IMDb / Rotten Tomatoes / Metacritic scores will be skipped.")
    bot.run(TOKEN)
