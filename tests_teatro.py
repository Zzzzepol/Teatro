"""Test Teatro without connecting to Discord."""
import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import aiosqlite
import Teatro_Bot as core
from teatro_workflows import migrate, Nights, install

class RegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        core.DB=await aiosqlite.connect(':memory:')
        core.DB.row_factory=aiosqlite.Row
        await core.DB.executescript(core.SCHEMA)
        await migrate(core.DB)
        self.member_bot=core.FilmBot()
        from teatro_members import install as members
        await members(self.member_bot,core)
        self.service=Nights(SimpleNamespace(),core)

    async def asyncTearDown(self):
        await core.DB.close()
        await core.commands.Bot.close(self.member_bot)

    async def test_migration_deduplicates_and_preserves_seen(self):
        await core.run('DELETE FROM migrations')
        for row in [(1,10,1,100,'up','2026-01-01'),(2,10,1,100,'down','2026-02-01'),(3,10,1,100,'seen','2026-03-01')]:
            await core.run('INSERT INTO votes VALUES(?,?,?,?,?,?)',row)
        await migrate(core.DB)
        await migrate(core.DB)
        self.assertEqual((await core.q_one('SELECT COUNT(*) n FROM ratings'))['n'],1)
        self.assertEqual((await core.q_one('SELECT vote FROM ratings'))['vote'],'down')
        self.assertEqual((await core.q_one('SELECT COUNT(*) n FROM watched'))['n'],1)

    async def test_filters_survive_cinema_and_mood(self):
        p=core.build_params('cinema',genre='35',language='ja',max_runtime=120,mood='short')
        self.assertEqual(p['with_original_language'],'ja')
        self.assertEqual(p['with_runtime.lte'],100)
        self.assertEqual(p['with_genres'],'35')

    async def test_winner_tie_and_restart(self):
        start=(self.service.now()+timedelta(days=1)).isoformat()
        await core.run("INSERT INTO nights(id,guild_id,state,start) VALUES(1,1,'voting',?)",(start,))
        for mid in [200,100]:
            await core.run('INSERT INTO nominations VALUES(?,?,?,?,?)',(1,mid,str(mid),10,90))
        n=await self.service.active(1)
        await self.service.close_vote(n)
        restarted=Nights(SimpleNamespace(),core)
        saved=await restarted.active(1)
        self.assertEqual(saved['winner'],100)
        self.assertEqual(saved['state'],'scheduling')

    async def test_failed_alert_remains_retryable(self):
        await core.run('INSERT INTO settings VALUES(1,2,NULL)')
        await core.run("INSERT INTO watchlist VALUES(1,10,100,'Film','2026-01-01')")
        old=core.bot
        channel=SimpleNamespace(send=AsyncMock(side_effect=core.discord.Forbidden(SimpleNamespace(status=403,reason='Forbidden'), 'denied')))
        core.bot=SimpleNamespace(get_channel=lambda _:channel,api=SimpleNamespace(details=AsyncMock(return_value={'id':100,'watch/providers':{'results':{'PH':{'flatrate':[{'provider_id':1,'provider_name':'Provider'}]}}}})))
        try:
            await core.run_release_alerts()
            self.assertIsNone(await core.q_one('SELECT * FROM notified'))
            channel.send.side_effect=None
            await core.run_release_alerts()
            self.assertIsNotNone(await core.q_one('SELECT * FROM notified'))
        finally:core.bot=old

    async def test_ineligible_nomination(self):
        with self.assertRaises(ValueError):
            await self.service.eligible({'max_runtime':90,'language':None,'platform':None},{'id':1,'runtime':120})

    async def test_unique_active_night(self):
        await core.run("INSERT INTO nights(guild_id,state) VALUES(1,'nominating')")
        with self.assertRaises(aiosqlite.IntegrityError):
            await core.run("INSERT INTO nights(guild_id,state) VALUES(1,'voting')")

    async def test_schedule_recovers_existing_discord_event(self):
        start=(self.service.now()+timedelta(days=1)).isoformat()
        await core.run("INSERT INTO nights(id,guild_id,channel_id,state,start,winner) VALUES(1,1,2,'scheduling',?,100)",(start,))
        await core.run("INSERT INTO nominations VALUES(1,100,'Film',10,90)")
        event=SimpleNamespace(id=55,description='Teatro movie night #1',url='https://discord.com/events/1/55')
        guild=SimpleNamespace(fetch_scheduled_events=AsyncMock(return_value=[event]),create_scheduled_event=AsyncMock())
        service=Nights(SimpleNamespace(get_guild=lambda _:guild),core)
        service.announce=AsyncMock()
        await service.schedule(await service.active(1))
        guild.create_scheduled_event.assert_not_awaited()
        row=await service.active(1)
        self.assertEqual(row['state'],'scheduled')
        self.assertEqual(row['event_id'],55)

    async def test_daily_restart_does_not_repeat_post(self):
        await core.run('INSERT INTO settings VALUES(1,2,NULL)')
        await core.run("INSERT INTO controls VALUES(1,0,'00:00')")
        original_post,original_alerts=core.post_daily,core.run_release_alerts
        core.post_daily=AsyncMock(return_value=True)
        core.run_release_alerts=AsyncMock()
        try:
            await core.run_daily_jobs()
            await core.run_daily_jobs()
            self.assertEqual(core.post_daily.await_count,1)
        finally:
            core.post_daily,core.run_release_alerts=original_post,original_alerts

    async def test_seen_does_not_replace_rating_and_repeated_cards_deduplicate(self):
        for message in (1,2):
            await core.run("INSERT INTO posts VALUES(?,1,2,100,'Film','{}',0,'manual','2026-01-01')",(message,))
        i=SimpleNamespace(message=SimpleNamespace(id=1),user=SimpleNamespace(id=10),response=SimpleNamespace(defer=AsyncMock()),edit_original_response=AsyncMock())
        await core._vote_cb('up')(i)
        await core._vote_cb('seen')(i)
        self.assertEqual(await core.vote_counts(1),{'up':1,'seen':1})
        i.message.id=2
        await core._vote_cb('down')(i)
        self.assertEqual(await core.vote_counts(1),{'down':1,'seen':1})

    async def test_member_data_deletion(self):
        await core.run("INSERT INTO private_watchlist VALUES(1,10,100,'Private')")
        await core.run("INSERT INTO ratings VALUES(1,10,100,'up','2026-01-01')")
        await core.run("INSERT INTO trivia_scores VALUES(1,10,'2026-01',3)")
        self.member_bot.nights=self.service
        i=SimpleNamespace(user=SimpleNamespace(id=10),response=SimpleNamespace(defer=AsyncMock()),followup=SimpleNamespace(send=AsyncMock()))
        await self.member_bot.tree.get_command('delete-my-data').callback(i,True)
        for table in ('private_watchlist','ratings','trivia_scores'):
            self.assertEqual((await core.q_one(f'SELECT COUNT(*) n FROM {table}'))['n'],0)

    async def test_cinema_window_excludes_past_and_uses_upcoming_rerelease(self):
        def movie(mid, title, releases, region=None):
            return {'id': mid, 'title': title, 'release_dates': {'results': [
                {'iso_3166_1': region or core.REGION, 'release_dates': releases}]}}
        def release(day, kind=3):
            return {'release_date': day+'T00:00:00.000Z', 'type': kind}
        films = [
            movie(1, 'Past only', [release('2024-01-01')]),
            movie(2, 'Returning film', [release('2024-01-01'), release('2026-11-01')]),
            movie(3, 'Today film', [release('2026-10-30')]),
            movie(4, 'Too late', [release('2027-01-01')]),
            movie(5, 'Digital only', [release('2026-11-01', 4)]),
            movie(6, 'Wrong region', [release('2026-11-01')], 'ZZ'),
            movie(7, 'Invalid date', [release('invalid')]),
        ]
        api = SimpleNamespace(tmdb=AsyncMock(return_value={'results': [{'id': d['id']} for d in films]}),
                              details=AsyncMock(side_effect=films))
        with patch.object(core, 'bot', SimpleNamespace(api=api)), patch.object(core, 'today', return_value=date(2026,10,30)):
            embed = await core.cinema_embed('Coming soon', date(2026,10,30), date(2026,12,29), True)
        self.assertIn('opens 30 Oct 2026', embed.description)
        self.assertIn('re-release 01 Nov 2026', embed.description)
        for excluded in ('Past only','Too late','Digital only','Wrong region','Invalid date','2024'):
            self.assertNotIn(excluded, embed.description)
        self.assertLess(embed.description.index('Today film'), embed.description.index('Returning film'))

    async def test_upcoming_excludes_old_films_even_with_future_ph_dates(self):
        start, end = date(2026,10,30), date(2028,10,30)
        def film(primary, status='Post Production', historical=None):
            regions = [{'iso_3166_1': core.REGION, 'release_dates': [
                {'type': 3, 'release_date': '2026-11-11T00:00:00Z'}]}]
            if historical:
                regions.append({'iso_3166_1': 'US', 'release_dates': [
                    {'type': 3, 'release_date': historical}]})
            return {'release_date': primary, 'status': status, 'release_dates': {'results': regions}}
        self.assertIsNone(core.upcoming_release(film('2021-09-15'), start, end))
        self.assertIsNone(core.upcoming_release(film('2024-02-28'), start, end))
        self.assertIsNone(core.upcoming_release(film('2026-12-18', 'Released'), start, end))
        self.assertIsNone(core.upcoming_release(film('2026-12-18', historical='2024-02-28'), start, end))
        self.assertIsNone(core.upcoming_release(film(''), start, end))
        self.assertIsNone(core.upcoming_release(film('2029-01-01'), start, end))
        self.assertEqual(core.upcoming_release(film('2026-11-11'), start, end)[0], date(2026,11,11))
        distant = {'release_date': '2028-07-01', 'status': 'In Production'}
        self.assertEqual(core.upcoming_release(distant, start, end),
                         (date(2028,7,1), f'Listed release · {core.REGION} date unconfirmed'))

    async def test_upcoming_browses_pages_without_requiring_ph_dates(self):
        movies = [{'id': 1, 'title': 'Old movie', 'release_date': '2024-01-01'},
                  {'id': 2, 'title': 'Future sequel', 'release_date': '2027-12-01'}]
        api = SimpleNamespace(tmdb=AsyncMock(return_value={'results':movies,'total_pages':3}),
                              details=AsyncMock(side_effect=movies))
        with patch.object(core, 'bot', SimpleNamespace(api=api)):
            embed = await core.upcoming_embed(date(2026,10,30), date(2028,10,30), 2, 'popularity.desc')
        self.assertNotIn('Old movie', embed.description)
        self.assertIn('Future sequel', embed.description)
        self.assertIn('date unconfirmed', embed.description)
        self.assertIn('Page 2/3', embed.footer.text)
        params = api.tmdb.call_args.args[1]
        self.assertEqual(params['page'], 2)
        self.assertNotIn('region', params)
        self.assertEqual(params['primary_release_date.gte'], '2026-10-30')

    async def test_rerelease_requires_regional_theatrical_evidence(self):
        start, end = date(2026,10,30), date(2028,10,30)
        movie = {'release_date': '2024-02-28', 'status': 'Released'}
        self.assertIsNone(core.regional_future_screening(movie, start, end))
        region = {'iso_3166_1': 'ZZ', 'release_dates': [{'type':3,'release_date':'2026-11-11'}]}
        movie['release_dates'] = {'results': [region]}
        self.assertIsNone(core.regional_future_screening(movie, start, end))
        region['iso_3166_1'] = core.REGION
        self.assertEqual(core.regional_future_screening(movie, start, end), (date(2026,11,11), f'First {core.REGION} theatrical listing'))
        region['release_dates'].insert(0, {'type':3,'release_date':'2024-02-28'})
        self.assertEqual(core.regional_future_screening(movie, start, end), (date(2026,11,11), 'Re-release'))
        region['release_dates'].pop(0)
        region['release_dates'][0]['type'] = 4
        self.assertIsNone(core.regional_future_screening(movie, start, end))
        region['release_dates'][0] = {'type':3,'release_date':'2024-02-28'}
        self.assertIsNone(core.regional_future_screening(movie, start, end))

    async def test_register_workflow_commands(self):
        bot=core.FilmBot()
        try:
            await install(bot,core)
            names={c.name for c in bot.tree.get_command('movienight').commands}
            self.assertTrue({'create','nominate','vote','review','cancel','reschedule'} <= names)
            bot.workflow_task.cancel()
            try:await bot.workflow_task
            except BaseException:pass
        finally:
            await core.commands.Bot.close(bot)

if __name__=='__main__':unittest.main()
