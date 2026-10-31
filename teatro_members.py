"""Handle member preferences and server settings."""
import json
import discord
from discord import app_commands

async def install(bot,c):
    await c.DB.executescript('''
CREATE TABLE IF NOT EXISTS preferences(guild_id INTEGER,user_id INTEGER,genres TEXT,language TEXT,max_runtime INTEGER,PRIMARY KEY(guild_id,user_id));
CREATE TABLE IF NOT EXISTS controls(guild_id INTEGER PRIMARY KEY,paused INTEGER DEFAULT 0,daily_time TEXT);
CREATE TABLE IF NOT EXISTS notifications(guild_id INTEGER,user_id INTEGER,releases INTEGER DEFAULT 1,PRIMARY KEY(guild_id,user_id));
''')
    await c.DB.commit()

    @bot.tree.command(name='help',description='Get started with Teatro')
    async def help_cmd(i:discord.Interaction):
        await i.response.send_message('🎭 **Welcome to Teatro**\n1. `/myservices` — choose your streaming services.\n2. `/preferences` — set your movie tastes.\n3. `/film` — discover a film; `/search` — find a specific title.\n4. `/watchlist add` — share what you want to watch.\n5. `/movienight status` — join the next movie night.\n6. `/series` — discover a series with cast, synopsis, providers and episode ratings.\n7. `/serieswatchlist view` — your private saved series.\nUse `/rolepanel` for film pings, `/seriesnotifications` for series pings, `/notifications` for release pings, and `/movienight rsvp` for event reminders. `/watchlist` is public; `/mylist` and `/serieswatchlist` are private. Admins can enable daily series with `/seriessetup`.',ephemeral=True)

    @bot.tree.command(name='preferences',description='Save preferences used by your personal film recommendations')
    @app_commands.guild_only()
    @app_commands.choices(genre=c.GENRE_CHOICES,language=c.LANG_CHOICES)
    async def preferences(i:discord.Interaction,genre:str=None,language:str=None,max_runtime:app_commands.Range[int,30,360]=180):
        await c.run('INSERT OR REPLACE INTO preferences VALUES(?,?,?,?,?)',(i.guild_id,i.user.id,genre,language,max_runtime))
        await i.response.send_message('Preferences saved. Explicit `/film` filters override these defaults.',ephemeral=True)

    @bot.tree.command(name='notifications',description='Opt into or out of watchlist release pings')
    @app_commands.guild_only()
    async def notifications(i:discord.Interaction,releases:bool):
        await c.run('INSERT OR REPLACE INTO notifications VALUES(?,?,?)',(i.guild_id,i.user.id,int(releases)))
        await i.response.send_message('Release notification preference saved.',ephemeral=True)

    @bot.tree.command(name='settings',description='Admin: pause daily posts or change their local time')
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def settings(i:discord.Interaction,paused:bool=False,daily_time:str=None):
        if daily_time:
            try:
                c.datetime.strptime(daily_time,'%H:%M')
            except ValueError:
                return await i.response.send_message('Use HH:MM, such as 21:00.',ephemeral=True)
        await c.run('INSERT INTO controls VALUES(?,?,?) ON CONFLICT(guild_id) DO UPDATE SET paused=excluded.paused,daily_time=COALESCE(excluded.daily_time,controls.daily_time)',(i.guild_id,int(paused),daily_time))
        await i.response.send_message(f'Daily posts {"paused" if paused else "enabled"}. Timezone: {c.TZ.key}.',ephemeral=True)

    @bot.tree.command(name='health',description='Admin: inspect bot and database health')
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def health(i:discord.Interaction):
        row=await c.q_one('SELECT MAX(day) day FROM job_runs WHERE guild_id=?',(i.guild_id,))
        await i.response.send_message(f'Database: reachable\nDiscord latency: {bot.latency:.2f}s\nDaily scheduler: {"running" if c.daily_job.is_running() else "stopped"}\nMovie-night worker: {"stopped" if bot.workflow_task.done() else "running"}\nLast daily post: {row["day"] or "none"}',ephemeral=True)

    admin=app_commands.Group(name='admin',description='Server administration',guild_only=True,default_permissions=discord.Permissions(manage_guild=True))
    @admin.command(name='status',description='Inspect schedule, movie night and permissions')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def admin_status(i:discord.Interaction):
        await i.response.defer(ephemeral=True)
        settings=await c.q_one('SELECT * FROM settings WHERE guild_id=?',(i.guild_id,))
        controls=await c.q_one('SELECT * FROM controls WHERE guild_id=?',(i.guild_id,))
        night=await bot.nights.active(i.guild_id)
        now=c.datetime.now(c.TZ)
        hour,minute=map(int,controls['daily_time'].split(':')) if controls and controls['daily_time'] else (c.POST_HOUR,c.POST_MIN)
        next_post=now.replace(hour=hour,minute=minute,second=0,microsecond=0)
        done=await c.q_one('SELECT 1 FROM job_runs WHERE guild_id=? AND day=?',(i.guild_id,str(now.date())))
        if done:next_post+=c.timedelta(days=1)
        if next_post<now:next_post=now
        channel=i.guild.get_channel(settings['channel_id']) if settings else None
        role=i.guild.get_role(settings['role_id']) if settings and settings['role_id'] else None
        missing=[]
        if channel:
            permissions=channel.permissions_for(i.guild.me)
            missing=[name.replace('_',' ') for name in ('view_channel','send_messages','embed_links','create_public_threads','send_messages_in_threads') if not getattr(permissions,name)]
        else:missing.append('Configured text channel')
        if settings and settings['role_id'] and (not role or role.managed or role>=i.guild.me.top_role or not i.guild.me.guild_permissions.manage_roles):
            missing.append('Assignable ping role / Manage Roles')
        if not i.guild.me.guild_permissions.create_events:missing.append('Create Events')
        schedule='Paused' if controls and controls['paused'] else f'<t:{int(next_post.timestamp())}:F>' if channel else 'Run /setup first'
        last=await c.q_one('SELECT MAX(day) day FROM job_runs WHERE guild_id=?',(i.guild_id,))
        alert=getattr(bot,'last_release_check',None)
        series_status='Not configured'
        if hasattr(bot,'series'):
            series_settings=await c.q_one('SELECT * FROM series_settings WHERE guild_id=?',(i.guild_id,))
            if series_settings:
                series_status=f"{'Enabled' if series_settings['enabled'] else 'Paused'} · {series_settings['daily_time']} · <#{series_settings['channel_id']}>"
        lines=[f"Channel: {channel.mention if channel else 'not configured'}",f"Ping role: {role.mention if role else 'none'}",f"Next daily check: {schedule}",f"Timezone: {c.TZ.key}",f"Last daily post: {last['day'] or 'none'}",f"Movie night: #{night['id']} · {night['state']}" if night else 'Movie night: none',f"Last successful release scan: {alert or 'none since restart'}",f"Daily worker: {'running' if c.daily_job.is_running() else 'stopped'}",f"Movie-night worker: {'stopped' if bot.workflow_task.done() else 'running'}",'Missing permissions: '+(', '.join(missing) or 'none detected')]
        lines.append('Daily series: '+series_status)
        if hasattr(bot,'series_task'):
            lines.append('Series worker: '+('stopped' if bot.series_task.done() else 'running'))
        await i.followup.send(embed=discord.Embed(title='Teatro admin status',description='\n'.join(lines)),ephemeral=True)
    bot.tree.add_command(admin)

    @bot.tree.command(name='privacy',description='See what Teatro stores')
    async def privacy(i:discord.Interaction):
        await i.response.send_message('Teatro stores Discord IDs, film preferences, services, film and series watchlists, ratings, watched status, movie-night votes, RSVPs and reviews. Shared film watchlists and posted reviews are visible in the server; `/mylist` and series watchlists are private. It does not read message content for recommendations. `/delete-my-data confirm:True` removes your stored member records across servers, including series activity. Discord messages, roles and previously created backups are not removed by this command.',ephemeral=True)

    @bot.tree.command(name='delete-my-data',description='Delete your stored member preferences and activity across servers')
    async def delete_data(i:discord.Interaction,confirm:bool=False):
        if not confirm:return await i.response.send_message('Use confirm:True to delete your stored member data.',ephemeral=True)
        await i.response.defer(ephemeral=True)
        async with bot.nights.lock, c.RATING_LOCK, c.WRITE_LOCK:
            try:
                for table in ('votes','ratings','watched','watchlist','services','poll_votes','night_votes','rsvps','reviews','preferences','notifications','private_watchlist','curated','trivia_scores'):
                    await c.DB.execute(f'DELETE FROM {table} WHERE user_id=?',(i.user.id,))
                for table in ('series_ratings','series_watched','series_watchlist'):
                    exists=await c.DB.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,))
                    if await exists.fetchone():
                        await c.DB.execute(f'DELETE FROM {table} WHERE user_id=?',(i.user.id,))
                await c.DB.execute('UPDATE nominations SET user_id=0 WHERE user_id=?',(i.user.id,))
                await c.DB.execute('UPDATE nights SET host_id=0 WHERE host_id=?',(i.user.id,))
                await c.DB.commit()
            except Exception:
                await c.DB.rollback();raise
        await i.followup.send('Your stored member data was deleted. Existing Discord messages and backups are unaffected.',ephemeral=True)

    await c.DB.executescript('''
CREATE TABLE IF NOT EXISTS private_watchlist(guild_id INTEGER,user_id INTEGER,tmdb_id INTEGER,title TEXT,PRIMARY KEY(guild_id,user_id,tmdb_id));
CREATE TABLE IF NOT EXISTS themes(guild_id INTEGER PRIMARY KEY,label TEXT,genre TEXT);
CREATE TABLE IF NOT EXISTS curated(guild_id INTEGER,tmdb_id INTEGER,user_id INTEGER,title TEXT,PRIMARY KEY(guild_id,tmdb_id));
CREATE TABLE IF NOT EXISTS trivia_scores(guild_id INTEGER,user_id INTEGER,season TEXT,wins INTEGER DEFAULT 0,PRIMARY KEY(guild_id,user_id,season));
''')
    await c.DB.commit()

    personal=app_commands.Group(name='mylist',description='Your private watchlist',guild_only=True)
    @personal.command(name='add',description='Privately save a film')
    async def private_add(i:discord.Interaction,title:str):
        await i.response.defer(ephemeral=True)
        mid=await c.resolve_movie_id(title)
        movie=await bot.api.details(mid) if mid else {}
        if not movie.get('id'):return await i.followup.send('Film not found.',ephemeral=True)
        await c.run('INSERT OR IGNORE INTO private_watchlist VALUES(?,?,?,?)',(i.guild_id,i.user.id,mid,movie['title'][:100]))
        await i.followup.send('Saved to your private list; this does not subscribe you to public release alerts.',ephemeral=True)
    private_add.autocomplete('title')(c.title_autocomplete)
    @personal.command(name='view',description='View a page of your private watchlist')
    async def private_view(i:discord.Interaction,page:app_commands.Range[int,1,10000]=1):
        rows=await c.q_all('SELECT title FROM private_watchlist WHERE guild_id=? AND user_id=? ORDER BY title,tmdb_id LIMIT 15 OFFSET ?',(i.guild_id,i.user.id,(page-1)*15))
        await i.response.send_message(f'**Private watchlist · page {page}**\n'+'\n'.join(r['title'] for r in rows) if rows else 'No films on this page.',ephemeral=True)
    @personal.command(name='remove',description='Remove a film from your private watchlist')
    async def private_remove(i:discord.Interaction,title:str):
        if not title.isdigit():return await i.response.send_message('Choose a film from autocomplete.',ephemeral=True)
        await c.run('DELETE FROM private_watchlist WHERE guild_id=? AND user_id=? AND tmdb_id=?',(i.guild_id,i.user.id,int(title)))
        await i.response.send_message('Removed if present.',ephemeral=True)
    @private_remove.autocomplete('title')
    async def private_ac(i,current):
        rows=await c.q_all('SELECT tmdb_id,title FROM private_watchlist WHERE guild_id=? AND user_id=? ORDER BY title',(i.guild_id,i.user.id))
        return [app_commands.Choice(name=r['title'][:100],value=str(r['tmdb_id'])) for r in rows if current.lower() in r['title'].lower()][:25]
    bot.tree.add_command(personal)

    @bot.tree.command(name='theme',description='Admin: set a film-club theme and optional genre; leave name empty to clear')
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(genre=c.GENRE_CHOICES)
    async def theme(i:discord.Interaction,name:app_commands.Range[str,1,80]=None,genre:str=None):
        if name:await c.run('INSERT OR REPLACE INTO themes VALUES(?,?,?)',(i.guild_id,name,genre))
        else:await c.run('DELETE FROM themes WHERE guild_id=?',(i.guild_id,))
        await i.response.send_message('Theme updated. Its genre applies to daily picks.',ephemeral=True)

    @bot.tree.command(name='curate',description='Suggest a member-curated pick for hosts to consider')
    @app_commands.guild_only()
    async def curate(i:discord.Interaction,title:str):
        await i.response.defer(ephemeral=True)
        mid=await c.resolve_movie_id(title)
        movie=await bot.api.details(mid) if mid else {}
        if not movie.get('id'):return await i.followup.send('Film not found.',ephemeral=True)
        count=await c.q_one('SELECT COUNT(*) n FROM curated WHERE guild_id=? AND user_id=?',(i.guild_id,i.user.id))
        if count['n']>=5:return await i.followup.send('You already have five suggestions. Ask a host to use a pick first.',ephemeral=True)
        await c.run('INSERT OR IGNORE INTO curated VALUES(?,?,?,?)',(i.guild_id,mid,i.user.id,movie['title'][:100]))
        await i.followup.send('Suggested for the host’s curated picks.',ephemeral=True)
    curate.autocomplete('title')(c.title_autocomplete)

    @bot.tree.command(name='curated',description='Browse member-curated suggestions or publish one as a host')
    @app_commands.guild_only()
    async def curated(i:discord.Interaction,movie_id:int=None,page:app_commands.Range[int,1,10000]=1):
        await i.response.defer(ephemeral=True)
        if movie_id is not None:
            try:await bot.nights.require_host(i)
            except ValueError as e:return await i.followup.send(str(e),ephemeral=True)
            row=await c.q_one('SELECT * FROM curated WHERE guild_id=? AND tmdb_id=?',(i.guild_id,movie_id))
            if not row:return await i.followup.send('Suggestion not found.',ephemeral=True)
            card=await c.make_card(movie_id,heading='🎞️ Member-curated pick')
            if not card:return await i.followup.send('Could not load this film; try again.',ephemeral=True)
            await c.deliver(i.channel,card,guild_id=i.guild_id,kind='curated')
            await c.run('DELETE FROM curated WHERE guild_id=? AND tmdb_id=?',(i.guild_id,movie_id))
            return await i.followup.send('Published.',ephemeral=True)
        rows=await c.q_all('SELECT * FROM curated WHERE guild_id=? ORDER BY title LIMIT 15 OFFSET ?',(i.guild_id,(page-1)*15))
        await i.followup.send('\n'.join(f"{r['tmdb_id']}: {r['title']}" for r in rows) or 'No suggestions on this page.',ephemeral=True)

    @bot.tree.command(name='triviaranks',description='See the current monthly trivia season standings')
    @app_commands.guild_only()
    async def trivia_ranks(i:discord.Interaction):
        season=c.today().strftime('%Y-%m')
        rows=await c.q_all('SELECT user_id,wins FROM trivia_scores WHERE guild_id=? AND season=? ORDER BY wins DESC,user_id LIMIT 10',(i.guild_id,season))
        await i.response.send_message(f'**Trivia season {season}**\n'+'\n'.join(f"<@{r['user_id']}>: {r['wins']} wins" for r in rows) if rows else 'No trivia wins this month.',allowed_mentions=discord.AllowedMentions.none())

    @bot.tree.command(name='wrapped',description='Your private year in films')
    @app_commands.guild_only()
    async def wrapped(i:discord.Interaction,year:app_commands.Range[int,2000,2100]=None):
        year=year or c.today().year
        args=(i.guild_id,i.user.id,f'{year}-01-01',f'{year+1}-01-01')
        seen=await c.q_one('SELECT COUNT(*) n FROM watched WHERE guild_id=? AND user_id=? AND created_at>=? AND created_at<?',args)
        liked=await c.q_one("SELECT COUNT(*) n FROM ratings WHERE guild_id=? AND user_id=? AND created_at>=? AND created_at<? AND vote='up'",args)
        trivia=await c.q_one('SELECT COALESCE(SUM(wins),0) n FROM trivia_scores WHERE guild_id=? AND user_id=? AND season LIKE ?',(i.guild_id,i.user.id,f'{year}-%'))
        await i.response.send_message(f"🎬 **Your Teatro {year}**\n{seen['n']} films marked watched\n{liked['n']} films loved\n{trivia['n']} trivia wins\nBased on your currently stored activity.",ephemeral=True)
