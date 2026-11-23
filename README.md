# offhours

Custom Discord community bot for offhours. Python 3.11+ and discord.py 2.x.

## Local Windows start
1. Download/clone the repository.
2. Copy `.env.example` to `.env`. Set `DISCORD_TOKEN`, `GUILD_ID`, and `OWNER_ID`. Keep `.env` private.
3. Run `start.bat` (or `python -m pip install -r requirements.txt` then `python bot.py`).
4. Ensure the bot has Message Content and Server Members privileged intents enabled.
5. In Discord, as the configured owner, send `?setup` to apply channels, roles, verification, panels and slowmodes. Restarting the bot alone doesn't rebuild the server. Rerunning `?setup` replaces the display panels without a visible `(edited)` label.

## Commands
- Owner: `?setup`, `?testwelcome`, `?say #channel your text`
- Staff: `?purge 10`, `?warn`, `?warnings`, `?timeout`, `?kick`, `?ban`, `?lock`, `?unlock`
- Members: `?rank`, `?leaderboard`, `?help`

## Behaviour
- Join panels and level-up notices in `#welcome`, member-avatar thumbnail, optional banner URL.
- Verify button assigns `regular` and removes `pending`; rules and verify are hidden for `regular` members.
- XP levels 0–150, award roles every five levels; starter colours until level 5; user rank and leaderboard.
- Discord slowmode: `#general` 1 second, `#media` 15 seconds, `#clips` 10 seconds, `#post-your-setup` 1 hour.
- Setup posts receive preset reactions in the order fire, heart, skull, cross. Discord controls actual display order.
- `?say` posts a message from the bot only when requested by the configured server owner; it suppresses everyone/role/user pings.
- Disable Maki's XP and join messages to prevent duplicates. Maki must be authorized separately.

## Cloud hosting / Kerit
Choose Python bot runtime. If the host supports Git repo deployment, link this repo and use `python bot.py` as startup command. Install dependencies from `requirements.txt`. Add the `.env.example` values as **host environment variables**, not a public GitHub file. Do **not** store tokens in the repository. Ensure only one bot instance is running (stop your Windows copy before starting cloud copy).

**Critical:** `offhours.sqlite3` stores levels, warnings, and panel identifiers. Free hosts can reset their filesystem. Use persistent disk storage and set `OFFHOURS_DB_PATH` to that mounted path, if provided. Copy your existing SQLite DB from Windows to host if preserving accumulated XP matters. GitHub is not a database; never commit SQLite or secrets.

Kerit's free plan presently requires a daily heartbeat and its onboarding checks; check the host dashboard for renewal requirements. A Git push only triggers an automatic redeployment if the host actually has Git-based deploys enabled.

## Notes / limits
- No direct ChatGPT → Discord live control is connected. ChatGPT can edit GitHub code; use owner-only `?say` for live announcements.
- Native Discord onboarding must be configured in Discord Server Settings; verification alone is not full native onboarding.
- The 256 MB host plan may be tight depending on active servers, dependency versions and image processing.
- For security, grant the bot only needed permissions once setup is complete.
