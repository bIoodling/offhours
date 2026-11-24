"""Discord interface for the hosted music service."""
import asyncio
import contextlib
import logging
import time

import discord
from discord import app_commands

from services.music_player import Player
from services.music_resolver import Resolver
from services.configuration import administrator, enabled

log = logging.getLogger('offhours.music')


async def reply(interaction, text):
    if interaction.response.is_done():
        await interaction.followup.send(text[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    else:
        await interaction.response.send_message(text[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class MusicControls(discord.ui.View):
    def __init__(self, service):
        super().__init__(timeout=None)
        self.service = service
        for index, (label, action) in enumerate([
            ('Previous', 'previous'), ('Pause / Resume', 'toggle'), ('Skip', 'skip'),
            ('Stop', 'stop'), ('Shuffle', 'shuffle'), ('Loop', 'loop'),
            ('Queue', 'queue'), ('Volume −', 'down'), ('Volume +', 'up'),
        ]):
            button = discord.ui.Button(label=label, custom_id='offhours:music:v2:' + action,
                row=index // 5, style=discord.ButtonStyle.danger if action == 'stop' else discord.ButtonStyle.secondary)
            async def callback(interaction, action=action):
                await self.service.dispatch(interaction, action)
            button.callback = callback
            self.add_item(button)


class MusicService:
    def __init__(self, bot, get_level, get_setting, put_setting):
        self.bot = bot
        self.get_level = get_level
        self.get_setting = get_setting
        self.put_setting = put_setting
        self.resolver = Resolver()
        self.players = {}
        self.pending = set()
        self.cooldowns = {}

    def gate(self, interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            raise ValueError('Music is available to members of the configured server.')
        member = interaction.user
        if not enabled(self.get_setting,interaction.guild_id,'music'):
            raise ValueError('Music is disabled in this server. Enable it in /setup.')
        role_ids = {r.id for r in member.roles}
        dj = {int(x) for x in self.get_setting(interaction.guild_id, 'music:dj_roles', '').split(',') if x.isdecimal()}
        try:minimum=max(0,min(150,int(self.get_setting(interaction.guild_id,'music:min_level','50'))))
        except (ValueError,TypeError):minimum=50
        if not administrator(member) and not (role_ids & dj) and self.get_level(interaction.guild_id, member.id) < minimum:
            raise ValueError('You have not reached this server’s music level requirement. Server administrators and configured DJ roles have an override.')
        channel = member.voice.channel if member.voice else None
        allowed_ids = {int(x) for x in self.get_setting(interaction.guild_id, 'music:channels', '').split(',') if x.isdecimal()}
        if not isinstance(channel, discord.VoiceChannel) or not (
                channel.id in allowed_ids if allowed_ids else True):
            raise ValueError('Join an allowed voice channel first.')
        return channel

    def player(self, guild):
        player = self.players.get(guild.id)
        if not player or player.closed:
            player = Player(guild, self.resolver, lambda: MusicControls(self))
            self.players[guild.id] = player
        return player

    async def play(self, interaction, query):
        key = (interaction.guild_id, interaction.user.id)
        acquired = False
        try:
            channel = self.gate(interaction)
            permissions = channel.permissions_for(interaction.guild.me)
            if not all((permissions.view_channel, permissions.connect, permissions.speak,
                        permissions.send_messages, permissions.embed_links)):
                raise ValueError('The bot needs View Channel, Connect, Speak, Send Messages and Embed Links in this voice channel.')
            # One metadata request at a time for the guild bounds memory and playlist work.
            if interaction.guild_id in self.pending:
                raise ValueError('A song request is still resolving. Try again when it finishes.')
            if time.monotonic() - self.cooldowns.get(key, 0) < 5:
                raise ValueError('Wait five seconds between song requests.')
            self.cooldowns[key] = time.monotonic()
            if len(self.cooldowns) > 1024:
                now = time.monotonic()
                self.cooldowns = {k: v for k, v in self.cooldowns.items() if now - v < 60}
            self.pending.add(interaction.guild_id)
            acquired = True
            await interaction.response.defer(ephemeral=True, thinking=True)
            tracks = await asyncio.wait_for(self.resolver.tracks(query, interaction.user.id),timeout=90)
            # User may have left or changed channels during metadata extraction.
            if self.gate(interaction).id != channel.id:
                raise ValueError('Your voice channel changed while resolving the request. Run /play again.')
            await self.player(interaction.guild).enqueue(channel, tracks)
            message = f'Queued {len(tracks)} track(s) in {channel.mention}. Audio resolution is in progress.'
            if any(t.matched for t in tracks):
                message += ' Spotify supplied metadata only; matched recordings may differ.'
            await reply(interaction, message)
        except TimeoutError:
            await reply(interaction,'Song resolution took too long. No tracks were queued; try a direct track URL or another source.')
        except ValueError as exc:
            await reply(interaction, str(exc))
        except Exception as exc:
            log.warning('Music request failed: %s', type(exc).__name__)
            await reply(interaction, 'Music request failed. Check host audio diagnostics and Discord voice permissions; no playback was confirmed.')
        finally:
            if acquired:
                self.pending.discard(interaction.guild_id)

    async def dispatch(self, interaction, action, value=None):
        try:
            channel = self.gate(interaction)
            player = self.players.get(interaction.guild_id)
            voice = interaction.guild.voice_client
            if not player or player.closed or not voice:
                raise ValueError('This player is no longer active. Start a new session with /play.')
            if voice.channel.id != channel.id:
                raise ValueError('Join the bot’s voice channel to control this player.')
            await interaction.response.defer(ephemeral=True)
            if action == 'toggle':
                action = 'resume' if voice.is_paused() else 'pause'
            if action in ('down', 'up'):
                value = round(player.volume * 100) + (-10 if action == 'down' else 10)
                action = 'volume'
            await reply(interaction, await player.control(action, value))
        except ValueError as exc:
            await reply(interaction, str(exc))
        except Exception as exc:
            log.warning('Music control failed: %s', type(exc).__name__)
            await reply(interaction, 'Discord or the audio host could not apply that control.')

    async def close(self):
        for player in list(self.players.values()):
            with contextlib.suppress(Exception):
                await player.close()


def install(bot, get_level, get_setting, put_setting):
    service = MusicService(bot, get_level, get_setting, put_setting)

    @bot.tree.command(name='play', description='Stream a song name or supported track/playlist URL')
    @app_commands.describe(query='Song name, YouTube, SoundCloud, Spotify metadata or direct audio URL')
    async def play(interaction: discord.Interaction, query: str):
        await service.play(interaction, query)

    group = app_commands.Group(name='music', description='Control music in your voice channel')
    bot.tree.add_command(group)

    # Each callback has an actual supported command signature and implementation.
    def add_control(name, description):
        async def callback(interaction: discord.Interaction):
            await service.dispatch(interaction, name)
        group.add_command(app_commands.Command(name=name, description=description, callback=callback))
    for name, description in [
        ('pause', 'Pause the current track'), ('resume', 'Resume paused audio'),
        ('skip', 'Skip the current track'), ('previous', 'Play the previous track'),
        ('stop', 'Clear the queue and disconnect'), ('shuffle', 'Shuffle queued tracks'),
        ('queue', 'Show queued tracks'), ('nowplaying', 'Show current playback status'),
        ('clear','Clear queued tracks without stopping current audio'), ('replay','Replay the current track from the beginning'),
    ]:
        add_control(name, description)

    @group.command(name='volume', description='Set volume from 0 to 100 percent')
    async def volume(interaction: discord.Interaction, percent: app_commands.Range[int, 0, 100]):
        await service.dispatch(interaction, 'volume', percent)

    @group.command(name='loop', description='Set track, queue or no repeat')
    @app_commands.choices(mode=[app_commands.Choice(name=x, value=x) for x in ('off', 'track', 'queue')])
    async def loop(interaction: discord.Interaction, mode: app_commands.Choice[str]):
        await service.dispatch(interaction, 'loop', mode.value)

    @group.command(name='dj-role', description='Administrator: allow or revoke an existing DJ role')
    async def dj_role(interaction: discord.Interaction, role: discord.Role, enabled: bool):
        if not interaction.guild or not administrator(interaction.user):
            return await reply(interaction, 'Server Administrator permission required.')
        if role.is_default() or role.managed:
            return await reply(interaction, 'Choose an existing non-managed role other than @everyone.')
        ids = {int(x) for x in get_setting(interaction.guild_id, 'music:dj_roles', '').split(',') if x.isdecimal()}
        ids.add(role.id) if enabled else ids.discard(role.id)
        put_setting(interaction.guild_id, 'music:dj_roles', ','.join(map(str, sorted(ids))))
        await reply(interaction, 'DJ role configuration saved.')

    @group.command(name='voice-channel', description='Administrator: configure allowed existing voice channels')
    async def voice_channel(interaction: discord.Interaction, channel: discord.VoiceChannel, enabled: bool):
        if not interaction.guild or not administrator(interaction.user):
            return await reply(interaction, 'Server Administrator permission required.')
        saved = get_setting(interaction.guild_id, 'music:channels', '')
        ids = {int(x) for x in saved.split(',') if x.isdecimal()} if saved else {
            c.id for c in interaction.guild.voice_channels}
        ids.add(channel.id) if enabled else ids.discard(channel.id)
        # Sentinel disables all voice channels, instead of accidentally restoring defaults.
        put_setting(interaction.guild_id, 'music:channels', ','.join(map(str, sorted(ids))) or '0')
        await reply(interaction, 'Allowed voice channels saved.')

    @group.command(name='remove', description='Remove one queued track by position')
    async def remove(interaction: discord.Interaction, position: app_commands.Range[int, 1, 100]):
        await service.dispatch(interaction, 'remove', position)

    @group.command(name='move', description='Move a queued track to a different position')
    async def move(interaction: discord.Interaction, source: app_commands.Range[int, 1, 100], destination: app_commands.Range[int, 1, 100]):
        await service.dispatch(interaction, 'move', (source, destination))

    @group.command(name='seek', description='Seek to seconds into the current track')
    async def seek(interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 36000]):
        await service.dispatch(interaction, 'seek', seconds)

    @group.command(name='search', description='Search for tracks without joining voice')
    @app_commands.checks.cooldown(1,5,key=lambda i:(i.guild_id,i.user.id))
    async def search(interaction: discord.Interaction, query: str):
        if not interaction.guild or not enabled(get_setting,interaction.guild_id,'music'):
            return await reply(interaction,'Music is disabled in this server.')
        await interaction.response.defer(ephemeral=True,thinking=True)
        try:
            tracks = await asyncio.wait_for(service.resolver.tracks(query,interaction.user.id),timeout=60)
            await reply(interaction,'\n'.join(f'{n}. {discord.utils.escape_markdown(t.title)[:180]}\n{t.url}' for n,t in enumerate(tracks[:5],1)))
        except Exception:
            await reply(interaction,'No accessible source found for this search.')

    @group.command(name='diagnostics', description='Administrator: test host encoding and inspect transport evidence')
    async def diagnostics(interaction: discord.Interaction):
        if not administrator(interaction.user):
            return await reply(interaction, 'Server Administrator permission required.')
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            from services.audio_runtime import smoke_check
            result = await asyncio.to_thread(smoke_check)
            voice = interaction.guild.voice_client
            await reply(interaction, f'Host audio encode/decode: PASS ({result["decoded_pcm_bytes"]} non-silent PCM bytes).\n'
                f'Voice connected: {bool(voice and voice.is_connected())}\n'
                f'Local packet sends: {getattr(voice, "audio_packets", 0)}\n'
                'Audible playback: UNVERIFIED. A listener must confirm hearing music in Discord.')
        except Exception as exc:
            log.warning('Host audio check failed: %s', type(exc).__name__)
            await reply(interaction, 'Host audio check FAILED. Run the repository startup script and inspect the private host console.')

    async def ready():
        if not getattr(bot, '_music_views_registered', False):
            bot.add_view(MusicControls(service))
            bot._music_views_registered = True
    bot.add_listener(ready, 'on_ready')

    async def voice_state(member, before, after):
        if bot.user and member.id == bot.user.id and before.channel and after.channel is None:
            player = service.players.get(member.guild.id)
            current=member.guild.voice_client
            if player and player.voice and not player.closed and (current is None or current is player.voice and not current.is_connected()):
                await player.close()
    bot.add_listener(voice_state, 'on_voice_state_update')
    return service
