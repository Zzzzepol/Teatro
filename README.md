# Teatro

A Python Discord film-club bot with recommendations, watchlists and persistent movie nights.

## Local setup

Use Python 3.11 or newer. Create a Discord application/bot and obtain a TMDB v3 API key. OMDb is optional.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Fill in your credentials locally; never commit .env.
python Teatro_Bot.py
```

Invite with `bot` and `applications.commands` scopes. Grant View Channels, Send Messages, Embed Links, Read Message History, Create Public Threads, Send Messages in Threads, Manage Roles, and Create Events. The bot role must be above the opt-in role. No message-content intent is needed. Audio streaming is not implemented.

Run `/setup channel:#movies role:@FilmClub`, then `/rolepanel`. Optional `GUILD_ID` enables server-scoped development command sync. `ALLOWED_GUILD_ID` restricts slash commands to your home server. Dates use `TIMEZONE` (default Asia/Manila).

## Movie nights

1. Admin: optionally assign `/movienight hostrole role:@MovieHost`.
2. Host: `/movienight create start:2026-11-07 20:00 max_runtime:150`. Choose optional platform/language from autocomplete. Start must be at least two hours ahead.
3. Members: `/movienight nominate title:...`. Two nominations per member, twenty total. Runtime, language and service constraints are enforced.
4. Host: `/movienight openvote minutes:60`. At least two nominations are required. Voting ends at the deadline, no later than five minutes before the event.
5. Members: `/movienight status`, then `/movienight vote film:...`. One vote per member; new votes replace the previous vote.
6. The worker saves the winner and creates a Discord Scheduled Event. Highest vote count wins; ties (including zero votes) use lowest TMDB ID. `/movienight close` closes voting early.
7. Members: `/movienight rsvp response:going` opts into a reminder within thirty minutes of the start. Maybe and Cannot Attend receive no reminder.
8. A spoiler discussion thread opens at the start. After the film's runtime, Going attendees can submit `/movienight review night_id:... score:... text:...`. Reviews are spoiler-hidden and update watched status and film rating.

Host commands: `/movienight reschedule`, `/movienight cancel`. One active night per server. Database state survives restarts; the worker checks every thirty seconds. If scheduling remains pending past its start (for example, permissions were missing), reschedule it to a future time. Nominations stay open until the host opens voting. Legacy polls are archived after upgrade.

## Other commands

- `/help`: onboarding.
- `/film`, `/search`, `/person`: discover and inspect films.
- `/preferences`, `/myservices`: member preferences and services. Use `/myservices search:Disney` or `/myservices search:Max` to find providers beyond the first 25; selections from previous searches are preserved. Explicit film filters override preferences; personal watched movies are excluded.
- `/watchlist add`, `/watchlist view`, `/watchlist remove`: public shared watchlist. Remove uses exact autocomplete selections.
- `/nowplaying`, `/comingsoon`: recorded regional releases, not verified showtimes. Coming Soon lists new unreleased movies, excluding old primary releases, films marked Released, and past theatrical releases worldwide. It defaults to 24 months sorted by popularity. Use `months:1`, `months:60`, `page:2`, or sort by Soonest Releases. PH theatrical dates are preferred; otherwise the listed release date is explicitly labeled PH date unconfirmed. Dates depend on TMDB and may change. A separate PH theatrical listings card requires an old original release plus an explicit future regional theatrical entry. It distinguishes first PH theatrical listings from re-releases using the film’s earlier PH theatrical history, and links to the TMDB release record. These listings are not independently verified showtimes.
- `/leaderboard`, `/recap`, `/trivia`: community ratings and activities.
- `/settings`: admin daily-post pause and local time.
- `/notifications`: release-alert ping opt-out.
- `/health`: admin scheduler/database status.
- `/privacy`, `/delete-my-data`: storage disclosure and member-data removal.

Ratings are unique per server/member/movie; Seen It is separate. Existing votes migrate automatically, preserving the latest rating and known watched status. Back up your existing database before upgrading. Old cards display refreshed totals when clicked; other copies are not immediately edited.

Daily picks catch up for the current day after downtime and avoid repeat sends after a completed run. Weekly lists and recaps track completion separately. Release alerts are marked delivered only after a successful send. As with any SQLite/Discord integration, a crash between a remote send and its database commit can still produce a duplicate. Use a single bot process with this database.

## Validation

```sh
.venv/bin/python -m unittest tests_teatro -v
```

Tests are offline and cover migration, filters, alert retry, daily deduplication, winner persistence, event recovery, nomination validation and command registration. Live Discord/API behavior still requires a test server and credentials.

## Community features

- `/mylist add / view / remove`: private watchlist with pages; no public release alerts.
- `/watchlist view page:2`: shared watchlist pagination.
- `/theme`: admin theme and genre used for daily picks; clear by omitting the name.
- `/curate`, `/curated`: members suggest films; hosts browse and publish suggestions.
- `/triviaranks`: monthly trivia seasons. One round per server per ten minutes.
- `/wrapped`: private annual watched, liked and trivia totals.
- Movie cards are compact. Details opens the full card privately; More Like This and Different Genre offer additional unseen choices.
- Movie-night status compares nominated films against Going attendees' saved streaming services and shows popular watchlist suggestions.

## Practical limits

Recommendations currently use genre/language/runtime rules, watched history and a 180-day server repeat window. Group service coverage is informational; the poll winner remains the members' vote. More Like This uses genre similarity, not an AI model. Theme changes are manual. Annual recaps use current stored records, not an immutable watch diary. Trivia rounds themselves are not restored after restart, but scores are persistent. The existing legacy core is retained while movie-night and member features are separate modules.

Hosting and deployment are intentionally deferred.
