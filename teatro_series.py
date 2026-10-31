"""Series recommendations, daily posts and episode charts."""
import asyncio
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta
from io import BytesIO

import aiohttp
import discord
from discord import app_commands

from series_chart import render_chart, SEASONS_PER_PAGE, EPISODES_PER_PAGE

log = logging.getLogger(__name__)
SCHEMA = """
CREATE TABLE IF NOT EXISTS series_settings(guild_id INTEGER PRIMARY KEY,channel_id INTEGER,role_id INTEGER,daily_time TEXT,enabled INTEGER);
CREATE TABLE IF NOT EXISTS series_posts(message_id INTEGER PRIMARY KEY,guild_id INTEGER,channel_id INTEGER,tmdb_id INTEGER,title TEXT,kind TEXT,posted_at TEXT);
CREATE TABLE IF NOT EXISTS series_ratings(guild_id INTEGER,user_id INTEGER,tmdb_id INTEGER,vote TEXT,PRIMARY KEY(guild_id,user_id,tmdb_id));
CREATE TABLE IF NOT EXISTS series_watched(guild_id INTEGER,user_id INTEGER,tmdb_id INTEGER,PRIMARY KEY(guild_id,user_id,tmdb_id));
CREATE TABLE IF NOT EXISTS series_watchlist(guild_id INTEGER,user_id INTEGER,tmdb_id INTEGER,title TEXT,PRIMARY KEY(guild_id,user_id,tmdb_id));
CREATE TABLE IF NOT EXISTS series_daily_runs(guild_id INTEGER,day TEXT,PRIMARY KEY(guild_id,day));
CREATE INDEX IF NOT EXISTS series_history ON series_posts(guild_id,posted_at);
"""


def series_embed(show, region, heading):
    title = show.get('name') or 'Untitled series'
    year = (show.get('first_air_date') or '')[:4]
    embed = discord.Embed(title=(f'{title} ({year})' if year else title)[:256],
        url=f"https://www.themoviedb.org/tv/{show['id']}", color=0x45BFA8,
        description=(show.get('overview') or 'No synopsis available.')[:1000])
    embed.set_author(name=heading)
    if show.get('poster_path'):
        embed.set_thumbnail(url=f"https://image.tmdb.org/t/p/w342{show['poster_path']}")
    embed.add_field(name='Genre', value=(', '.join(g['name'] for g in show.get('genres', [])) or 'Unknown')[:1024])
    runtime = show.get('episode_run_time') or []
    embed.add_field(name='Episode length', value=f'{runtime[0]} min' if runtime else 'Varies / unknown')
    embed.add_field(name='Seasons / episodes', value=f"{show.get('number_of_seasons', '?')} seasons · {show.get('number_of_episodes', '?')} episodes")
    creators = ', '.join(p['name'] for p in show.get('created_by', [])[:4])
    if creators:
        embed.add_field(name='Created by', value=creators[:1024], inline=False)
    cast = show.get('aggregate_credits', {}).get('cast', [])[:6]
    cast_names = []
    for actor in cast:
        roles = actor.get('roles') or []
        character = roles[0].get('character') if roles else None
        cast_names.append(actor['name'] + (f' — {character}' if character else ''))
    embed.add_field(name='Cast', value=('\n'.join(cast_names) or 'Cast unavailable.')[:1024], inline=False)
    rating = show.get('vote_average')
    embed.add_field(name='TMDB rating', value=f"{rating:.1f}/10 · {show.get('vote_count', 0):,} votes" if isinstance(rating, (float, int)) and show.get('vote_count') else 'Not rated yet')
    embed.add_field(name='Status', value=show.get('status') or 'Unknown')
    availability = show.get('watch/providers', {}).get('results', {}).get(region, {})
    lines = []
    for key, label in [('flatrate', 'Subscription'), ('free', 'Free'), ('ads', 'With ads'), ('rent', 'Rent'), ('buy', 'Buy')]:
        names = ', '.join(provider['provider_name'] for provider in availability.get(key, [])[:6])
        if names:
            lines.append(f'**{label}:** {names}')
    embed.add_field(name=f'Where to watch ({region})', value=('\n'.join(lines) or 'No regional availability found in our data.')[:1024], inline=False)
    embed.set_footer(text='Series and episode ratings: TMDB · Streaming data: JustWatch · Availability may vary by season')
    return embed


