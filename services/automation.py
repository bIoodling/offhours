"""Persistent message automation and explicitly configured temporary voice rooms."""
import asyncio
import contextlib
import datetime
import json
import logging
import re
import time
from collections import defaultdict

import discord
from discord.ext import tasks

log = logging.getLogger('offhours.automation')


class AutomationError(ValueError):
    pass


async def reply(interaction, text, **kwargs):
    kwargs.setdefault('ephemeral', True)
    kwargs.setdefault('allowed_mentions', discord.AllowedMentions.none())
    if interaction.response.is_done():
        return await interaction.followup.send(text[:1900], **kwargs)
    return await interaction.response.send_message(text[:1900], **kwargs)


def delay_seconds(text, minimum=10, maximum=365 * 86400):
    match = re.fullmatch(r'\s*(\d{1,8})\s*([smhdw])\s*', text.lower())
    if not match:
        raise AutomationError('Use a delay such as 30m, 2h, 3d or 1w.')
    seconds = int(match[1]) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[match[2]]
    if not minimum <= seconds <= maximum:
        raise AutomationError(f'Choose a delay between {minimum} and {maximum} seconds.')
    return seconds


def delivery_time(value):
    if re.fullmatch(r'\s*\d{1,8}\s*[smhdw]\s*', value.lower()):
        return time.time() + delay_seconds(value)
    try:
        parsed = datetime.datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        if not parsed.tzinfo:
            raise AutomationError('An absolute time must include its timezone, for example 2026-12-01T18:00:00+00:00.')
        result = parsed.timestamp()
    except (ValueError, OverflowError):
        raise AutomationError('Use a delay such as 2h or an ISO date/time including timezone.') from None
    if not time.time() + 10 <= result <= time.time() + 365 * 86400:
        raise AutomationError('Choose a time between ten seconds and one year from now.')
    return result


