# Offhours

Public, multi-server Discord bot using Python 3.12 and discord.py. Every server keeps its own settings, XP, moderation records, panels, tickets and music session. Existing SQLite XP tables are retained.

## Kerit Cloud Bot Drift

1. Upload/link this repository and select a complete **Python 3.12** runtime.
2. Set the private `DISCORD_TOKEN` host environment variable. Optional integration credentials are listed in `.env.example`; missing optional credentials do not prevent startup.
3. Use **`python main.py`** as the startup command (`sh start.sh` also works). It installs pinned packages into a private `.venv` on the host, including FFmpeg's packaged executable, PyNaCl, Discord DAVE encryption support, yt-dlp, its JavaScript solver and Deno. Root access or member-side installation is unnecessary.
4. Enable Server Members and Message Content privileged intents in the Discord Developer Portal. Invite the application with `bot` and `applications.commands` scopes and the permissions needed for the features you enable. Voice needs View Channel, Connect and Speak; player messages also need Send Messages and Embed Links.
5. Keep the database on persistent host storage. Preserve your existing `offhours.sqlite3` and its configured path when updating. Set `OFFHOURS_DB_PATH` if the host provides a persistent mount. Use the hosting supervisor's restart-on-crash option; run only one bot process per database/token.
6. In each server run `/setup` or `?setup`. Select existing channels/roles, then enable individual features. Setup does not create, rename, move or delete the server's channels, categories or roles.

The repository Dockerfile is an alternative when custom Docker images are supported. It runs as an unprivileged user and stores the database under `/app/data`; mount that directory persistently. The running Kerit container's Python version, writable storage, process execution, HTTPS access and outbound Discord voice UDP still depend on the purchased host configuration. No deployment or audible playback is claimed by this repository update.

## Existing Offhours server

Keep the existing database. An existing `GUILD_ID` is used only for one-time migration of the old server's resolved channel/role IDs and saved settings into a separate server-specific preset. It does not restrict operation to that server. Ambiguous names are left unconfigured. `OWNER_ID` is no longer a universal administrator override: each server controls its own permissions. Existing objects and the server icon are not changed by migration. See `presets/offhours.json`.

## Core interface

- `/help`: browse the actually registered commands and their descriptions. Discord autocomplete shows parameters.
- `/setup`, `/config`: per-server modules, existing object selectors, command permissions, prefix/timezone, configuration export/import and stored backups.
- `/chatgpt prompt:`: propose one or more allowed Discord actions, inspect a private preview/full proposal, then Approve, Edit, Regenerate or Cancel. Actions use fixed handlers and recheck the approving member's actual permissions. API credentials stay on the host; usage is separately billed by OpenAI. Server-owner-only by default, with an explicit administrator opt-in in `/ai-config`.
- Dedicated panel editors: `/setup-join-panel`, `/setup-leave-panel`, `/setup-rules-panel`, `/setup-verify-panel`, `/setup-level-panel`, `/setup-tickets`, `/setup-invite-panel`, `/setup-roles`, `/setup-reaction-roles`, `/setup-boost-panel`, `/setup-live-alerts`, `/setup-video-alerts`, plus community/log panel editors. Saved configuration loads into a private preview with controls, text modals, channel/role selectors, variables and image attachments.
- `/ticket`, `/ticket-config`: private tickets, multiple panels and category routes, custom forms, support roles, claim/close/reopen, participants, priority, notes, TXT/HTML transcripts, limits, cooldowns, working hours, reminders, automatic closing and statistics. An existing category must be explicitly selected; opening a ticket deliberately creates only its private ticket channel. Transcript archives must remain private.
- Quick moderation slash/prefix commands and `/mod`, `/security`, `/logs`: runtime permission/hierarchy checks, persistent cases, warnings, timeouts, tempbans, seven-day-safe purge filters, lock restoration, opt-in filters and event logging. Staff log messages suppress pings.
- `/play query:` and `/music`: streaming song search/URLs, queue controls, previous/pause/resume/skip/stop/shuffle/loop/seek/volume, a public player in voice chat, per-server DJ roles and level requirement. Expired sources are resolved when playback starts. Spotify supplies metadata matched to playable sources; it does not provide Spotify audio or guarantee an identical recording. Access restrictions are not bypassed. Progressive public HTTP(S) direct audio is supported; playlist manifests and private-network URLs are rejected. Queue exhaustion disconnects after an idle timeout. Volume/seek reopen the stream at the selected position, so a short buffering pause is possible.
- `/levels`: existing XP, configurable cooldown/multiplier, anti-AFK voice XP, existing-role milestone rewards, rank cards and server-scoped XP backup/import.
- `/role selfrole`, `/colour`, `/region`, `/notifications`: existing-role selections and persistent role menus.
- `/invites`: best-effort attribution and invite management. Ambiguous or pre-tracking joins remain unknown.
- `/suggest`, `/suggestion`, `/giveaway`, `/poll`, `/starboard`: persistent voting, timed giveaways, native Discord polls and privacy-aware starred-message reposting.
- `/social`: official Twitch and YouTube notification monitoring, separate roles/destinations, event persistence and a short live-alert batching window. YouTube live search is quota-limited and cached for thirty minutes. TikTok generic LIVE monitoring is not exposed by its public Display API; it requires the explicitly configured supported provider adapter described below. Accounts are selected by administrators, never guessed.
- `/onboarding`: reviewed native Community/onboarding configuration where Discord permits it, with a working existing-role menu alternative. Discord eligibility/default-channel requirements still apply; unsupported Server Guide editing is explained in the command.
- `/analytics`: opt-in observed activity counts, never invented historical statistics.
- `/safety-check` and panel test commands: invoked by an administrator only. They never run automatically at startup. Readiness checks and packet counters do not establish that a listener heard music.

Existing information, utility, role and channel commands remain available. This update prioritizes implemented core systems; it does not claim the original 400-command brief is complete.

## TikTok provider adapter

Set a trusted HTTPS `TIKTOK_PROVIDER_URL` and optional host-only bearer `TIKTOK_PROVIDER_TOKEN`. The adapter receives `?username=...` and must return `{"events":[{"id":"stable-id","type":"live","live":true,"url":"https://www.tiktok.com/@account/live","title":"..."}]}`; uploads use `type:"video"`. Only HTTPS TikTok links are accepted. The provider must legitimately support the monitored accounts; no scraping, cookie collection or access bypass is implemented. Without this integration the bot reports monitoring as unsupported.

## Updates and backups

Stop the bot before replacing repository files. Keep the database, `.env`/host secrets and persistent storage. Restart with `python main.py`; changed pinned requirements reinstall automatically. Never upload private databases, transcripts or secrets to GitHub. For a full SQLite backup, stop the process and copy the database, or use SQLite's online backup API while running; copying only the main file during WAL writes can lose recent data. Logs rotate at 1 MiB with three backups. SIGTERM/SIGINT stop services and voice workers before closing SQLite.

Runtime/live testing is left to the owner as requested. Startup, command registration and generated player messages are not a release claim of audible playback.

Reference APIs: [discord.py](https://discordpy.readthedocs.io/en/stable/), [Discord onboarding](https://docs.discord.com/developers/resources/guild#modify-guild-onboarding), [OpenAI Responses](https://platform.openai.com/docs/api-reference/responses), [yt-dlp JavaScript support](https://github.com/yt-dlp/yt-dlp/wiki/EJS), [Twitch API](https://dev.twitch.tv/docs/api/), [YouTube Data API](https://developers.google.com/youtube/v3).
