"""Guild-isolated moderation, scheduled actions and opt-in message protection."""
import asyncio
from collections import defaultdict, deque
import contextlib
import datetime as dt
import io
import json
import logging
import re
import time
from urllib.parse import urlsplit

import discord
from discord.ext import tasks

log = logging.getLogger('offhours.moderation')
UTC = dt.timezone.utc


def administrator(member):
    return isinstance(member, discord.Member) and member.guild_permissions.administrator


def staff(member, permission='manage_messages'):
    return isinstance(member, discord.Member) and getattr(member.guild_permissions, permission, False)


def duration(value, maximum=365 * 86400):
    match = re.fullmatch(r'\s*(\d+)\s*([smhdw]?)\s*', value.lower())
    if not match:
        raise ValueError('Use a duration such as 30m, 12h or 7d.')
    seconds = int(match[1]) * {'': 60, 's': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[match[2]]
    if not 1 <= seconds <= maximum:
        raise ValueError(f'Duration must be between 1 and {maximum} seconds.')
    return seconds


class ModerationService:
    def __init__(self, bot, connection, get_setting, put_setting):
        self.bot, self.db = bot, connection
        self.get, self.put = get_setting, put_setting
        connection.executescript('''
        CREATE TABLE IF NOT EXISTS mod_cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,guild INTEGER NOT NULL,user INTEGER,
            moderator INTEGER NOT NULL,action TEXT NOT NULL,reason TEXT NOT NULL,
            created REAL NOT NULL,expires REAL,status TEXT NOT NULL DEFAULT 'active');
        CREATE INDEX IF NOT EXISTS mod_cases_guild_user ON mod_cases(guild,user,created);
        CREATE TABLE IF NOT EXISTS mod_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,guild INTEGER NOT NULL,user INTEGER NOT NULL,
            author INTEGER NOT NULL,body TEXT NOT NULL,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS mod_ignored (
            guild INTEGER NOT NULL,kind TEXT NOT NULL,target INTEGER NOT NULL,PRIMARY KEY(guild,kind,target));
        CREATE TABLE IF NOT EXISTS mod_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,guild INTEGER NOT NULL,kind TEXT NOT NULL,
            target INTEGER NOT NULL,payload TEXT NOT NULL,due REAL NOT NULL,attempts INTEGER DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending');
        CREATE INDEX IF NOT EXISTS mod_jobs_due ON mod_jobs(status,due);
        CREATE TABLE IF NOT EXISTS security_incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,guild INTEGER NOT NULL,user INTEGER NOT NULL,
            kind TEXT NOT NULL,detail TEXT NOT NULL,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS afk_members (
            guild INTEGER NOT NULL,user INTEGER NOT NULL,reason TEXT NOT NULL,since REAL NOT NULL,
            PRIMARY KEY(guild,user));
        CREATE TABLE IF NOT EXISTS channel_locks (
            guild INTEGER NOT NULL,channel INTEGER NOT NULL,previous TEXT NOT NULL,
            PRIMARY KEY(guild,channel));
        ''')
        connection.commit()
        self.messages = defaultdict(lambda: deque(maxlen=100))
        self.joins = defaultdict(lambda: deque(maxlen=1000))
        self.audit_events = defaultdict(lambda: deque(maxlen=100))
        self.cooldowns = {}

    def enabled(self, guild, feature):
        return self.get(guild, 'module:security', '0') == '1' and self.get(guild, 'security:' + feature, '0') == '1'

    def number(self, guild, key, default, minimum=1, maximum=100000):
        try:
            return max(minimum, min(maximum, int(self.get(guild, 'security:' + key, str(default)))))
        except (ValueError, TypeError):
            return default

    def check(self, interaction, permission='manage_messages', target=None):
        if not interaction.guild or not staff(interaction.user, permission):
            raise ValueError('You need ' + permission.replace('_', ' ').title() + ' permission in this server.')
        me = interaction.guild.me
        if not me or not getattr(me.guild_permissions, permission, False):
            raise ValueError('The bot needs ' + permission.replace('_', ' ').title() + ' permission.')
        if target and isinstance(target, discord.Member):
            if target.guild.id != interaction.guild.id or target.id in (interaction.guild.owner_id, me.id, interaction.user.id):
                raise ValueError('That member is protected from this action.')
            if target.top_role >= me.top_role:
                raise ValueError('The target’s highest role is above or equal to the bot’s role.')
            if interaction.user.id != interaction.guild.owner_id and target.top_role >= interaction.user.top_role:
                raise ValueError('The target’s highest role is above or equal to your role.')

    async def log(self, guild, text, event='moderation'):
        if self.get(guild.id, 'module:logs', '0') != '1':
            return
        if self.get(guild.id, 'logs:' + event, '1') != '1':
            return
        panels=getattr(self.bot,'panel_service',None)
        if panels:
            configured=panels.load(guild.id,'logs')
            destination=int(configured.get('destination_id') or configured.get('channel_id') or 0)
            override=self.get(guild.id,'logs:channel:'+event,'')
            if configured.get('enabled') and (not override or override==str(destination)):
                try:
                    emitted=await panels.emit('logs',guild.me,event=text[:1900])
                    if emitted:return
                except discord.HTTPException:
                    pass
        raw = self.get(guild.id, 'logs:channel:' + event, '') or self.get(guild.id, 'moderation:log_channel', '') or self.get(guild.id, 'channel:logs', '')
        channel = guild.get_channel(int(raw)) if raw.isdecimal() else None
        panels=getattr(self.bot,'panel_service',None)
        config=panels.load(guild.id,'logs') if panels else {}
        if not channel and config.get('channel_id'):
            channel=guild.get_channel(int(config['channel_id']))
        if channel:
            with contextlib.suppress(discord.HTTPException):
                if panels and panels.is_enabled(guild.id,'logs',config):
                    await channel.send(embed=panels.render('logs',config,guild.me,event=text),
                        files=panels.asset_files(guild.id,'logs',config),allowed_mentions=discord.AllowedMentions.none(),silent=True)
                else:
                    await channel.send(text[:1900], allowed_mentions=discord.AllowedMentions.none(), silent=True)

    async def record_case(self, guild, user, moderator, action, reason, expires=None):
        result = self.db.execute('INSERT INTO mod_cases(guild,user,moderator,action,reason,created,expires) VALUES(?,?,?,?,?,?,?)',
            (guild.id, user, moderator, action, reason[:1000], time.time(), expires))
        self.db.commit()
        case_id = result.lastrowid
        await self.log(guild, f'Case #{case_id} · {action} · User {user or "—"} · Moderator {moderator}\n{reason[:1000]}')
        return case_id

    def schedule(self, guild, kind, target, payload, due):
        cursor = self.db.execute('INSERT INTO mod_jobs(guild,kind,target,payload,due) VALUES(?,?,?,?,?)',
            (guild, kind, target, json.dumps(payload), due))
        self.db.commit()
        return cursor.lastrowid

    async def execute_action(self, interaction, action, target=None, reason='', value=None):
        reason = reason.strip()[:1000] or 'No reason supplied'
        permission = {
            'ban': 'ban_members', 'unban': 'ban_members', 'softban': 'ban_members', 'tempban': 'ban_members',
            'kick': 'kick_members', 'timeout': 'moderate_members', 'untimeout': 'moderate_members',
            'nick': 'manage_nicknames', 'deafen': 'deafen_members', 'undeafen': 'deafen_members',
            'lock': 'manage_channels', 'unlock': 'manage_channels', 'slowmode': 'manage_channels',
        }.get(action, 'manage_messages')
        self.check(interaction, permission, target)
        audit_reason = f'{interaction.user} ({interaction.user.id}): {reason}'[:512]
        expires = None
        if action in ('ban', 'tempban', 'softban'):
            if action == 'tempban':
                expires = time.time() + duration(value)
            await interaction.guild.ban(target, reason=audit_reason, delete_message_seconds=86400 if action == 'softban' else 0)
            if action == 'softban':
                await interaction.guild.unban(target, reason='Softban cleanup completed: ' + audit_reason[:470])
            elif expires:
                self.schedule(interaction.guild.id, 'unban', target.id, {'reason': 'Temporary ban expired'}, expires)
        elif action == 'unban':
            await interaction.guild.unban(target, reason=audit_reason)
            self.db.execute("UPDATE mod_jobs SET status='cancelled' WHERE guild=? AND kind='unban' AND target=? AND status='pending'", (interaction.guild.id, target.id))
        elif action == 'kick':
            await target.kick(reason=audit_reason)
        elif action in ('timeout', 'untimeout'):
            seconds = duration(value, 28 * 86400) if action == 'timeout' else 0
            expires = time.time() + seconds if seconds else None
            await target.timeout(dt.timedelta(seconds=seconds) if seconds else None, reason=audit_reason)
        elif action == 'warn':
            self.db.execute('INSERT INTO warnings(guild,user,moderator,reason,timestamp) VALUES(?,?,?,?,?)',
                (interaction.guild.id, target.id, interaction.user.id, reason, int(time.time())))
        elif action == 'nick':
            await target.edit(nick=value[:32] or None, reason=audit_reason)
        elif action in ('deafen', 'undeafen'):
            if not target.voice:
                raise ValueError('The member is not connected to voice.')
            await target.edit(deafen=action == 'deafen', reason=audit_reason)
        elif action in ('lock', 'unlock'):
            channel = target or interaction.channel
            if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
                raise ValueError('Choose an existing text or voice channel.')
            overwrite = channel.overwrites_for(interaction.guild.default_role)
            if action == 'lock':
                saved = {'send_messages': overwrite.send_messages, 'connect': overwrite.connect}
                self.db.execute('INSERT OR IGNORE INTO channel_locks VALUES(?,?,?)', (interaction.guild.id, channel.id, json.dumps(saved)))
                if isinstance(channel, discord.VoiceChannel):
                    overwrite.connect = False
                overwrite.send_messages = False
            else:
                row = self.db.execute('SELECT previous FROM channel_locks WHERE guild=? AND channel=?', (interaction.guild.id, channel.id)).fetchone()
                if not row:
                    raise ValueError('This channel was not locked by Offhours; its permissions were left untouched.')
                saved = json.loads(row[0])
                overwrite.send_messages = saved['send_messages']
                overwrite.connect = saved['connect']
            await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=audit_reason)
            if action == 'unlock':
                self.db.execute('DELETE FROM channel_locks WHERE guild=? AND channel=?', (interaction.guild.id, channel.id))
        elif action == 'slowmode':
            await interaction.channel.edit(slowmode_delay=int(value), reason=audit_reason)
        else:
            raise ValueError('Unsupported moderation action.')
        self.db.commit()
        target_id = getattr(target, 'id', None)
        return await self.record_case(interaction.guild, target_id, interaction.user.id, action, reason, expires)

    async def purge(self, interaction, amount, bots_only=False):
        self.check(interaction)
        cutoff = discord.utils.utcnow() - dt.timedelta(days=7)
        deleted = await interaction.channel.purge(limit=amount, check=lambda m: m.created_at >= cutoff and (not bots_only or m.author.bot), bulk=True,
            reason=f'Offhours purge by {interaction.user.id}')
        await self.record_case(interaction.guild, None, interaction.user.id, 'clean' if bots_only else 'purge', f'{len(deleted)} messages deleted')
        return len(deleted)

    def ignored(self, message):
        rows = self.db.execute('SELECT kind,target FROM mod_ignored WHERE guild=?', (message.guild.id,)).fetchall()
        role_ids = {r.id for r in message.author.roles}
        return any((kind == 'user' and target == message.author.id) or (kind == 'channel' and target == message.channel.id)
                   or (kind == 'role' and target in role_ids) for kind, target in rows)

    def whitelisted(self, member):
        values = set(self.get(member.guild.id, 'security:whitelist', '').split(','))
        return str(member.id) in values or any(str(role.id) in values for role in member.roles)

    async def incident(self, guild, user, kind, detail):
        self.db.execute('INSERT INTO security_incidents(guild,user,kind,detail,created) VALUES(?,?,?,?,?)',
            (guild.id, user, kind, detail[:1000], time.time()))
        self.db.commit()
        await self.log(guild, f'Security · {kind} · User {user}\n{detail[:1000]}', 'security')

    async def on_message(self, message):
        if not message.guild or message.author.bot or not isinstance(message.author, discord.Member):
            return
        guild, member = message.guild, message.author
        # AFK status is server scoped, including when a member returns.
        returned = self.db.execute('DELETE FROM afk_members WHERE guild=? AND user=?', (guild.id, member.id)).rowcount
        if returned:
            self.db.commit()
            with contextlib.suppress(discord.HTTPException):
                await message.channel.send(f'{member.display_name[:80]} is no longer AFK.', allowed_mentions=discord.AllowedMentions.none(), delete_after=8)
        afk_lines = []
        for mentioned in message.mentions[:5]:
            row = self.db.execute('SELECT reason,since FROM afk_members WHERE guild=? AND user=?', (guild.id, mentioned.id)).fetchone()
            if row:
                afk_lines.append(f'{mentioned.display_name}: {row[0][:150]} (<t:{int(row[1])}:R>)')
        if afk_lines:
            with contextlib.suppress(discord.HTTPException):
                await message.channel.send('\n'.join(afk_lines), allowed_mentions=discord.AllowedMentions.none(), delete_after=20)
        exempt_roles = {int(x) for x in self.get(guild.id, 'security:exempt_roles', '').split(',') if x.isdecimal()}
        moderator_exempt = self.get(guild.id, 'security:moderator_exempt', '1') == '1' and member.guild_permissions.manage_messages
        if self.get(guild.id, 'module:security', '0') != '1' or moderator_exempt or any(r.id in exempt_roles for r in member.roles) or self.whitelisted(member) or self.ignored(message):
            return
        now = time.monotonic()
        key = (guild.id, member.id)
        if len(self.messages) > 10000:
            self.messages = defaultdict(lambda: deque(maxlen=100), {k: v for k, v in self.messages.items() if v and now-v[-1] < 60})
            self.cooldowns = {k: v for k,v in self.cooldowns.items() if now-v < 60}
        bucket = self.messages[key]
        bucket.append(now)
        while bucket and now - bucket[0] > 8:
            bucket.popleft()
        violation = None
        threshold = self.number(guild.id, 'spam_limit', 8, 3, 30)
        if self.enabled(guild.id, 'antispam') and len(bucket) >= threshold:
            violation = 'antispam'
        elif self.enabled(guild.id, 'antimention') and len(message.mentions) + len(message.role_mentions) >= self.number(guild.id, 'mention_limit', 5, 2, 50):
            violation = 'antimention'
        elif self.enabled(guild.id, 'antiinvite') and re.search(r'(discord(?:app)?\.com/invite/|discord\.gg/)', message.content, re.I):
            violation = 'antiinvite'
        elif self.enabled(guild.id, 'antiattachment') and message.attachments:
            violation = 'antiattachment'
        elif self.enabled(guild.id, 'wordfilter'):
            words = [w.strip().casefold() for w in self.get(guild.id, 'security:blocked_words', '').split(',') if w.strip()]
            content = message.content.casefold()
            if any(re.search(r'(?<!\w)' + re.escape(word) + r'(?!\w)', content) for word in words):
                violation = 'wordfilter'
        if not violation and self.enabled(guild.id, 'scamfilter'):
            urls = re.findall(r'https?://[^\s<>]+', message.content, re.I)
            configured = {x.strip().casefold() for x in self.get(guild.id, 'security:blocked_domains', '').split(',') if x.strip()}
            for url in urls:
                host = (urlsplit(url).hostname or '').casefold().rstrip('.')
                # Local suspicious-pattern rules; this is not a reputation-service verdict.
                spoof = bool(re.search(r'(?:discord(?:nitro|gift|airdrop)|steam(?:gift|airdrop|giveaway))', host))
                if host in configured or any(host.endswith('.' + d) for d in configured) or (spoof and host not in ('discord.com','discord.gift','steamcommunity.com','steampowered.com')):
                    violation = 'scamfilter'
                    break
        if not violation and self.enabled(guild.id, 'antilink'):
            link_roles = {int(x) for x in self.get(guild.id, 'security:link_roles', '').split(',') if x.isdecimal()}
            allowed_by_role = any(role.id in link_roles for role in member.roles)
            if allowed_by_role:
                return
            urls = re.findall(r'https?://[^\s<>]+|www\.[^\s<>]+', message.content, re.I)
            domains = {x.strip().lower() for x in self.get(guild.id, 'security:allowed_domains', '').split(',') if x.strip()}
            if any((urlsplit(u if '://' in u else 'https://' + u).hostname or '').lower() not in domains for u in urls):
                violation = 'antilink'
        if not violation:
            return
        if now - self.cooldowns.get(key, -100) < 5:
            with contextlib.suppress(discord.HTTPException):
                await message.delete()
            return
        self.cooldowns[key] = now
        try:
            await message.delete()
            await self.incident(guild, member.id, violation, f'Message removed from channel {message.channel.id}.')
            count = self.db.execute('SELECT COUNT(*) FROM security_incidents WHERE guild=? AND user=? AND created>?',
                (guild.id, member.id, time.time()-86400)).fetchone()[0]
            warning_after = self.number(guild.id, 'warning_after', 3, 1, 50)
            timeout_after = self.number(guild.id, 'timeout_after', 5, 2, 100)
            if count == warning_after:
                reason = f'Configured security escalation after {count} incidents in 24 hours'
                self.db.execute('INSERT INTO warnings(guild,user,moderator,reason,timestamp) VALUES(?,?,?,?,?)',
                    (guild.id, member.id, self.bot.user.id, reason, int(time.time())))
                self.db.commit()
                await self.record_case(guild, member.id, self.bot.user.id, 'automatic-warning', reason)
            if count >= timeout_after and guild.me.guild_permissions.moderate_members and member.top_role < guild.me.top_role and not member.guild_permissions.administrator:
                minutes = self.number(guild.id, 'timeout_minutes', 10, 1, 40320)
                await member.timeout(dt.timedelta(minutes=minutes), reason='Offhours configured security escalation')
                await self.record_case(guild, member.id, self.bot.user.id, 'automatic-timeout',
                    f'{minutes} minutes after {count} incidents in 24 hours', time.time()+minutes*60)
        except discord.HTTPException as exc:
            log.warning('Security action rejected guild=%s status=%s', guild.id, getattr(exc, 'status', 0))

    async def on_member_join(self, member):
        guild = member.guild
        if self.get(guild.id, 'module:logs', '0') == '1':
            await self.log(guild, f'Member joined · {member.id} · account created <t:{int(member.created_at.timestamp())}:F>', 'members')
        if member.bot or self.get(guild.id, 'module:security', '0') != '1' or self.whitelisted(member):
            return
        now = time.monotonic()
        bucket = self.joins[guild.id]
        bucket.append(now)
        while bucket and now - bucket[0] > 10:
            bucket.popleft()
        age_hours = (discord.utils.utcnow() - member.created_at).total_seconds() / 3600
        reason = None
        minimum_age = self.number(guild.id, 'minimum_age_hours', 24, 1, 8760)
        if self.enabled(guild.id, 'accountage') and age_hours < minimum_age:
            reason = 'accountage'
        if self.enabled(guild.id, 'antiraid') and len(bucket) >= self.number(guild.id, 'raid_limit', 10, 3, 100):
            reason = 'antiraid'
        if reason:
            raw = self.get(guild.id, 'security:quarantine_role', '')
            role = guild.get_role(int(raw)) if raw.isdecimal() else None
            dangerous = role and any((role.permissions.administrator, role.permissions.manage_guild, role.permissions.manage_roles,
                role.permissions.manage_channels, role.permissions.ban_members, role.permissions.kick_members, role.permissions.manage_webhooks))
            if role and not role.managed and not role.is_default() and role < guild.me.top_role and not dangerous:
                with contextlib.suppress(discord.HTTPException):
                    await member.add_roles(role, reason='Offhours configured ' + reason)
            if guild.me.guild_permissions.moderate_members and member.top_role < guild.me.top_role:
                with contextlib.suppress(discord.HTTPException):
                    await member.timeout(dt.timedelta(minutes=10), reason='Offhours configured ' + reason)
            await self.incident(guild, member.id, reason, 'Join flagged; configured quarantine role and/or temporary timeout attempted.')
        if self.enabled(guild.id, 'antihoist') and member.display_name[:1] in '!$%&*+-./:;=?@^_`|~':
            nickname = member.display_name.lstrip(' !$%&*+-./:;=?@^_`|~')[:32]
            if nickname and guild.me.guild_permissions.manage_nicknames and member.top_role < guild.me.top_role:
                with contextlib.suppress(discord.HTTPException):
                    await member.edit(nick=nickname, reason='Offhours configured antihoist')

    async def on_audit_log_entry_create(self, entry):
        guild = entry.guild
        if not self.enabled(guild.id, 'antinuke') or not entry.user_id or entry.user_id in (guild.owner_id, self.bot.user.id):
            return
        actor = guild.get_member(entry.user_id)
        if not actor or self.whitelisted(actor) or actor.top_role >= guild.me.top_role:
            return
        destructive = {discord.AuditLogAction.channel_delete, discord.AuditLogAction.role_delete,
                       discord.AuditLogAction.ban, discord.AuditLogAction.kick,
                       discord.AuditLogAction.webhook_create}
        if entry.action not in destructive:
            return
        key, now = (guild.id, actor.id), time.monotonic()
        bucket = self.audit_events[key]
        bucket.append(now)
        while bucket and now - bucket[0] > 10:
            bucket.popleft()
        if len(bucket) < self.number(guild.id, 'nuke_limit', 5, 3, 30):
            return
        dangerous = [r for r in actor.roles if not r.managed and not r.is_default() and r < guild.me.top_role
                     and any((r.permissions.administrator, r.permissions.manage_channels, r.permissions.manage_roles,
                              r.permissions.ban_members, r.permissions.kick_members, r.permissions.manage_webhooks))]
        if dangerous and guild.me.guild_permissions.manage_roles:
            try:
                await actor.remove_roles(*dangerous, reason='Offhours configured destructive-action rate containment')
                await self.incident(guild, actor.id, 'antinuke', 'Removed dangerous role assignments after destructive audit-event threshold.')
                bucket.clear()
            except discord.HTTPException:
                await self.incident(guild, actor.id, 'antinuke-failed', 'Discord denied removal of dangerous role assignments.')

    async def on_message_delete(self, message):
        if not message.guild or message.author.bot or self.get(message.guild.id, 'module:logs', '0') != '1':
            return
        # Private ticket contents must never appear in shared moderation logs.
        ticket = self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tickets'").fetchone()
        if ticket:
            columns = {r[1] for r in self.db.execute('PRAGMA table_info(tickets)')}
            if 'channel' in columns and 'guild' in columns and self.db.execute('SELECT 1 FROM tickets WHERE guild=? AND channel=?', (message.guild.id, message.channel.id)).fetchone():
                return
            if 'channel_id' in columns and 'guild_id' in columns and self.db.execute('SELECT 1 FROM tickets WHERE guild_id=? AND channel_id=?', (message.guild.id, message.channel.id)).fetchone():
                return
        # Log metadata only, excluding message bodies, attachments and private content.
        ignored = self.get(message.guild.id, 'logs:ignored_channels', '').split(',')
        if str(message.channel.id) not in ignored:
            await self.log(message.guild, f'Message deleted · author {message.author.id} · channel {message.channel.id} · message {message.id}', 'messages')

    async def on_message_edit(self, before, after):
        if not after.guild or after.author.bot:
            return
        if before.content != after.content:
            if self.get(after.guild.id, 'module:logs', '0') == '1':
                if str(after.channel.id) not in self.get(after.guild.id, 'logs:ignored_channels', '').split(','):
                    await self.log(after.guild, f'Message edited · author {after.author.id} · channel {after.channel.id} · message {after.id}', 'messages')
            await self.on_message(after)

    async def on_member_update(self, before, after):
        if self.get(after.guild.id, 'module:logs', '0') != '1':
            return
        added = [r.id for r in after.roles if r not in before.roles]
        removed = [r.id for r in before.roles if r not in after.roles]
        changes = []
        if added: changes.append('roles added: ' + ', '.join(map(str, added)))
        if removed: changes.append('roles removed: ' + ', '.join(map(str, removed)))
        if before.nick != after.nick: changes.append('nickname changed')
        if before.timed_out_until != after.timed_out_until: changes.append('timeout changed')
        if changes:
            await self.log(after.guild, f'Member updated · {after.id}\n' + '\n'.join(changes), 'members')

    async def on_member_remove(self, member):
        if self.get(member.guild.id, 'module:logs', '0') == '1':
            await self.log(member.guild, f'Member left · {member.id}', 'members')

    async def on_guild_channel_update(self, before, after):
        if self.get(after.guild.id, 'module:logs', '0') == '1':
            changes = []
            if before.name != after.name: changes.append('name changed')
            if before.category_id != after.category_id: changes.append('category changed')
            if before.overwrites != after.overwrites: changes.append('permission overwrites changed')
            if changes: await self.log(after.guild, f'Channel updated · {after.id}\n' + '\n'.join(changes), 'channels')

    async def on_guild_channel_delete(self, channel):
        if self.get(channel.guild.id, 'module:logs', '0') == '1':
            await self.log(channel.guild, f'Channel deleted · {channel.id}', 'channels')

    async def on_guild_role_update(self, before, after):
        if self.get(after.guild.id, 'module:logs', '0') == '1' and (before.name != after.name or before.permissions != after.permissions or before.colour != after.colour):
            await self.log(after.guild, f'Role updated · {after.id} · name, permissions or colour changed', 'roles')

    async def on_guild_role_delete(self, role):
        if self.get(role.guild.id, 'module:logs', '0') == '1':
            await self.log(role.guild, f'Role deleted · {role.id}', 'roles')

    @tasks.loop(seconds=30)
    async def jobs(self):
        rows = self.db.execute("SELECT id,guild,kind,target,payload,attempts FROM mod_jobs WHERE status='pending' AND due<=? ORDER BY due LIMIT 50", (time.time(),)).fetchall()
        for job_id, guild_id, kind, target, raw, attempts in rows:
            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue
            payload = json.loads(raw)
            try:
                if kind == 'unban':
                    try:
                        await guild.unban(discord.Object(id=target), reason=payload.get('reason', 'Temporary ban expired'))
                    except discord.NotFound:
                        pass  # Already unbanned is an idempotent completion.
                    await self.record_case(guild, target, self.bot.user.id, 'automatic-unban', payload.get('reason', 'Expired'))
                elif kind == 'reminder':
                    user = self.bot.get_user(target) or await self.bot.fetch_user(target)
                    await user.send(f'Reminder from {guild.name}: {payload["text"][:1500]}', allowed_mentions=discord.AllowedMentions.none())
                elif kind == 'remove-role':
                    member = guild.get_member(target) or await guild.fetch_member(target)
                    role = guild.get_role(int(payload['role']))
                    if role and role in member.roles:
                        await member.remove_roles(role, reason='Temporary role expired')
                else:
                    raise ValueError('Unknown scheduled action')
                self.db.execute("UPDATE mod_jobs SET status='complete' WHERE id=? AND guild=?", (job_id, guild_id))
            except (discord.HTTPException, ValueError, KeyError) as exc:
                self.db.execute('UPDATE mod_jobs SET attempts=attempts+1,due=?,status=? WHERE id=? AND guild=?',
                    (time.time() + 300, 'failed' if attempts >= 2 else 'pending', job_id, guild_id))
                if attempts >= 2:
                    await self.log(guild, f'Scheduled action #{job_id} ({kind}) failed after three attempts: {type(exc).__name__}.')
            self.db.commit()

    @jobs.before_loop
    async def before_jobs(self):
        await self.bot.wait_until_ready()

    async def ready(self):
        if not self.jobs.is_running():
            self.jobs.start()

    async def close(self):
        self.jobs.cancel()