class AutomationService:
    def __init__(self, bot, connection, get_setting, put_setting):
        self.bot, self.db, self.get, self.put = bot, connection, get_setting, put_setting
        self.locks = defaultdict(asyncio.Lock)
        self.cooldowns = {}
        self.pending_sticky = {}
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS automation_commands(
          guild INTEGER NOT NULL,name TEXT NOT NULL,response TEXT NOT NULL,embed INTEGER NOT NULL DEFAULT 0,
          cooldown INTEGER NOT NULL DEFAULT 10,roles TEXT NOT NULL DEFAULT '[]',channels TEXT NOT NULL DEFAULT '[]',
          enabled INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(guild,name));
        CREATE TABLE IF NOT EXISTS automation_triggers(
          id INTEGER PRIMARY KEY AUTOINCREMENT,guild INTEGER NOT NULL,trigger TEXT NOT NULL,response TEXT NOT NULL,
          match TEXT NOT NULL DEFAULT 'exact',channel INTEGER NOT NULL DEFAULT 0,role INTEGER NOT NULL DEFAULT 0,
          cooldown INTEGER NOT NULL DEFAULT 30,enabled INTEGER NOT NULL DEFAULT 1);
        CREATE INDEX IF NOT EXISTS automation_trigger_guild ON automation_triggers(guild,enabled);
        CREATE TABLE IF NOT EXISTS automation_sticky(
          guild INTEGER NOT NULL,channel INTEGER NOT NULL,content TEXT NOT NULL,message INTEGER,
          cooldown INTEGER NOT NULL DEFAULT 60,last REAL NOT NULL DEFAULT 0,PRIMARY KEY(guild,channel));
        CREATE TABLE IF NOT EXISTS automation_schedules(
          id INTEGER PRIMARY KEY AUTOINCREMENT,guild INTEGER NOT NULL,channel INTEGER NOT NULL,creator INTEGER NOT NULL,
          content TEXT NOT NULL,due REAL NOT NULL,repeat INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'active',
          last_message INTEGER,last_error TEXT NOT NULL DEFAULT '',deliveries INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS automation_schedule_due ON automation_schedules(status,due);
        CREATE TABLE IF NOT EXISTS automation_voice_rooms(
          guild INTEGER NOT NULL,channel INTEGER NOT NULL,owner INTEGER NOT NULL,created REAL NOT NULL,
          permissions TEXT NOT NULL,locked INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(guild,channel));
        CREATE INDEX IF NOT EXISTS automation_voice_owner ON automation_voice_rooms(guild,owner);
        CREATE TABLE IF NOT EXISTS automation_voice_trusted(
          guild INTEGER NOT NULL,channel INTEGER NOT NULL,user INTEGER NOT NULL,PRIMARY KEY(guild,channel,user));
        ''')
        # A process stopped during a send has an uncertain outcome. Pause that occurrence
        # instead of publishing a duplicate automatically after restart.
        self.db.execute("UPDATE automation_schedules SET status='paused',last_error='Delivery confirmation was interrupted; review the channel before resuming.' WHERE status='sending'")
        self.db.commit()

    def query(self, sql, args=()):
        cursor = self.db.execute(sql, args)
        names = [row[0] for row in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    @staticmethod
    def admin(member):
        return isinstance(member, discord.Member) and (member.guild_permissions.administrator or member.guild_permissions.manage_guild)

    def enabled(self, guild, feature=None):
        module = self.get(guild, 'module:automation', '0') == '1'
        return module and (not feature or self.get(guild, 'automation:' + feature, '0') == '1')

    def enable(self, guild, feature=None):
        self.put(guild, 'module:automation', '1')
        if feature:
            self.put(guild, 'automation:' + feature, '1')

    def command(self, guild, name):
        rows = self.query('SELECT * FROM automation_commands WHERE guild=? AND name=?', (guild, name.casefold()))
        if not rows:
            raise AutomationError('No custom command has that name in this server.')
        return rows[0]

    @staticmethod
    def render(template, message=None, member=None, guild=None, channel=None, args=''):
        if message:
            member, guild, channel = message.author, message.guild, message.channel
        values = {'{args}': args, '{user}': member.display_name, '{user.name}': member.name,
                  '{user.mention}': member.mention, '{user.id}': str(member.id),
                  '{server.name}': guild.name, '{server.id}': str(guild.id),
                  '{server.member_count}': str(guild.member_count or 0),
                  '{channel}': channel.mention, '{channel.name}': channel.name,
                  '{timestamp}': f'<t:{int(time.time())}:F>'}
        # One replacement pass prevents inserted user text from becoming a template.
        return re.sub(r'\{(?:args|user(?:\.(?:name|mention|id))?|server\.(?:name|id|member_count)|channel(?:\.name)?|timestamp)\}',
                      lambda match: values.get(match[0], match[0]), template)[:1900]

    def cooldown(self, key, seconds):
        now = time.monotonic()
        if now - self.cooldowns.get(key, 0) < seconds:
            return False
        self.cooldowns[key] = now
        if len(self.cooldowns) > 4096:
            self.cooldowns = {k: v for k, v in self.cooldowns.items() if now - v < 3600}
            if len(self.cooldowns) > 4096:
                self.cooldowns = dict(sorted(self.cooldowns.items(), key=lambda row: row[1], reverse=True)[:2048])
        return True

    async def on_message(self, message):
        if not message.guild or message.author.bot or not isinstance(message.author, discord.Member):
            return
        guild = message.guild.id
        if not self.enabled(guild):
            return
        prefix = self.get(guild, 'prefix', '?')
        content = message.content
        try:
            if prefix and content.startswith(prefix) and self.enabled(guild, 'custom'):
                invocation = content[len(prefix):].strip().split(maxsplit=1)
                if invocation and not self.bot.get_command(invocation[0]):
                    rows = self.query('SELECT * FROM automation_commands WHERE guild=? AND name=? AND enabled=1', (guild, invocation[0].casefold()))
                    if rows:
                        command = rows[0]
                        roles, channels = set(json.loads(command['roles'])), set(json.loads(command['channels']))
                        if roles and not roles.intersection(r.id for r in message.author.roles):
                            return
                        if channels and message.channel.id not in channels:
                            return
                        if not self.cooldown(('custom', guild, command['name'], message.author.id), command['cooldown']):
                            return
                        text = self.render(command['response'], message=message, args=invocation[1] if len(invocation) > 1 else '')
                        if command['embed']:
                            await message.channel.send(embed=discord.Embed(description=text, colour=0xD4D4D4), allowed_mentions=discord.AllowedMentions.none(), silent=True)
                        else:
                            await message.channel.send(text, allowed_mentions=discord.AllowedMentions.none(), silent=True)
                        return
            if not (prefix and content.startswith(prefix)) and self.enabled(guild, 'autoresponder'):
                for rule in self.query('SELECT * FROM automation_triggers WHERE guild=? AND enabled=1 ORDER BY id LIMIT 100', (guild,)):
                    if rule['channel'] and rule['channel'] != message.channel.id:
                        continue
                    if rule['role'] and not any(role.id == rule['role'] for role in message.author.roles):
                        continue
                    haystack, needle = content[:4000].casefold(), rule['trigger'].casefold()
                    matches = haystack == needle if rule['match'] == 'exact' else haystack.startswith(needle) if rule['match'] == 'starts' else needle in haystack
                    if matches and self.cooldown(('trigger', guild, rule['id'], message.channel.id), rule['cooldown']):
                        await message.channel.send(self.render(rule['response'], message=message), allowed_mentions=discord.AllowedMentions.none(), silent=True)
                        break  # A message can cause at most one automatic response.
            if self.enabled(guild, 'sticky'):
                rows = self.query('SELECT * FROM automation_sticky WHERE guild=? AND channel=?', (guild, message.channel.id))
                key = (guild, message.channel.id)
                if rows and (key not in self.pending_sticky or self.pending_sticky[key].done()):
                    self.pending_sticky[key] = asyncio.create_task(self.repost_sticky(message.channel), name=f'sticky-{message.channel.id}')
        except discord.HTTPException:
            log.warning('Message automation failed guild=%s channel=%s', guild, message.channel.id)
        except (ValueError, TypeError):
            log.warning('Invalid message automation configuration guild=%s', guild)

    async def post_sticky(self, channel, record):
        message = await channel.send(record['content'][:1900], allowed_mentions=discord.AllowedMentions.none(), silent=True)
        self.db.execute('UPDATE automation_sticky SET message=?,last=? WHERE guild=? AND channel=?',
                        (message.id, time.time(), channel.guild.id, channel.id))
        self.db.commit()
        if record['message'] and record['message'] != message.id:
            with contextlib.suppress(discord.HTTPException):
                old = await channel.fetch_message(record['message'])
                if old.author.id == self.bot.user.id:
                    await old.delete()
        return message

    async def repost_sticky(self, channel):
        key = (channel.guild.id, channel.id)
        try:
            rows = self.query('SELECT * FROM automation_sticky WHERE guild=? AND channel=?', key)
            if not rows:
                return
            wait = max(5, rows[0]['cooldown'] - (time.time() - rows[0]['last']))
            await asyncio.sleep(wait)
            async with self.locks[('sticky', *key)]:
                rows = self.query('SELECT * FROM automation_sticky WHERE guild=? AND channel=?', key)
                if rows and self.enabled(channel.guild.id, 'sticky'):
                    await self.post_sticky(channel, rows[0])
        except discord.HTTPException:
            log.warning('Sticky update failed guild=%s channel=%s', *key)
        finally:
            self.pending_sticky.pop(key, None)

    def schedule(self, guild, schedule_id):
        rows = self.query('SELECT * FROM automation_schedules WHERE guild=? AND id=?', (guild, schedule_id))
        if not rows:
            raise AutomationError('No schedule with that ID exists in this server.')
        return rows[0]

    @tasks.loop(seconds=30)
    async def scheduled_messages(self):
        for record in self.query("SELECT * FROM automation_schedules WHERE status='active' AND due<=? ORDER BY due LIMIT 30", (time.time(),)):
            if not self.enabled(record['guild'], 'schedules'):
                continue
            guild = self.bot.get_guild(record['guild'])
            channel = guild.get_channel(record['channel']) if guild else None
            if not guild:
                continue
            async with self.locks[('schedule', record['guild'])]:
                latest = self.schedule(record['guild'], record['id'])
                if latest['status'] != 'active' or latest['due'] > time.time():
                    continue
                if not isinstance(channel, discord.TextChannel):
                    self.db.execute("UPDATE automation_schedules SET status='paused',last_error='Selected channel no longer exists.' WHERE guild=? AND id=?", (record['guild'], record['id']))
                    self.db.commit()
                    continue
                self.db.execute("UPDATE automation_schedules SET status='sending',last_error='' WHERE guild=? AND id=?", (record['guild'], record['id']))
                self.db.commit()
                try:
                    message = await channel.send(record['content'][:1900], nonce=f'{record["id"]}:{int(record["due"])}',
                                                 allowed_mentions=discord.AllowedMentions.none(), silent=True)
                    if record['repeat']:
                        # Skip missed occurrences; never flood a channel after downtime.
                        missed = max(1, int((time.time() - record['due']) // record['repeat']) + 1)
                        due = record['due'] + missed * record['repeat']
                        status = 'active'
                    else:
                        due, status = record['due'], 'completed'
                    self.db.execute('UPDATE automation_schedules SET due=?,status=?,last_message=?,deliveries=deliveries+1 WHERE guild=? AND id=?',
                                    (due, status, message.id, record['guild'], record['id']))
                    self.db.commit()
                except discord.HTTPException as exc:
                    description = 'Discord denied delivery; check channel permissions.' if isinstance(exc, discord.Forbidden) else 'Delivery failed or could not be confirmed. Review the channel before resuming.'
                    self.db.execute("UPDATE automation_schedules SET status='paused',last_error=? WHERE guild=? AND id=?",
                                    (description, record['guild'], record['id']))
                    self.db.commit()
                    log.warning('Scheduled message paused guild=%s schedule=%s type=%s', record['guild'], record['id'], type(exc).__name__)

    def voice_room(self, guild, channel):
        rows = self.query('SELECT * FROM automation_voice_rooms WHERE guild=? AND channel=?', (guild, channel))
        if not rows:
            raise AutomationError('Use this command inside a temporary room created by this bot.')
        return rows[0]

    @staticmethod
    def encode_permissions(overwrites):
        return json.dumps([{'id': target.id, 'kind': 'role' if isinstance(target, discord.Role) else 'member',
                            'allow': overwrite.pair()[0].value, 'deny': overwrite.pair()[1].value}
                           for target, overwrite in overwrites.items()])

    @staticmethod
    def decode_permissions(guild, encoded):
        result = {}
        for item in json.loads(encoded):
            target = guild.get_role(item['id']) if item['kind'] == 'role' else guild.get_member(item['id'])
            if not target:
                target = discord.Object(id=item['id'], type=discord.Role if item['kind'] == 'role' else discord.Member)
            if target:
                result[target] = discord.PermissionOverwrite.from_pair(discord.Permissions(item['allow']), discord.Permissions(item['deny']))
        return result

    async def voice_state(self, member, before, after):
        if member.bot:
            return
        guild = member.guild
        try:
            if before.channel and (not after.channel or before.channel.id != after.channel.id):
                await self.cleanup_room(guild, before.channel.id)
            lobby_id = self.get(guild.id, 'tempvoice:lobby', '0')
            if not after.channel or not lobby_id.isdecimal() or after.channel.id != int(lobby_id) or not self.enabled(guild.id, 'tempvoice'):
                return
            async with self.locks[('voice', guild.id)]:
                if not member.voice or not member.voice.channel or member.voice.channel.id != int(lobby_id):
                    return
                existing = self.query('SELECT channel FROM automation_voice_rooms WHERE guild=? AND owner=? ORDER BY created DESC LIMIT 1', (guild.id, member.id))
                if existing:
                    room = guild.get_channel(existing[0]['channel'])
                    if isinstance(room, discord.VoiceChannel):
                        await member.move_to(room, reason='Return to owned temporary voice room')
                        return
                maximum = min(50, max(1, int(self.get(guild.id, 'tempvoice:max_rooms', '10'))))
                count = self.db.execute('SELECT COUNT(*) FROM automation_voice_rooms WHERE guild=?', (guild.id,)).fetchone()[0]
                if count >= maximum:
                    return
                lobby = after.channel
                category_id = self.get(guild.id, 'tempvoice:category', '0')
                category = guild.get_channel(int(category_id)) if category_id.isdecimal() else None
                if not isinstance(category, discord.CategoryChannel):
                    return
                permissions = dict(lobby.overwrites)
                permissions[member] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True)
                permissions[guild.me] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, manage_channels=True, move_members=True)
                limit = max(0, min(99, int(self.get(guild.id, 'tempvoice:limit', '0'))))
                name = self.get(guild.id, 'tempvoice:naming', "{user}'s room").replace('{user}', member.display_name)[:100]
                room = await guild.create_voice_channel(name or 'Temporary room', category=category, overwrites=permissions,
                    user_limit=limit, bitrate=min(lobby.bitrate, guild.bitrate_limit), reason='Member joined the explicitly configured temporary-room lobby')
                self.db.execute('INSERT INTO automation_voice_rooms VALUES(?,?,?,?,?,0)',
                    (guild.id, room.id, member.id, time.time(), self.encode_permissions(permissions)))
                self.db.commit()
                if member.voice and member.voice.channel and member.voice.channel.id == lobby.id:
                    await member.move_to(room, reason='Move requester into their new temporary voice room')
                else:
                    await self.cleanup_room(guild, room.id)
        except (discord.HTTPException, ValueError):
            log.warning('Temporary voice operation failed guild=%s member=%s', guild.id, member.id)

    async def cleanup_room(self, guild, channel_id):
        rows = self.query('SELECT channel FROM automation_voice_rooms WHERE guild=? AND channel=?', (guild.id, channel_id))
        if not rows:
            return
        room = guild.get_channel(channel_id)
        if room and (not isinstance(room, discord.VoiceChannel) or room.members):
            return
        if room:
            await room.delete(reason='Empty temporary voice channel created and recorded by offhours')
        self.db.execute('DELETE FROM automation_voice_trusted WHERE guild=? AND channel=?', (guild.id, channel_id))
        self.db.execute('DELETE FROM automation_voice_rooms WHERE guild=? AND channel=?', (guild.id, channel_id))
        self.db.commit()

    async def ready(self):
        if not self.scheduled_messages.is_running():
            self.scheduled_messages.start()
        for record in self.query('SELECT guild,channel FROM automation_voice_rooms LIMIT 1000'):
            guild = self.bot.get_guild(record['guild'])
            if guild:
                with contextlib.suppress(discord.HTTPException):
                    await self.cleanup_room(guild, record['channel'])

    async def close(self):
        self.scheduled_messages.cancel()
        pending = list(self.pending_sticky.values())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
