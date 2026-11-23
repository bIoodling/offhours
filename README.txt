OFFHOURS BOT — STATUS AND SETUP
Updated: 2026-11-23

IMPORTANT: This is a status document, not proof that all requested features work.

SETUP
1. Install requirements from requirements.txt.
2. Install FFmpeg separately and ensure 'ffmpeg -version' works in the same terminal.
3. Set DISCORD_TOKEN, GUILD_ID and OWNER_ID in .env.
4. Start bot.py.
5. Run ?setup to edit existing channels only. It does NOT create or remove channels or categories.
6. Run /test-safety. The command privately attaches a downloadable readme.txt with the actual checks and failures.

MUSIC
Requires yt-dlp, PyNaCl and FFmpeg. The bot supports YouTube and SoundCloud URLs subject to extraction availability and service terms. Spotify audio is NOT streamed directly. Level 50 unlocks /music in general/afk voice.

COMMUNITY / ONBOARDING
The bot cannot claim Discord Community or native Onboarding has been configured. Enable Community under Discord Server Settings > Enable Community, select rules and announcements, then configure Onboarding in Server Settings. /setup-check reports Community status. Role-selection components in the bot are not the same as Discord native onboarding.

SAFETY REPORT
/test-safety performs non-destructive checks and returns a private downloadable readme.txt. It does not post to staff channels. Tests cover permissions, role hierarchy, channel access, link detection, XP logic and music dependencies. It does not perform real joins, bans or spam.

ROLE COLOURS
Existing core roles receive muted blue/green/rose colours during ?setup. Existing level and colour roles remain.

COMMANDS
This version does NOT contain 450+ distinct implemented commands. Do not advertise unimplemented features as functional. Existing ? commands and slash commands remain; run ?help and use Discord slash autocomplete to discover them.

STARTUP
The existing startup audit and join preview may still post to Discord. The /test-safety command itself no longer posts diagnostics to staff channels.

DUPLICATES
Previous duplicated channels are not automatically deleted. Review duplicates manually before removing them, preserving channel history and permissions.
