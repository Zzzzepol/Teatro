"""Handle movie nights and database updates."""
import asyncio
import json
import logging
from datetime import datetime, timedelta

import discord
from discord import app_commands

log = logging.getLogger(__name__)
SCHEMA = """
CREATE TABLE IF NOT EXISTS migrations(version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS ratings(guild_id INTEGER,user_id INTEGER,tmdb_id INTEGER,vote TEXT,created_at TEXT,PRIMARY KEY(guild_id,user_id,tmdb_id));
CREATE TABLE IF NOT EXISTS watched(guild_id INTEGER,user_id INTEGER,tmdb_id INTEGER,created_at TEXT,PRIMARY KEY(guild_id,user_id,tmdb_id));
CREATE TABLE IF NOT EXISTS job_runs(guild_id INTEGER,day TEXT,PRIMARY KEY(guild_id,day));
CREATE TABLE IF NOT EXISTS nights(id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id INTEGER,channel_id INTEGER,host_id INTEGER,state TEXT,start TEXT,deadline TEXT,max_runtime INTEGER,platform TEXT,language TEXT,winner INTEGER,event_id INTEGER,thread_id INTEGER,reminded INTEGER DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_night ON nights(guild_id) WHERE state NOT IN ('completed','cancelled');
CREATE TABLE IF NOT EXISTS nominations(night_id INTEGER,tmdb_id INTEGER,title TEXT,user_id INTEGER,runtime INTEGER,PRIMARY KEY(night_id,tmdb_id));
CREATE TABLE IF NOT EXISTS night_votes(night_id INTEGER,user_id INTEGER,tmdb_id INTEGER,PRIMARY KEY(night_id,user_id));
CREATE TABLE IF NOT EXISTS rsvps(night_id INTEGER,user_id INTEGER,status TEXT,PRIMARY KEY(night_id,user_id));
CREATE TABLE IF NOT EXISTS reviews(night_id INTEGER,user_id INTEGER,score INTEGER,body TEXT,PRIMARY KEY(night_id,user_id));
CREATE TABLE IF NOT EXISTS host_roles(guild_id INTEGER PRIMARY KEY,role_id INTEGER);
CREATE INDEX IF NOT EXISTS posts_guild_date ON posts(guild_id,posted_at);
CREATE INDEX IF NOT EXISTS ratings_movie ON ratings(guild_id,tmdb_id);
"""

async def migrate(db):
    await db.executescript(SCHEMA)
    if not await (await db.execute('SELECT 1 FROM migrations WHERE version=1')).fetchone():

        await db.execute("INSERT OR REPLACE INTO ratings SELECT guild_id,user_id,tmdb_id,vote,created_at FROM votes WHERE vote!='seen' ORDER BY created_at,message_id")
        await db.execute("INSERT OR REPLACE INTO watched SELECT guild_id,user_id,tmdb_id,created_at FROM votes WHERE vote='seen' ORDER BY created_at,message_id")
        await db.execute('INSERT INTO migrations VALUES(1)')
    await db.commit()