class SeriesService:
    def __init__(self, bot, core):
        self.bot, self.c = bot, core
        self.cache = {}
        self.lock = asyncio.Lock()
        self.requests = asyncio.Semaphore(3)
        self.charts = asyncio.Semaphore(1)

    async def cached(self, path, params=None):
        key = (path, json.dumps(params or {}, sort_keys=True))
        hit = self.cache.get(key)
        if hit and time.monotonic() - hit[0] < 3600:
            return hit[1]
        async with self.requests:
            data = await self.bot.api.tmdb(path, params)
        if data:
            if len(self.cache) >= 128:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = (time.monotonic(), data)
        return data

    async def details(self, series_id):
        return await self.cached(f'/tv/{series_id}', {'language': 'en-US', 'append_to_response': 'aggregate_credits,videos,watch/providers,external_ids'})

    async def search(self, query):
        match = re.fullmatch(r'(.+?)\s*\(?((?:19|20)\d{2})\)?', query.strip())
        params = {'query': query.strip(), 'include_adult': 'false'}
        if match:
            params.update(query=match[1].strip(), first_air_date_year=match[2])
        return await self.cached('/search/tv', params)

    async def resolve(self, query):
        if query.startswith('tv:') and query[3:].isdigit():
            return int(query[3:])
        data = await self.search(query)
        if 'results' not in data:
            raise ValueError('Series search is unavailable. Please try again shortly.')
        results = [show for show in data['results'] if not show.get('adult')]
        if not results:
            raise ValueError('No series found. Try its title or choose an autocomplete result.')
        return results[0]['id']

    async def pick(self, guild_id, user_id=None):
        since = (datetime.now(self.c.TZ) - timedelta(days=180)).isoformat()
        rows = await self.c.q_all("SELECT tmdb_id FROM series_posts WHERE guild_id=? AND kind!='search' AND posted_at>=?", (guild_id, since))
        excluded = {r['tmdb_id'] for r in rows}
        if user_id:
            watched = await self.c.q_all('SELECT tmdb_id FROM series_watched WHERE guild_id=? AND user_id=?', (guild_id, user_id))
            excluded.update(r['tmdb_id'] for r in watched)
        params = {'include_adult': 'false', 'vote_average.gte': 7, 'vote_count.gte': 100,
                  'first_air_date.lte': str(self.c.today()), 'with_type': '2|3|4', 'sort_by': 'popularity.desc'}
        first = await self.cached('/discover/tv', {**params, 'page': 1})
        if 'results' not in first:
            raise ValueError('Series recommendations are unavailable. Please try again shortly.')
        pages = list(range(1, min(10, first.get('total_pages', 1)) + 1))
        random.shuffle(pages)
        for page in pages[:4]:
            data = first if page == 1 else await self.cached('/discover/tv', {**params, 'page': page})
            candidates = [s for s in data.get('results', []) if s['id'] not in excluded and not s.get('adult')]
            random.shuffle(candidates)
            for candidate in candidates[:3]:
                show = await self.details(candidate['id'])
                if show.get('id') and not show.get('adult'):
                    return show
        raise ValueError('No new series found right now. Try /series with a title.')

    async def chart(self, show, season_page=1, episode_page=1):
        async with self.charts:
            seasons = sorted({s['season_number'] for s in show.get('seasons', []) if s.get('season_number', 0) > 0})
            first = (season_page - 1) * SEASONS_PER_PAGE
            selected = seasons[first:first + SEASONS_PER_PAGE]
            if not selected:
                raise ValueError('No seasons on that page. Start with season_page:1.')
            results = await asyncio.gather(*(self.cached(f"/tv/{show['id']}/season/{number}", {'language': 'en-US'}) for number in selected))
            if any('episodes' not in result for result in results):
                raise ValueError('Some season data is temporarily unavailable. Try the chart again shortly.')
            for number, result in zip(selected, results):
                if result.get('season_number') != number:
                    raise ValueError('Season data did not match the request. Try again later.')
            low = (episode_page - 1) * EPISODES_PER_PAGE + 1
            high = low + EPISODES_PER_PAGE - 1
            if not any(low <= e.get('episode_number', 0) <= high for season in results for e in season['episodes']):
                raise ValueError('No episodes on that page. Try a lower episode_page.')
            poster = None
            path = show.get('poster_path') or ''
            if re.fullmatch(r'/[A-Za-z0-9._-]+', path):
                try:
                    async with self.bot.api.s.get('https://image.tmdb.org/t/p/w342' + path, timeout=aiohttp.ClientTimeout(total=8)) as response:
                        if response.status == 200:
                            data = bytearray()
                            async for chunk in response.content.iter_chunked(65536):
                                data.extend(chunk)
                                if len(data) > 2_000_000:
                                    break
                            if len(data) <= 2_000_000:
                                poster = bytes(data)
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass
            png = await asyncio.to_thread(render_chart, show, results, self.c.today(), poster, episode_page)
            note = f'Seasons {selected[0]}–{selected[-1]} of {len(seasons)} · Episodes {low}–{high}. Use /seriesratings for other pages.'
            return png, note

    async def send(self, show, channel, guild_id, kind, interaction=None, role_id=None):
        if not show.get('id') or show.get('adult'):
            raise ValueError('Series details are unavailable or this series is marked adult.')
        embed = series_embed(show, self.c.REGION, '📺 Daily Series Pick' if kind == 'daily' else '📺 Series recommendation')
        file = None
        try:
            png, note = await self.chart(show)
            file = discord.File(BytesIO(png), filename='episode-ratings.png')
            embed.set_image(url='attachment://episode-ratings.png')
            embed.add_field(name='Episode ratings', value=note, inline=False)
        except ValueError as error:
            embed.add_field(name='Episode ratings', value=str(error), inline=False)
        except Exception:
            log.exception('Series chart failed for %s', show['id'])
            embed.add_field(name='Episode ratings', value='Chart unavailable right now. Try /seriesratings later.', inline=False)
        kwargs = {'embed': embed, 'view': SeriesView(self, show), 'allowed_mentions': discord.AllowedMentions(users=False, everyone=False, replied_user=False, roles=[discord.Object(id=role_id)] if role_id else False)}
        if file:
            kwargs['file'] = file
        if kind == 'daily':
            kwargs['content'] = (f'<@&{role_id}> ' if role_id else '') + '**Today’s series pick**'
        try:
            message = await interaction.followup.send(**kwargs, wait=True) if interaction else await channel.send(**kwargs)
        finally:
            if file:
                file.close()
        await self.c.run('INSERT INTO series_posts VALUES(?,?,?,?,?,?,?)', (message.id, guild_id, channel.id, show['id'], show['name'], kind, self.c.now_iso()))
        return message

    async def daily(self, settings):
        async with self.lock:
            day = str(self.c.today())
            args = (settings['guild_id'], day)
            if await self.c.q_one('SELECT 1 FROM series_daily_runs WHERE guild_id=? AND day=?', args):
                return False
            channel = self.bot.get_channel(settings['channel_id'])
            if not channel:
                raise ValueError('The series channel is missing or inaccessible. Run /seriessetup again.')
            existing = await self.c.q_one("SELECT 1 FROM series_posts WHERE guild_id=? AND kind='daily' AND substr(posted_at,1,10)=?", args)
            if not existing:
                show = await self.pick(settings['guild_id'])
                await self.send(show, channel, settings['guild_id'], 'daily', role_id=settings['role_id'])
            await self.c.run('INSERT OR IGNORE INTO series_daily_runs VALUES(?,?)', args)
            return not bool(existing)

    async def tick(self):
        now = datetime.now(self.c.TZ)
        settings = await self.c.q_all('SELECT * FROM series_settings WHERE enabled=1')
        for row in settings:
            hour, minute = map(int, row['daily_time'].split(':'))
            if (now.hour, now.minute) < (hour, minute):
                continue
            try:
                await self.daily(row)
            except Exception:
                log.exception('Daily series failed for guild %s', row['guild_id'])

    async def worker(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await self.tick()
            except Exception:
                log.exception('Series scheduler failed')
            await asyncio.sleep(60)


class SeriesView(discord.ui.View):
    def __init__(self, service, show=None):
        super().__init__(timeout=None)
        self.service = service
        actions = [('Loved it', 'up'), ('Meh', 'meh'), ('Not for me', 'down'), ('Seen it', 'seen'),
                   ('Watchlist', 'save'), ('Can I watch?', 'watch'), ('Episode ratings', 'chart')]
        for index, (label, action) in enumerate(actions):
            button = discord.ui.Button(label=label, custom_id=f'series:{action}', row=0 if index < 4 else 1,
                                       style=discord.ButtonStyle.secondary)
            button.callback = self.callback(action)
            self.add_item(button)
        if show:
            trailer = service.c.trailer_url(show)
            imdb = show.get('external_ids', {}).get('imdb_id')
            region = show.get('watch/providers', {}).get('results', {}).get(service.c.REGION, {})
            for label, url in [('Trailer', trailer), ('IMDb', f'https://www.imdb.com/title/{imdb}/' if imdb else None),
                               ('Watch providers', region.get('link'))]:
                if url and url.startswith('https://'):
                    self.add_item(discord.ui.Button(label=label, url=url, row=2))

    async def interaction_check(self, interaction):
        allowed = self.service.c.ALLOWED_GUILD_ID
        if allowed and interaction.guild_id != int(allowed):
            await interaction.response.send_message('This bot is reserved for its home server.', ephemeral=True)
            return False
        return True

    async def on_error(self, interaction, error, item):
        log.error('Series button failed', exc_info=error)
        message = str(error) if isinstance(error, ValueError) else 'Could not complete that action. Please try again.'
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    def callback(self, action):
        async def handle(i):
            await i.response.defer(ephemeral=True)
            c = self.service.c
            post = await c.q_one('SELECT * FROM series_posts WHERE message_id=? AND guild_id=?', (i.message.id, i.guild_id))
            if not post:
                raise ValueError('This series post is no longer in my records.')
            args = (i.guild_id, i.user.id, post['tmdb_id'])
            if action in ('up', 'meh', 'down', 'seen'):
                async with c.RATING_LOCK:
                    table = 'series_watched' if action == 'seen' else 'series_ratings'
                    existing = await c.q_one(f'SELECT * FROM {table} WHERE guild_id=? AND user_id=? AND tmdb_id=?', args)
                    if existing and (action == 'seen' or existing['vote'] == action):
                        await c.run(f'DELETE FROM {table} WHERE guild_id=? AND user_id=? AND tmdb_id=?', args)
                        message = 'Watched status removed.' if action == 'seen' else 'Rating removed.'
                    elif action == 'seen':
                        await c.run('INSERT INTO series_watched VALUES(?,?,?)', args)
                        message = 'Marked as watched. Your rating stays separate.'
                    else:
                        await c.run('INSERT OR REPLACE INTO series_ratings VALUES(?,?,?,?)', (*args, action))
                        message = 'Your series rating was saved.'
            elif action == 'save':
                await c.run('INSERT OR IGNORE INTO series_watchlist VALUES(?,?,?,?)', (*args, post['title']))
                message = 'Saved to your private series watchlist. See /serieswatchlist view.'
            else:
                show = await self.service.details(post['tmdb_id'])
                if not show.get('id'):
                    raise ValueError('Series details are temporarily unavailable.')
                if action == 'chart':
                    png, note = await self.service.chart(show)
                    file = discord.File(BytesIO(png), filename='episode-ratings.png')
                    try:
                        return await i.followup.send(note, file=file, ephemeral=True)
                    finally:
                        file.close()
                services = await c.q_one('SELECT provider_ids FROM services WHERE user_id=?', (i.user.id,))
                mine = set(json.loads(services['provider_ids'])) if services else set()
                providers = show.get('watch/providers', {}).get('results', {}).get(c.REGION, {}).get('flatrate', [])
                hits = [p['provider_name'] for p in providers if p['provider_id'] in mine]
                if hits:
                    message = 'Listed on your services: ' + ', '.join(hits) + '. Availability may vary by season.'
                elif not services:
                    message = 'Set your services with /myservices first.'
                else:
                    message = 'No matching subscription availability found in our data for your services.'
            await i.followup.send(message, ephemeral=True)
        return handle


async def install(bot, c):
    await c.DB.executescript(SCHEMA)
    await c.DB.commit()
    service = SeriesService(bot, c)
    bot.series = service
    bot.add_view(SeriesView(service))

    async def title_autocomplete(i, current):
        if len(current) < 2:
            return []
        data = await service.search(current)
        return [app_commands.Choice(name=c.trunc(f"{s['name']} ({(s.get('first_air_date') or '')[:4]})", 100), value=f"tv:{s['id']}")
                for s in data.get('results', [])[:20] if not s.get('adult')]

    async def command_error(i, error):
        i.extras['teatro_error_handled'] = True
        original = getattr(error, 'original', error)
        if isinstance(original, ValueError):
            message = str(original)
        elif isinstance(original, app_commands.MissingPermissions):
            message = 'You need Manage Server permission for this command.'
        elif isinstance(original, app_commands.CommandOnCooldown):
            message = f'Try again in {original.retry_after:.0f} seconds.'
        elif isinstance(original, discord.Forbidden):
            message = 'I need View Channel, Send Messages, Embed Links and Attach Files in this channel.'
        else:
            log.error('Series command failed', exc_info=original)
            message = 'Series data is unavailable right now. Please try again shortly.'
        if i.response.is_done():
            await i.followup.send(message, ephemeral=True)
        else:
            await i.response.send_message(message, ephemeral=True)

    @bot.tree.command(name='series', description='Find a series, or get a fresh recommendation with episode ratings')
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 15, key=lambda i: (i.guild_id, i.user.id))
    async def series(i: discord.Interaction, title: str = None):
        await i.response.defer()
        show = await service.details(await service.resolve(title)) if title else await service.pick(i.guild_id, i.user.id)
        await service.send(show, i.channel, i.guild_id, 'search' if title else 'manual', interaction=i)
    series.autocomplete('title')(title_autocomplete)
    series.error(command_error)

    @bot.tree.command(name='seriesratings', description='See an episode-rating grid; browse long shows by season and episode page')
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 20, key=lambda i: (i.guild_id, i.user.id))
    async def ratings(i: discord.Interaction, title: str, season_page: app_commands.Range[int, 1, 100] = 1, episode_page: app_commands.Range[int, 1, 200] = 1):
        await i.response.defer()
        show = await service.details(await service.resolve(title))
        if not show.get('id') or show.get('adult'):
            raise ValueError('Series details unavailable or excluded as adult.')
        png, note = await service.chart(show, season_page, episode_page)
        file = discord.File(BytesIO(png), filename='episode-ratings.png')
        try:
            await i.followup.send(note, file=file)
        finally:
            file.close()
    ratings.autocomplete('title')(title_autocomplete)
    ratings.error(command_error)

    @bot.tree.command(name='seriessetup', description='Admin: enable or pause daily series picks on their own schedule')
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(i: discord.Interaction, channel: discord.TextChannel, daily_time: str = '18:00', role: discord.Role = None, enabled: bool = True):
        await i.response.defer(ephemeral=True)
        try:
            normalized = datetime.strptime(daily_time, '%H:%M').strftime('%H:%M')
        except ValueError:
            raise ValueError('Use HH:MM, such as 18:00.')
        permissions = channel.permissions_for(i.guild.me)
        if not all((permissions.view_channel, permissions.send_messages, permissions.embed_links, permissions.attach_files)):
            raise ValueError('I need View Channel, Send Messages, Embed Links and Attach Files in the series channel.')
        if role and (role.is_default() or role.managed or role >= i.guild.me.top_role or not i.guild.me.guild_permissions.manage_roles):
            raise ValueError('Choose an assignable ping role below my role and grant Manage Roles.')
        async with service.lock:
            await c.run('INSERT OR REPLACE INTO series_settings VALUES(?,?,?,?,?)', (i.guild_id, channel.id, role.id if role else None, normalized, int(enabled)))
        await i.followup.send(f"Daily series {'enabled' if enabled else 'paused'} in {channel.mention} at {normalized} ({c.TZ.key}). Use /seriesdaily to post today’s pick now. The next scheduler check catches up if today’s time has passed.", ephemeral=True)
    setup.error(command_error)

    @bot.tree.command(name='seriesdaily', description='Admin: post today’s series pick now, once per day')
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def daily(i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        settings = await c.q_one('SELECT * FROM series_settings WHERE guild_id=?', (i.guild_id,))
        if not settings:
            raise ValueError('Run /seriessetup first.')
        posted = await service.daily(settings)
        await i.followup.send('Daily series posted.' if posted else 'Today’s series pick has already been posted.', ephemeral=True)
    daily.error(command_error)

    @bot.tree.command(name='seriesnotifications', description='Join or leave the configured daily series ping role')
    @app_commands.guild_only()
    async def notifications(i: discord.Interaction, enabled: bool):
        await i.response.defer(ephemeral=True)
        settings = await c.q_one('SELECT role_id FROM series_settings WHERE guild_id=?', (i.guild_id,))
        role = i.guild.get_role(settings['role_id']) if settings and settings['role_id'] else None
        if not role:
            raise ValueError('An admin must choose a ping role with /seriessetup first.')
        if role.is_default() or role.managed or role >= i.guild.me.top_role or not i.guild.me.guild_permissions.manage_roles:
            raise ValueError('The series role must be below my role, and I need Manage Roles.')
        if enabled:
            await i.user.add_roles(role, reason='Daily series opt-in')
        else:
            await i.user.remove_roles(role, reason='Daily series opt-out')
        await i.followup.send('Series pings enabled.' if enabled else 'Series pings disabled.', ephemeral=True)
    notifications.error(command_error)

    watchlist = app_commands.Group(name='serieswatchlist', description='Your private series watchlist', guild_only=True)
    @watchlist.command(name='view', description='See series saved with the Watchlist button')
    async def view_list(i: discord.Interaction, page: app_commands.Range[int, 1, 1000] = 1):
        rows = await c.q_all('SELECT title FROM series_watchlist WHERE guild_id=? AND user_id=? ORDER BY title,tmdb_id LIMIT 15 OFFSET ?', (i.guild_id, i.user.id, (page - 1) * 15))
        lines = [discord.utils.escape_markdown(c.trunc(row['title'], 90)) for row in rows]
        await i.response.send_message(f'**Your series watchlist · page {page}**\n' + ('\n'.join(lines) or 'No series on this page.'), ephemeral=True)
    @watchlist.command(name='remove', description='Remove a saved series')
    async def remove(i: discord.Interaction, title: str):
        if not title.startswith('tv:') or not title[3:].isdigit():
            raise ValueError('Choose an exact series from autocomplete.')
        await c.run('DELETE FROM series_watchlist WHERE guild_id=? AND user_id=? AND tmdb_id=?', (i.guild_id, i.user.id, int(title[3:])))
        await i.response.send_message('Removed if it was on your list.', ephemeral=True)
    @remove.autocomplete('title')
    async def saved_titles(i, current):
        rows = await c.q_all('SELECT title,tmdb_id FROM series_watchlist WHERE guild_id=? AND user_id=? AND title LIKE ? LIMIT 25', (i.guild_id, i.user.id, '%' + current + '%'))
        return [app_commands.Choice(name=c.trunc(row['title'], 100), value=f"tv:{row['tmdb_id']}") for row in rows]
    watchlist.error(command_error)
    bot.tree.add_command(watchlist)
    bot.series_task = asyncio.create_task(service.worker())
