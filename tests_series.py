"""Exercise series features without Discord or API connections."""
import asyncio
import unittest
from datetime import date
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
from PIL import Image
import Teatro_Bot as core
from series_chart import episode_score, render_chart
from teatro_series import SCHEMA, SeriesService, SeriesView, install, series_embed


def show_data():
    return {'id': 100, 'name': 'Sample series', 'first_air_date': '2020-01-01',
            'overview': 'A sample synopsis.', 'vote_average': 8.2, 'vote_count': 100,
            'status': 'Ended', 'seasons': [{'season_number': 0}, {'season_number': 1}],
            'aggregate_credits': {'cast': [{'name': 'Sample Actor', 'roles': [{'character': 'Alex'}]}]},
            'watch/providers': {'results': {'PH': {'flatrate': [{'provider_id': 8, 'provider_name': 'Netflix'}]}}}}


class ChartTests(unittest.TestCase):
    def test_scores_require_air_date_and_votes(self):
        episode = {'air_date': '2020-01-01', 'vote_average': 8.4, 'vote_count': 10}
        today = date(2026, 10, 31)
        self.assertEqual(episode_score(episode, today), 8.4)
        for changes in ({'air_date': '2027-01-01'}, {'air_date': None}, {'vote_count': 0}, {'vote_average': 11}):
            self.assertIsNone(episode_score({**episode, **changes}, today))
        self.assertEqual(episode_score({**episode, 'vote_average': 0}, today), 0)

    def test_chart_handles_sparse_episodes_and_short_seasons(self):
        episodes = [{'episode_number': n, 'air_date': '2020-01-01', 'vote_count': 10, 'vote_average': 8.2} for n in (1, 3, 31)]
        for page in (1, 2):
            png = render_chart(show_data(), [{'season_number': 3, 'episodes': episodes}], date(2026, 10, 31), episode_page=page)
            with Image.open(BytesIO(png)) as image:
                self.assertEqual(image.format, 'PNG')
                self.assertGreaterEqual(image.height, 620)
                self.assertEqual(image.width, 900)

    def test_card_includes_cast_synopsis_and_regional_services(self):
        embed = series_embed(show_data(), 'PH', 'Daily series')
        fields = {f.name: f.value for f in embed.fields}
        self.assertEqual(embed.description, 'A sample synopsis.')
        self.assertIn('Sample Actor — Alex', fields['Cast'])
        self.assertIn('Netflix', fields['Where to watch (PH)'])
        self.assertIn('JustWatch', embed.footer.text)
        self.assertLess(len(embed), 6000)
        self.assertIn('No regional availability', series_embed(show_data(), 'US', 'Series').fields[-1].value)


class SeriesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        core.DB = await aiosqlite.connect(':memory:')
        core.DB.row_factory = aiosqlite.Row
        await core.DB.executescript(core.SCHEMA + SCHEMA)
        from teatro_workflows import migrate
        await migrate(core.DB)
        self.channel = SimpleNamespace(id=2, send=AsyncMock(return_value=SimpleNamespace(id=9)))
        self.bot = SimpleNamespace(api=SimpleNamespace(tmdb=AsyncMock()), get_channel=lambda _: self.channel)
        self.service = SeriesService(self.bot, core)
        self.settings = {'guild_id': 1, 'channel_id': 2, 'role_id': None}

    async def asyncTearDown(self):
        await core.DB.close()

    async def test_daily_dedupes_across_restart_without_affecting_films(self):
        self.service.pick = AsyncMock(return_value=show_data())
        self.service.chart = AsyncMock(side_effect=ValueError('No episode data yet.'))
        self.assertTrue(await self.service.daily(self.settings))
        restarted = SeriesService(self.bot, core)
        self.assertFalse(await restarted.daily(self.settings))
        self.channel.send.assert_awaited_once()
        self.assertIsNone(await core.q_one('SELECT * FROM job_runs'))
        self.assertIsNotNone(await core.q_one('SELECT * FROM series_posts'))

    async def test_failed_post_can_retry(self):
        self.service.pick = AsyncMock(return_value=show_data())
        self.service.chart = AsyncMock(side_effect=ValueError('No episode data yet.'))
        self.channel.send.side_effect = RuntimeError('Offline')
        with self.assertRaises(RuntimeError):
            await self.service.daily(self.settings)
        self.assertIsNone(await core.q_one('SELECT * FROM series_daily_runs'))
        self.channel.send.side_effect = None
        self.assertTrue(await self.service.daily(self.settings))

    async def test_buttons_persist_and_seen_does_not_replace_rating(self):
        await core.run("INSERT INTO series_posts VALUES(9,1,2,100,'Sample','manual','2026-01-01')")
        view = SeriesView(self.service)
        self.assertTrue(view.is_persistent())
        interaction = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=3), message=SimpleNamespace(id=9),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        for action in ('up', 'seen', 'save'):
            await view.callback(action)(interaction)
        self.assertEqual((await core.q_one('SELECT vote FROM series_ratings'))['vote'], 'up')
        self.assertIsNotNone(await core.q_one('SELECT * FROM series_watched'))
        self.assertIsNotNone(await core.q_one('SELECT * FROM series_watchlist'))
        self.assertIsNone(await core.q_one('SELECT * FROM watched'))
        await view.callback('seen')(interaction)
        self.assertIsNone(await core.q_one('SELECT * FROM series_watched'))
        self.assertIsNotNone(await core.q_one('SELECT * FROM series_ratings'))

    async def test_chart_rejects_partial_seasons_and_excludes_specials(self):
        self.bot.api.tmdb.return_value = {}
        with self.assertRaisesRegex(ValueError, 'temporarily unavailable'):
            await self.service.chart(show_data())
        self.bot.api.tmdb.assert_awaited_once_with('/tv/100/season/1', {'language': 'en-US'})

    async def test_chart_attachment_and_mentions(self):
        self.service.chart = AsyncMock(return_value=(b'png', 'Season 1'))
        await self.service.send(show_data(), self.channel, 1, 'daily', role_id=99)
        kwargs = self.channel.send.call_args.kwargs
        self.assertEqual(kwargs['embed'].image.url, 'attachment://episode-ratings.png')
        self.assertEqual(kwargs['allowed_mentions'].to_dict(), {'roles': [99], 'parse': []})

    async def test_chart_pages_request_only_selected_seasons(self):
        show = {**show_data(), 'seasons': [{'season_number': n} for n in range(11)]}
        async def season_data(path, params):
            number = int(path.rsplit('/', 1)[1])
            return {'season_number': number, 'episodes': [{'episode_number': 31, 'air_date': None}]}
        self.bot.api.tmdb.side_effect = season_data
        png, note = await self.service.chart(show, 2, 2)
        self.assertTrue(png.startswith(b'\x89PNG'))
        self.assertIn('Episodes 31–60', note)
        self.assertEqual([call.args[0] for call in self.bot.api.tmdb.call_args_list], ['/tv/100/season/9', '/tv/100/season/10'])

    async def test_search_uses_series_year(self):
        self.bot.api.tmdb.return_value = {'results': [{'id': 100}]}
        self.assertEqual(await self.service.resolve('Sample series (2020)'), 100)
        self.assertEqual(self.bot.api.tmdb.call_args.args[1]['first_air_date_year'], '2020')
        self.assertEqual(self.bot.api.tmdb.call_args.args[1]['query'], 'Sample series')

    async def test_delete_data_includes_series_and_preserves_other_members(self):
        from teatro_members import install as members
        bot = core.FilmBot()
        bot.nights = SimpleNamespace(lock=asyncio.Lock())
        await members(bot, core)
        try:
            for user in (3, 4):
                await core.run('INSERT INTO series_ratings VALUES(1,?,100,?)', (user, 'up'))
                await core.run('INSERT INTO series_watched VALUES(1,?,100)', (user,))
                await core.run('INSERT INTO series_watchlist VALUES(1,?,100,?)', (user, 'Sample'))
            i = SimpleNamespace(user=SimpleNamespace(id=3), response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
            await bot.tree.get_command('delete-my-data').callback(i, True)
            for table in ('series_ratings', 'series_watched', 'series_watchlist'):
                rows = await core.q_all(f'SELECT user_id FROM {table}')
                self.assertEqual([row['user_id'] for row in rows], [4])
        finally:
            await core.commands.Bot.close(bot)

    async def test_registration_and_chart_command(self):
        bot = core.FilmBot()
        with patch.object(SeriesService, 'worker', new=AsyncMock()):
            await install(bot, core)
        try:
            names = {command.name for command in bot.tree.get_commands()}
            self.assertTrue({'series', 'seriesratings', 'seriessetup', 'seriesdaily', 'seriesnotifications', 'serieswatchlist'} <= names)
            bot.series.details = AsyncMock(return_value=show_data())
            bot.series.chart = AsyncMock(return_value=(b'png', 'Season 1'))
            i = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
            await bot.tree.get_command('seriesratings').callback(i, 'tv:100')
            self.assertEqual(i.followup.send.call_args.kwargs['file'].filename, 'episode-ratings.png')
        finally:
            bot.series_task.cancel()
            try:
                await bot.series_task
            except asyncio.CancelledError:
                pass
            await core.commands.Bot.close(bot)


if __name__ == '__main__':
    unittest.main()
