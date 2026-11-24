"""Guild-isolated suggestions, giveaways and starboards with persistent controls."""
import asyncio
import contextlib
import io
import json
import logging
import random
import re
import time
from collections import defaultdict
from types import SimpleNamespace

import discord
from discord.ext import tasks

log = logging.getLogger('offhours.community')


class CommunityError(ValueError):
    pass


async def reply(interaction, text, **kwargs):
    kwargs.setdefault('ephemeral', True)
    kwargs.setdefault('allowed_mentions', discord.AllowedMentions.none())
    if interaction.response.is_done():
        return await interaction.followup.send(text[:1900], **kwargs)
    return await interaction.response.send_message(text[:1900], **kwargs)


def parse_duration(text):
    match = re.fullmatch(r'\s*(\d{1,6})\s*([smhdw])\s*', text.lower())
    if not match:
        raise CommunityError('Use a duration such as 30m, 2h, 3d or 1w.')
    seconds = int(match[1]) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[match[2]]
    if not 60 <= seconds <= 90 * 86400:
        raise CommunityError('Choose a duration between one minute and 90 days.')
    return seconds


class CommunityService:
    def __init__(self, bot, connection, get_setting, put_setting, get_level=None):
        self.bot, self.db, self.get, self.put = bot, connection, get_setting, put_setting
        self.get_level = get_level or (lambda guild, user: 0)
        self.locks = defaultdict(asyncio.Lock)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS community_suggestions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,guild INTEGER NOT NULL,channel INTEGER NOT NULL,
          message INTEGER,author INTEGER NOT NULL,content TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
          reason TEXT NOT NULL DEFAULT '',reviewer INTEGER,created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS suggestion_message ON community_suggestions(guild,message);
        CREATE TABLE IF NOT EXISTS community_votes(
          guild INTEGER NOT NULL,suggestion INTEGER NOT NULL,user INTEGER NOT NULL,vote INTEGER NOT NULL,
          PRIMARY KEY(guild,suggestion,user));
        CREATE TABLE IF NOT EXISTS community_giveaways(
          id INTEGER PRIMARY KEY AUTOINCREMENT,guild INTEGER NOT NULL,channel INTEGER NOT NULL,message INTEGER,
          host INTEGER NOT NULL,prize TEXT NOT NULL,ends REAL NOT NULL,count INTEGER NOT NULL,
          role INTEGER NOT NULL DEFAULT 0,minlevel INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'active',
          winners TEXT NOT NULL DEFAULT '[]',created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS giveaway_deadlines ON community_giveaways(status,ends);
        CREATE TABLE IF NOT EXISTS community_entries(
          guild INTEGER NOT NULL,giveaway INTEGER NOT NULL,user INTEGER NOT NULL,created REAL NOT NULL,
          PRIMARY KEY(guild,giveaway,user));
        CREATE TABLE IF NOT EXISTS community_stars(
          guild INTEGER NOT NULL,source INTEGER NOT NULL,message INTEGER NOT NULL,destination INTEGER,
          posted INTEGER,stars INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(guild,message));
        CREATE TABLE IF NOT EXISTS community_polls(
          guild INTEGER NOT NULL,message INTEGER NOT NULL,channel INTEGER NOT NULL,author INTEGER NOT NULL,
          question TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(guild,message));
        ''')
        self.db.commit()

    def query(self, sql, args=()):
        cursor = self.db.execute(sql, args)
        names = [x[0] for x in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    @staticmethod
    def admin(member):
        return isinstance(member, discord.Member) and (member.guild_permissions.manage_guild or member.guild_permissions.administrator)

    @staticmethod
    def moderator(member):
        return CommunityService.admin(member) or (isinstance(member, discord.Member) and member.guild_permissions.manage_messages)

    def enabled(self, guild, module):
        return self.get(guild, 'module:' + module, '0').lower() in ('1', 'true', 'yes', 'on')

    def panel(self, guild, kind):
        try:
            value = json.loads(self.get(guild, 'panel:' + kind, '{}'))
            return value if isinstance(value, dict) else {}
        except (ValueError, TypeError):
            return {}

    def channel(self, guild, kind):
        config = self.panel(guild.id, kind)
        selected = self.get(guild.id, kind + ':channel', '') or config.get('channel_id') or config.get('channel') or 0
        try:
            channel = guild.get_channel(int(selected))
        except (ValueError, TypeError):
            channel = None
        return channel if isinstance(channel, discord.TextChannel) else None

    def suggestion(self, guild, suggestion_id=None, message_id=None):
        column, value = ('id', suggestion_id) if suggestion_id is not None else ('message', message_id)
        rows = self.query(f'SELECT * FROM community_suggestions WHERE guild=? AND {column}=?', (guild, value))
        if not rows:
            raise CommunityError('No matching suggestion exists in this server.')
        return rows[0]

    def suggestion_embed(self, record):
        votes = self.query('SELECT vote,COUNT(*) AS count FROM community_votes WHERE guild=? AND suggestion=? GROUP BY vote', (record['guild'], record['id']))
        counts = {r['vote']: r['count'] for r in votes}
        colours = {'pending': 0xBFC5CE, 'approved': 0x8BA99C, 'denied': 0xBA929D, 'withdrawn': 0x777F8B}
        embed = discord.Embed(title=f'Suggestion #{record["id"]}', description=record['content'][:3500], colour=colours.get(record['status'], 0xBFC5CE))
        embed.add_field(name='Submitted by', value=f'<@{record["author"]}>')
        embed.add_field(name='Votes', value=f'For: {counts.get(1, 0)} · Against: {counts.get(-1, 0)}')
        embed.add_field(name='Status', value=record['status'].title())
        if record['reason']:
            embed.add_field(name='Staff response', value=record['reason'][:1000], inline=False)
        embed.set_footer(text='One vote per member. Press the same choice again to remove your vote.')
        return embed

    async def submit(self, interaction, text):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            raise CommunityError('Submit suggestions from within a server.')
        if not self.enabled(interaction.guild_id, 'suggestions'):
            raise CommunityError('Suggestions are disabled in this server.')
        if not 10 <= len(text.strip()) <= 3500:
            raise CommunityError('A suggestion must contain between 10 and 3500 characters.')
        channel = self.channel(interaction.guild, 'suggestions')
        if not channel:
            raise CommunityError('An administrator must select an existing suggestions channel first.')
        if not channel.permissions_for(interaction.user).view_channel:
            raise CommunityError('You do not have access to the configured suggestions channel.')
        last = self.db.execute('SELECT MAX(created) FROM community_suggestions WHERE guild=? AND author=?', (interaction.guild_id, interaction.user.id)).fetchone()[0] or 0
        if time.time() - last < 60:
            raise CommunityError('Wait one minute between suggestions.')
        await interaction.response.defer(ephemeral=True)
        # Recheck after the network await; another request may have committed meanwhile.
        last = self.db.execute('SELECT MAX(created) FROM community_suggestions WHERE guild=? AND author=?',
                               (interaction.guild_id, interaction.user.id)).fetchone()[0] or 0
        if time.time() - last < 60:
            raise CommunityError('Wait one minute between suggestions.')
        cursor = self.db.execute('INSERT INTO community_suggestions(guild,channel,author,content,created) VALUES(?,?,?,?,?)',
            (interaction.guild_id, channel.id, interaction.user.id, text.strip(), time.time()))
        suggestion_id = cursor.lastrowid
        self.db.commit()
        record = self.suggestion(interaction.guild_id, suggestion_id)
        try:
            message = await channel.send(embed=self.suggestion_embed(record), view=SuggestionVotes(self), allowed_mentions=discord.AllowedMentions.none(), silent=True)
        except BaseException:
            self.db.execute('DELETE FROM community_suggestions WHERE guild=? AND id=?', (interaction.guild_id, suggestion_id))
            self.db.commit()
            raise
        self.db.execute('UPDATE community_suggestions SET message=? WHERE guild=? AND id=?', (message.id, interaction.guild_id, suggestion_id))
        self.db.commit()
        await reply(interaction, f'Suggestion submitted: {message.jump_url}')

    async def vote(self, interaction, direction):
        try:
            if not interaction.guild or not isinstance(interaction.user, discord.Member):
                raise CommunityError('Only server members can vote.')
            if not self.enabled(interaction.guild_id, 'suggestions'):
                raise CommunityError('Suggestions are disabled in this server.')
            record = self.suggestion(interaction.guild_id, message_id=interaction.message.id)
            if record['status'] != 'pending':
                raise CommunityError('Voting is closed for this suggestion.')
            async with self.locks[('suggest', record['id'])]:
                prior = self.db.execute('SELECT vote FROM community_votes WHERE guild=? AND suggestion=? AND user=?', (interaction.guild_id, record['id'], interaction.user.id)).fetchone()
                if prior and prior[0] == direction:
                    self.db.execute('DELETE FROM community_votes WHERE guild=? AND suggestion=? AND user=?', (interaction.guild_id, record['id'], interaction.user.id))
                else:
                    self.db.execute('INSERT OR REPLACE INTO community_votes VALUES(?,?,?,?)', (interaction.guild_id, record['id'], interaction.user.id, direction))
                self.db.commit()
                await interaction.response.edit_message(embed=self.suggestion_embed(record), view=SuggestionVotes(self))
        except CommunityError as exc:
            await reply(interaction, str(exc))
        except discord.HTTPException:
            await reply(interaction, 'Discord could not update the voting display. Your stored vote is preserved.')

    async def refresh_suggestion(self, guild, record):
        channel = guild.get_channel(record['channel'])
        if not channel:
            raise CommunityError('The original suggestion channel no longer exists.')
        message = await channel.fetch_message(record['message'])
        await message.edit(embed=self.suggestion_embed(record), view=SuggestionVotes(self) if record['status'] == 'pending' else None)

    def giveaway(self, guild, giveaway_id=None, message_id=None):
        column, value = ('id', giveaway_id) if giveaway_id is not None else ('message', message_id)
        rows = self.query(f'SELECT * FROM community_giveaways WHERE guild=? AND {column}=?', (guild, value))
        if not rows:
            raise CommunityError('No matching giveaway exists in this server.')
        return rows[0]

    def eligible(self, member, record):
        return member and not member.bot and (not record['role'] or any(r.id == record['role'] for r in member.roles)) and self.get_level(record['guild'], member.id) >= record['minlevel']

    def giveaway_embed(self, record):
        count = self.db.execute('SELECT COUNT(*) FROM community_entries WHERE guild=? AND giveaway=?', (record['guild'], record['id'])).fetchone()[0]
        embed = discord.Embed(title='Giveaway · ' + record['prize'][:200], colour=0xBFC5CE)
        embed.description = f'Giveaway #{record["id"]}\nHosted by <@{record["host"]}>\nEnds <t:{int(record["ends"])}:R>\nWinners: {record["count"]}\nEntries: {count}'
        if record['role']:
            embed.add_field(name='Required role', value=f'<@&{record["role"]}>')
        if record['minlevel']:
            embed.add_field(name='Required level', value=str(record['minlevel']))
        if record['status'] in ('ended', 'ending'):
            winners = json.loads(record['winners'])
            embed.add_field(name='Selected winners', value=', '.join(f'<@{user}>' for user in winners) or 'No eligible entries', inline=False)
            embed.set_footer(text='Ended · Winners were randomly selected from eligible entries.')
        elif record['status'] == 'cancelled':
            embed.set_footer(text='Cancelled · No winners selected.')
        else:
            embed.set_footer(text='Use Enter / Leave to manage your entry. Requirements are checked again at drawing.')
        return embed

    async def enter(self, interaction):
        try:
            if not interaction.guild or not isinstance(interaction.user, discord.Member):
                raise CommunityError('Only server members can enter.')
            if not self.enabled(interaction.guild_id, 'giveaways'):
                raise CommunityError('Giveaways are disabled in this server.')
            record = self.giveaway(interaction.guild_id, message_id=interaction.message.id)
            async with self.locks[('giveaway', record['id'])]:
                record = self.giveaway(interaction.guild_id, record['id'])
                if record['status'] != 'active' or record['ends'] <= time.time():
                    raise CommunityError('This giveaway has ended.')
                prior = self.db.execute('SELECT 1 FROM community_entries WHERE guild=? AND giveaway=? AND user=?',
                    (interaction.guild_id, record['id'], interaction.user.id)).fetchone()
                if prior:
                    self.db.execute('DELETE FROM community_entries WHERE guild=? AND giveaway=? AND user=?', (interaction.guild_id, record['id'], interaction.user.id))
                    result = 'Your giveaway entry was removed.'
                else:
                    if not self.eligible(interaction.user, record):
                        raise CommunityError('You do not meet this giveaway’s role or level requirements.')
                    total = self.db.execute('SELECT COUNT(*) FROM community_entries WHERE guild=? AND giveaway=?', (interaction.guild_id, record['id'])).fetchone()[0]
                    if total >= 10000:
                        raise CommunityError('This giveaway has reached the 10,000-entry host safety limit.')
                    self.db.execute('INSERT INTO community_entries VALUES(?,?,?,?)', (interaction.guild_id, record['id'], interaction.user.id, time.time()))
                    result = 'Your giveaway entry was saved.'
                self.db.commit()
                await interaction.response.defer(ephemeral=True)
                await interaction.message.edit(embed=self.giveaway_embed(record), view=GiveawayEntry(self))
                await reply(interaction, result)
        except CommunityError as exc:
            await reply(interaction, str(exc))
        except discord.HTTPException:
            await reply(interaction, 'Discord could not refresh the giveaway display. Your stored entry change is preserved.')

    async def finish(self, guild, giveaway_id, reroll=False):
        async with self.locks[('giveaway', giveaway_id)]:
            record = self.giveaway(guild.id, giveaway_id)
            if record['status'] == 'cancelled':
                raise CommunityError('This giveaway was cancelled.')
            if record['status'] == 'ended' and not reroll:
                raise CommunityError('This giveaway has already ended.')
            if record['status'] != 'ending' or reroll:
                rows = self.query('SELECT user FROM community_entries WHERE guild=? AND giveaway=? LIMIT 10000', (guild.id, giveaway_id))
                candidates = []
                prior = set(json.loads(record['winners'])) if reroll else set()
                for row in rows:
                    member = guild.get_member(row['user'])
                    if member is None:
                        with contextlib.suppress(discord.NotFound, discord.Forbidden):
                            member = await guild.fetch_member(row['user'])
                    if row['user'] not in prior and self.eligible(member, record):
                        candidates.append(row['user'])
                winners = random.SystemRandom().sample(candidates, min(record['count'], len(candidates)))
                self.db.execute("UPDATE community_giveaways SET status='ending',winners=? WHERE guild=? AND id=?", (json.dumps(winners), guild.id, giveaway_id))
                self.db.commit()
                record = self.giveaway(guild.id, giveaway_id)
            channel = guild.get_channel(record['channel'])
            if not channel:
                raise CommunityError('The giveaway channel no longer exists. The selected result remains stored.')
            try:
                message = await channel.fetch_message(record['message'])
                await message.edit(embed=self.giveaway_embed(record), view=None)
            except discord.NotFound:
                await channel.send(embed=self.giveaway_embed(record), allowed_mentions=discord.AllowedMentions.none(), silent=True)
            self.db.execute("UPDATE community_giveaways SET status='ended' WHERE guild=? AND id=?", (guild.id, giveaway_id))
            self.db.commit()
            return json.loads(record['winners'])

    @tasks.loop(seconds=30)
    async def deadlines(self):
        for record in self.query("SELECT guild,id FROM community_giveaways WHERE (status='active' AND ends<=?) OR status='ending' ORDER BY ends LIMIT 50", (time.time(),)):
            guild = self.bot.get_guild(record['guild'])
            if guild:
                try:
                    await self.finish(guild, record['id'])
                except (CommunityError, discord.HTTPException):
                    log.warning('Giveaway conclusion pending for guild=%s giveaway=%s', guild.id, record['id'])

    async def star_event(self, payload):
        if not payload.guild_id or not self.enabled(payload.guild_id, 'starboard'):
            return
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        channel = guild.get_channel(payload.channel_id)
        destination = self.channel(guild, 'starboard')
        emoji = self.get(guild.id, 'starboard:emoji', '⭐')
        if not isinstance(channel, discord.TextChannel) or not destination or channel.id == destination.id or str(payload.emoji) != emoji:
            return
        if channel.is_nsfw() and not destination.is_nsfw():
            return
        # Do not copy private-channel content to audiences who cannot access it.
        for role in guild.roles:
            if destination.permissions_for(role).view_channel and not channel.permissions_for(role).view_channel:
                return
        for member, overwrite in destination.overwrites.items():
            if isinstance(member, discord.Member) and overwrite.view_channel and not channel.permissions_for(member).view_channel:
                return
        for member, overwrite in channel.overwrites.items():
            if isinstance(member, discord.Member) and overwrite.view_channel is False and destination.permissions_for(member).view_channel:
                return
        async with self.locks[('star', guild.id)]:
            try:
                message = await channel.fetch_message(payload.message_id)
                if message.author.bot:
                    return
                reaction = discord.utils.find(lambda r: str(r.emoji) == emoji, message.reactions)
                count = 0
                if reaction:
                    async for user in reaction.users(limit=1000):
                        if not user.bot and user.id != message.author.id:
                            count += 1
                threshold = max(1, min(100, int(self.get(guild.id, 'starboard:threshold', '3'))))
                rows = self.query('SELECT * FROM community_stars WHERE guild=? AND message=?', (guild.id, message.id))
                old = rows[0] if rows else None
                if count < threshold:
                    if old and old['posted']:
                        target = guild.get_channel(old['destination'])
                        if target:
                            with contextlib.suppress(discord.NotFound):
                                await (await target.fetch_message(old['posted'])).delete()
                        self.db.execute('DELETE FROM community_stars WHERE guild=? AND message=?', (guild.id, message.id))
                        self.db.commit()
                    return
                embed = discord.Embed(description=message.content[:3500] or '[Attachment]', colour=0xC5B47A, timestamp=message.created_at)
                embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
                embed.add_field(name='Original message', value=f'[Jump to message]({message.jump_url})', inline=False)
                image = next((a for a in message.attachments if a.content_type and a.content_type.startswith('image/')), None)
                if image:
                    embed.set_image(url=image.url)
                content = f'{emoji} **{count}** · {channel.mention}'
                posted = None
                if old and old['posted'] and old['destination'] == destination.id:
                    with contextlib.suppress(discord.NotFound):
                        posted = await destination.fetch_message(old['posted'])
                        await posted.edit(content=content, embed=embed, allowed_mentions=discord.AllowedMentions.none())
                if not posted:
                    posted = await destination.send(content, embed=embed, allowed_mentions=discord.AllowedMentions.none(), silent=True)
                self.db.execute('INSERT OR REPLACE INTO community_stars VALUES(?,?,?,?,?,?)',
                    (guild.id, channel.id, message.id, destination.id, posted.id, count))
                self.db.commit()
            except (discord.HTTPException, ValueError):
                log.warning('Starboard update failed for guild=%s message=%s', guild.id, payload.message_id)

    async def deleted_message(self, payload):
        if not payload.guild_id:
            return
        rows = self.query('SELECT * FROM community_stars WHERE guild=? AND message=?', (payload.guild_id, payload.message_id))
        guild = self.bot.get_guild(payload.guild_id)
        for row in rows:
            destination = guild.get_channel(row['destination']) if guild else None
            if destination and row['posted']:
                with contextlib.suppress(discord.HTTPException):
                    await (await destination.fetch_message(row['posted'])).delete()
        self.db.execute('DELETE FROM community_stars WHERE guild=? AND message=?', (payload.guild_id, payload.message_id))
        self.db.commit()

    async def bulk_deleted_messages(self, payload):
        for message_id in payload.message_ids:
            await self.deleted_message(SimpleNamespace(guild_id=payload.guild_id, message_id=message_id))

    async def edited_message(self, payload):
        if not payload.guild_id:
            return
        exists = self.db.execute('SELECT 1 FROM community_stars WHERE guild=? AND message=?',
                                  (payload.guild_id, payload.message_id)).fetchone()
        if exists:
            await self.star_event(SimpleNamespace(guild_id=payload.guild_id, channel_id=payload.channel_id,
                message_id=payload.message_id, emoji=self.get(payload.guild_id, 'starboard:emoji', '⭐')))

    async def ready(self):
        if not getattr(self, '_registered', False):
            self.bot.add_view(SuggestionVotes(self))
            self.bot.add_view(GiveawayEntry(self))
            self._registered = True
        if not self.deadlines.is_running():
            self.deadlines.start()

    async def close(self):
        self.deadlines.cancel()


class SuggestionVotes(discord.ui.View):
    def __init__(self, service):
        super().__init__(timeout=None)
        for label, vote in [('For', 1), ('Against', -1)]:
            button = discord.ui.Button(label=label, custom_id=f'offhours:suggestion:vote:{vote}', style=discord.ButtonStyle.secondary)
            async def callback(interaction, vote=vote):
                await service.vote(interaction, vote)
            button.callback = callback
            self.add_item(button)


class GiveawayEntry(discord.ui.View):
    def __init__(self, service):
        super().__init__(timeout=None)
        button = discord.ui.Button(label='Enter / Leave', custom_id='offhours:giveaway:entry:v1', style=discord.ButtonStyle.secondary)
        button.callback = service.enter
        self.add_item(button)


class SuggestionModal(discord.ui.Modal, title='Submit a suggestion'):
    text = discord.ui.TextInput(label='Your suggestion', style=discord.TextStyle.paragraph, min_length=10, max_length=3500)

    def __init__(self, service):
        super().__init__()
        self.service = service

    async def on_submit(self, interaction):
        try:
            await self.service.submit(interaction, str(self.text))
        except CommunityError as exc:
            await reply(interaction, str(exc))
        except discord.HTTPException:
            await reply(interaction, 'Discord could not publish your suggestion. Check the configured channel permissions.')