class Nights:
    def __init__(self, bot, core):
        self.bot, self.c = bot, core
        self.lock = asyncio.Lock()

    def now(self):
        return datetime.now(self.c.TZ)

    async def active(self, guild_id):
        return await self.c.q_one("SELECT * FROM nights WHERE guild_id=? AND state NOT IN ('completed','cancelled')", (guild_id,))

    async def require_host(self, i, night=None):
        role = await self.c.q_one('SELECT role_id FROM host_roles WHERE guild_id=?', (i.guild_id,))
        if i.user.guild_permissions.manage_guild or (night and night['host_id'] == i.user.id) or (role and any(r.id == role['role_id'] for r in i.user.roles)):
            return
        raise ValueError('You need Manage Server, the Movie Host role, or ownership of this movie night.')

    async def eligible(self, night, movie):
        if not movie.get('id') or movie.get('adult'):
            raise ValueError('Movie not found in TMDB or excluded as adult. Select a title from autocomplete and try again.')
        if not movie.get('runtime') or movie['runtime'] > night['max_runtime']:
            raise ValueError(f"This film runs {movie.get('runtime') or 'an unknown number of'} minutes; this night allows at most {night['max_runtime']} minutes. A host can create a new night with a higher max_runtime.")
        if night['language'] and movie.get('original_language') != night['language']:
            raise ValueError('This film does not match the selected language.')
        if night['platform'] and int(night['platform']) not in {p for p, _ in self.c.analyze(movie)['flat']}:
            raise ValueError('This film is not listed on the selected streaming service in your region.')

    async def announce(self, night, text):
        channel = self.bot.get_channel(night['channel_id'])
        if not channel:
            channel = await self.bot.fetch_channel(night['channel_id'])
        return await channel.send(text, allowed_mentions=discord.AllowedMentions.none())

    async def open_vote(self, night, minutes=60):
        count = await self.c.q_one('SELECT COUNT(*) n FROM nominations WHERE night_id=?', (night['id'],))
        if count['n'] < 2:
            raise ValueError('Nominate at least two films before opening voting.')
        deadline = min(self.now() + timedelta(minutes=minutes), datetime.fromisoformat(night['start'])-timedelta(minutes=5))
        if deadline <= self.now():
            raise ValueError('Reschedule the night before opening voting.')
        await self.c.run("UPDATE nights SET state='voting',deadline=? WHERE id=?", (deadline.isoformat(),night['id']))
        await self.announce(night, f"🍿 Voting is open for movie night #{night['id']} until <t:{int(deadline.timestamp())}:F>. Use `/movienight status` and `/movienight vote`. Ties go to the lowest TMDB ID; zero votes also use this rule.")

    async def close_vote(self, night):
        winner = await self.c.q_one('SELECT n.*,COUNT(v.user_id) votes FROM nominations n LEFT JOIN night_votes v ON n.night_id=v.night_id AND n.tmdb_id=v.tmdb_id WHERE n.night_id=? GROUP BY n.tmdb_id ORDER BY votes DESC,n.tmdb_id LIMIT 1', (night['id'],))
        if not winner:
            raise ValueError('No nominations found.')

        await self.c.run("UPDATE nights SET state='scheduling',winner=? WHERE id=?", (winner['tmdb_id'],night['id']))

    async def schedule(self, night):
        guild = self.bot.get_guild(night['guild_id'])
        winner = await self.c.q_one('SELECT * FROM nominations WHERE night_id=? AND tmdb_id=?',(night['id'],night['winner']))
        start = datetime.fromisoformat(night['start'])
        if start <= self.now():

            return
        marker = f'Teatro movie night #{night["id"]}'
        events = await guild.fetch_scheduled_events()
        event = next((e for e in events if e.description == marker), None)
        if event is None:
            event = await guild.create_scheduled_event(name=f"Teatro: {winner['title']}"[:100], description=marker,
                start_time=start,end_time=start+timedelta(minutes=winner['runtime'] or 120),
                entity_type=discord.EntityType.external,location=f"Discord channel {night['channel_id']}",privacy_level=discord.PrivacyLevel.guild_only)
        await self.c.run("UPDATE nights SET state='scheduled',event_id=? WHERE id=?",(event.id,night['id']))
        await self.announce(night, f"🎬 **{winner['title']}** won! <t:{int(start.timestamp())}:F>\n{event.url}\nUse `/movienight rsvp` to opt in to reminders.")

    async def tick(self):
        async with self.lock:
            rows = await self.c.q_all("SELECT * FROM nights WHERE state NOT IN ('completed','cancelled')")
            for n in rows:
                try:
                    now = self.now()
                    if n['state'] == 'voting' and now >= datetime.fromisoformat(n['deadline']):
                        await self.close_vote(n)
                    elif n['state'] == 'scheduling':
                        await self.schedule(n)
                    elif n['state'] == 'scheduled':
                        start = datetime.fromisoformat(n['start'])
                        channel = self.bot.get_channel(n['channel_id'])
                        if not channel:
                            continue
                        if not n['reminded'] and start-timedelta(minutes=30) <= now < start:
                            users = await self.c.q_all("SELECT user_id FROM rsvps WHERE night_id=? AND status='going'",(n['id'],))
                            for offset in range(0,len(users),50):
                                chunk = users[offset:offset+50]
                                await channel.send('🍿 Movie night starts '+f'<t:{int(start.timestamp())}:R>! '+ ' '.join(f'<@{u["user_id"]}>' for u in chunk),allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=u['user_id']) for u in chunk]))
                            await self.c.run('UPDATE nights SET reminded=1 WHERE id=?',(n['id'],))
                        if now >= start and not n['thread_id']:
                            thread = await channel.create_thread(name=f"Spoilers · Teatro night #{n['id']}",type=discord.ChannelType.public_thread,auto_archive_duration=1440)
                            await self.c.run('UPDATE nights SET thread_id=? WHERE id=?',(thread.id,n['id']))
                            await thread.send('Discuss the film here. Mark spoilers with ||spoiler text||. Afterward, use `/movienight review`.')
                        winner = await self.c.q_one('SELECT runtime FROM nominations WHERE night_id=? AND tmdb_id=?',(n['id'],n['winner']))
                        if now >= start+timedelta(minutes=winner['runtime'] or 120):
                            await self.announce(n, f"🎭 Movie night #{n['id']} has finished. Share a rating and spoiler-hidden review with `/movienight review night_id:{n['id']}`.")
                            await self.c.run("UPDATE nights SET state='completed' WHERE id=?",(n['id'],))
                except Exception:
                    log.exception('Movie night %s job failed; will retry',n['id'])

    async def worker(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await self.tick()
            except Exception:
                log.exception('Movie night worker failed')
            await asyncio.sleep(30)

async def install(bot, core):
    await migrate(core.DB)
    service = Nights(bot,core)
    group = app_commands.Group(name='movienight',description='Nominate, vote, schedule and review a movie night',guild_only=True)

    async def begin(i):
        await i.response.defer(ephemeral=True)

    async def active(i):
        n = await service.active(i.guild_id)
        if not n:
            raise ValueError('No active movie night. A host can use `/movienight create`.')
        return n

    @group.command(name='create',description='Host: open nominations; start format YYYY-MM-DD HH:MM in configured timezone')
    async def create(i:discord.Interaction,start:str,max_runtime:app_commands.Range[int,30,360]=180,platform:str=None,language:str=None):
        await begin(i)
        await service.require_host(i)
        when = datetime.strptime(start,'%Y-%m-%d %H:%M').replace(tzinfo=core.TZ)
        if when <= service.now()+timedelta(hours=2):
            raise ValueError('Choose a start at least two hours in the future.')
        if not isinstance(i.channel,discord.TextChannel):
            raise ValueError('Create movie nights in a server text channel.')
        if platform and not platform.isdigit():
            raise ValueError('Choose a streaming service from autocomplete.')
        if language and language not in core.LANGS.values():
            raise ValueError('Choose a language from autocomplete.')
        perms = i.channel.permissions_for(i.guild.me)
        if not all((perms.send_messages,perms.create_public_threads,perms.send_messages_in_threads,i.guild.me.guild_permissions.create_events)):
            raise ValueError('I need Send Messages, Create Public Threads, Send Messages in Threads, and Create Events.')
        async with service.lock:
            if await service.active(i.guild_id):
                raise ValueError('This server already has an active movie night.')
            await core.run("INSERT INTO nights(guild_id,channel_id,host_id,state,start,max_runtime,platform,language) VALUES(?,?,?,'nominating',?,?,?,?)",(i.guild_id,i.channel_id,i.user.id,when.isoformat(),max_runtime,platform,language))
            n=await active(i)
        await service.announce(n,f"🎬 Movie night #{n['id']} planned for <t:{int(when.timestamp())}:F>. Nominate with `/movienight nominate`. Maximum runtime: {max_runtime} minutes.")
        await i.followup.send('Nominations are open. Use `/movienight openvote` when ready.',ephemeral=True)

    create.autocomplete('platform')(core.platform_autocomplete)
    @create.autocomplete('language')
    async def language_ac(i,current):
        return [app_commands.Choice(name=k,value=v) for k,v in core.LANGS.items() if current.lower() in k.lower()]

    @group.command(name='nominate',description='Nominate a film; two nominations per member, twenty total')
    async def nominate(i:discord.Interaction,title:str):
        await begin(i)
        mid=await core.resolve_movie_id(title)
        movie=await bot.api.details(mid) if mid else {}
        async with service.lock:
            n=await active(i)
            if n['state']!='nominating':
                raise ValueError('Nominations are closed.')
            await service.eligible(n,movie)
            counts=await core.q_one('SELECT COUNT(*) total,SUM(user_id=?) mine FROM nominations WHERE night_id=?',(i.user.id,n['id']))
            if counts['total']>=20 or (counts['mine'] or 0)>=2:
                raise ValueError('Nomination limit reached: two per member, twenty per night.')
            if await core.q_one('SELECT 1 FROM nominations WHERE night_id=? AND tmdb_id=?',(n['id'],mid)):
                raise ValueError('That movie is already nominated.')
            await core.run('INSERT INTO nominations VALUES(?,?,?,?,?)',(n['id'],mid,movie['title'][:100],i.user.id,movie['runtime']))
        await i.followup.send(f"Nominated **{movie['title']}**.",ephemeral=True)
    nominate.autocomplete('title')(core.title_autocomplete)

    @group.command(name='openvote',description='Host: close nominations and open a timed vote')
    async def openvote(i:discord.Interaction,minutes:app_commands.Range[int,5,1440]=60):
        await begin(i)
        async with service.lock:
            n=await active(i); await service.require_host(i,n)
            if n['state']!='nominating':
                raise ValueError('This night is not accepting nominations.')
            await service.open_vote(n,minutes)
        await i.followup.send('Voting is open.',ephemeral=True)

    @group.command(name='vote',description='Vote for a nominated film; voting again updates your vote')
    async def vote(i:discord.Interaction,film:str):
        await begin(i)
        async with service.lock:
            n=await active(i)
            if n['state']!='voting' or service.now()>=datetime.fromisoformat(n['deadline']):
                raise ValueError('Voting is closed.')
            if not film.isdigit() or not await core.q_one('SELECT 1 FROM nominations WHERE night_id=? AND tmdb_id=?',(n['id'],int(film))):
                raise ValueError('Choose a nominated movie from autocomplete.')
            await core.run('INSERT OR REPLACE INTO night_votes VALUES(?,?,?)',(n['id'],i.user.id,int(film)))
        await i.followup.send('Vote saved.',ephemeral=True)
    @vote.autocomplete('film')
    async def vote_ac(i,current):
        n=await service.active(i.guild_id)
        if not n:return []
        rows=await core.q_all('SELECT * FROM nominations WHERE night_id=?',(n['id'],))
        return [app_commands.Choice(name=r['title'][:100],value=str(r['tmdb_id'])) for r in rows if current.lower() in r['title'].lower()][:25]

    @group.command(name='close',description='Host: close voting early and schedule the winner')
    async def close(i:discord.Interaction):
        await begin(i)
        async with service.lock:
            n=await active(i); await service.require_host(i,n)
            if n['state']!='voting':raise ValueError('This night is not voting.')
            await service.close_vote(n)
        await i.followup.send('Winner saved. The scheduled event will be created shortly.',ephemeral=True)

    @group.command(name='rsvp',description='Going opts into the movie-night reminder')
    @app_commands.choices(response=[app_commands.Choice(name=x,value=x) for x in ('going','maybe','cannot attend')])
    async def rsvp(i:discord.Interaction,response:str):
        await begin(i)
        async with service.lock:
            n=await active(i)
            await core.run('INSERT OR REPLACE INTO rsvps VALUES(?,?,?)',(n['id'],i.user.id,response))
        await i.followup.send(f'RSVP saved: {response}. Only Going receives a reminder.',ephemeral=True)

    @group.command(name='status',description='See the active night, nominations, votes and RSVPs')
    async def status(i:discord.Interaction):
        await begin(i); n=await active(i)
        rows=await core.q_all('SELECT n.title,COUNT(v.user_id) votes FROM nominations n LEFT JOIN night_votes v ON n.night_id=v.night_id AND n.tmdb_id=v.tmdb_id WHERE n.night_id=? GROUP BY n.tmdb_id ORDER BY votes DESC,n.tmdb_id',(n['id'],))
        rsvps=await core.q_all('SELECT status,COUNT(*) n FROM rsvps WHERE night_id=? GROUP BY status',(n['id'],))
        lines=[f"**{r['title']}** — {r['votes']} vote(s)" for r in rows]
        attendees=await core.q_all("SELECT r.user_id,s.provider_ids FROM rsvps r LEFT JOIN services s ON s.user_id=r.user_id WHERE r.night_id=? AND r.status='going'",(n['id'],))
        nominations=await core.q_all('SELECT tmdb_id,title FROM nominations WHERE night_id=? ORDER BY tmdb_id',(n['id'],))
        if attendees and nominations:
            details=await asyncio.gather(*(bot.api.details(r['tmdb_id']) for r in nominations))
            coverage=[]
            known=sum(bool(a['provider_ids']) for a in attendees)
            for film,d in zip(nominations,details):
                providers={pid for pid,_ in core.analyze(d)['flat']}
                accessible=sum(bool(providers.intersection(json.loads(a['provider_ids'] or '[]'))) for a in attendees)
                coverage.append((accessible,film['title']))
            coverage.sort(key=lambda x:(-x[0],x[1]))
            lines.append(f"\n**Streaming access** ({known}/{len(attendees)} attendees have saved services)")
            lines.extend(f"{title}: {count}/{len(attendees)} attendees have a listed service" for count,title in coverage)
        if n['state']=='nominating':
            suggestions=await core.q_all('SELECT title,COUNT(*) n FROM watchlist WHERE guild_id=? GROUP BY tmdb_id ORDER BY n DESC,title LIMIT 3',(i.guild_id,))
            if suggestions:lines.append('\nPopular watchlist suggestions: '+', '.join(r['title'] for r in suggestions))
        await i.followup.send(embed=discord.Embed(title=f"Movie night #{n['id']} · {n['state']}", description=core.trunc(f"Start: <t:{int(datetime.fromisoformat(n['start']).timestamp())}:F>\n"+'\n'.join(lines)+'\n'+', '.join(f"{r['status']}: {r['n']}" for r in rsvps), 4000)),ephemeral=True)

    @group.command(name='reschedule',description='Host: move a movie night to a new date')
    async def reschedule(i:discord.Interaction,start:str):
        await begin(i)
        when=datetime.strptime(start,'%Y-%m-%d %H:%M').replace(tzinfo=core.TZ)
        if when<=service.now()+timedelta(hours=2):raise ValueError('Choose a time at least two hours ahead.')
        async with service.lock:
            n=await active(i);await service.require_host(i,n)
            if n['thread_id']:raise ValueError('This night has started. Cancel it and create a new night.')
            if n['event_id']:
                event=await i.guild.fetch_scheduled_event(n['event_id'])
                winner=await core.q_one('SELECT runtime FROM nominations WHERE night_id=? AND tmdb_id=?',(n['id'],n['winner']))
                await event.edit(start_time=when,end_time=when+timedelta(minutes=winner['runtime'] or 120))
            await core.run('UPDATE nights SET start=?,reminded=0 WHERE id=?',(when.isoformat(),n['id']))
        await service.announce(n,f"📅 Movie night #{n['id']} moved to <t:{int(when.timestamp())}:F>.")
        await i.followup.send('Rescheduled. The voting deadline is unchanged.',ephemeral=True)

    @group.command(name='cancel',description='Host: cancel the active movie night')
    async def cancel(i:discord.Interaction):
        await begin(i)
        async with service.lock:
            n=await active(i);await service.require_host(i,n)
            if n['event_id']:
                try:
                    event=await i.guild.fetch_scheduled_event(n['event_id']);await event.delete()
                except discord.NotFound:pass
            await core.run("UPDATE nights SET state='cancelled' WHERE id=?",(n['id'],))
        await service.announce(n,f"Movie night #{n['id']} was cancelled.")
        await i.followup.send('Cancelled.',ephemeral=True)

    @group.command(name='hostrole',description='Admin: designate the Movie Host role')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def hostrole(i:discord.Interaction,role:discord.Role):
        await begin(i)
        if role.is_default() or role.managed:raise ValueError('Choose a dedicated, non-managed role.')
        await core.run('INSERT OR REPLACE INTO host_roles VALUES(?,?)',(i.guild_id,role.id))
        await i.followup.send('Movie Host role saved.',ephemeral=True)

    @group.command(name='review',description='Rate a finished movie night and post a spoiler-hidden review')
    async def review(i:discord.Interaction,night_id:int,score:app_commands.Range[int,1,5],text:app_commands.Range[str,1,1000]):
        await begin(i)
        n=await core.q_one("SELECT * FROM nights WHERE id=? AND guild_id=? AND state='completed'",(night_id,i.guild_id))
        if not n:raise ValueError('Choose a completed movie night from this server.')
        attendance=await core.q_one("SELECT 1 FROM rsvps WHERE night_id=? AND user_id=? AND status='going'",(night_id,i.user.id))
        if not attendance:raise ValueError('Reviews are available to members who RSVP’d Going.')
        if await core.q_one('SELECT 1 FROM reviews WHERE night_id=? AND user_id=?',(night_id,i.user.id)):
            raise ValueError('You already reviewed this night.')
        channel=bot.get_channel(n['thread_id']) or await bot.fetch_channel(n['thread_id'])
        safe=discord.utils.escape_markdown(text.replace('|','').replace('\n',' '))
        await channel.send(f"{discord.utils.escape_markdown(i.user.display_name)} rated it {score}/5.\n||{safe}||",allowed_mentions=discord.AllowedMentions.none())
        await core.run('INSERT INTO reviews VALUES(?,?,?,?)',(night_id,i.user.id,score,text))
        await core.run('INSERT OR REPLACE INTO watched VALUES(?,?,?,?)',(i.guild_id,i.user.id,n['winner'],core.now_iso()))
        await core.run('INSERT OR REPLACE INTO ratings VALUES(?,?,?,?,?)',(i.guild_id,i.user.id,n['winner'],'up' if score>=4 else 'meh' if score==3 else 'down',core.now_iso()))
        await i.followup.send('Review posted with spoilers hidden; watched status and rating saved.',ephemeral=True)

    @group.error
    async def error(i,error):
        original=getattr(error,'original',error)
        message=str(original) if isinstance(original,ValueError) else 'Could not complete that action. Check permissions and try again.'
        if not isinstance(original,ValueError):log.error('Movie night command failed',exc_info=original)
        if i.response.is_done():await i.followup.send(message,ephemeral=True)
        else:await i.response.send_message(message,ephemeral=True)

    bot.tree.add_command(group)
    bot.workflow_task=asyncio.create_task(service.worker())
    bot.nights=service
