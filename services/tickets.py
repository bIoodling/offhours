"""Private, guild-scoped support tickets backed by the existing SQLite connection."""
import asyncio
import contextlib
import datetime
import html
import io
import json
import logging
import re
import time
from collections import defaultdict
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks

log = logging.getLogger('offhours.tickets')


class TicketError(ValueError):
    pass


async def reply(interaction, message, **kwargs):
    kwargs.setdefault('ephemeral', True)
    kwargs.setdefault('allowed_mentions', discord.AllowedMentions.none())
    if interaction.response.is_done():
        return await interaction.followup.send(message[:1900], **kwargs)
    return await interaction.response.send_message(message[:1900], **kwargs)


class TicketService:
    def __init__(self, bot, connection, get_setting, put_setting):
        self.bot = bot
        self.db = connection
        self.get_setting = get_setting
        self.put_setting = put_setting
        self.locks = defaultdict(asyncio.Lock)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS ticket_panels (
          id INTEGER PRIMARY KEY AUTOINCREMENT, guild INTEGER NOT NULL,
          name TEXT NOT NULL, config TEXT NOT NULL, UNIQUE(guild,name));
        CREATE TABLE IF NOT EXISTS ticket_records (
          id INTEGER PRIMARY KEY AUTOINCREMENT, guild INTEGER NOT NULL,
          channel INTEGER, owner INTEGER NOT NULL, panel INTEGER NOT NULL,
          assigned INTEGER, status TEXT NOT NULL DEFAULT 'open', reason TEXT NOT NULL,
          priority TEXT NOT NULL DEFAULT 'normal', waiting TEXT NOT NULL DEFAULT 'staff',
          created REAL NOT NULL, updated REAL NOT NULL, closed REAL,
          first_response REAL, rating INTEGER, reminded REAL DEFAULT 0);
        CREATE UNIQUE INDEX IF NOT EXISTS ticket_channel ON ticket_records(guild,channel);
        CREATE INDEX IF NOT EXISTS ticket_owner ON ticket_records(guild,owner,status);
        CREATE TABLE IF NOT EXISTS ticket_notes (
          id INTEGER PRIMARY KEY AUTOINCREMENT, guild INTEGER NOT NULL,
          ticket INTEGER NOT NULL, author INTEGER NOT NULL, content TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS ticket_members (
          guild INTEGER NOT NULL, ticket INTEGER NOT NULL, user INTEGER NOT NULL,
          PRIMARY KEY(guild,ticket,user));
        CREATE TABLE IF NOT EXISTS ticket_blacklist (
          guild INTEGER NOT NULL, user INTEGER NOT NULL, reason TEXT NOT NULL,
          PRIMARY KEY(guild,user));
        CREATE TABLE IF NOT EXISTS ticket_responses (
          guild INTEGER NOT NULL, name TEXT NOT NULL, content TEXT NOT NULL,
          PRIMARY KEY(guild,name));
        ''')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(ticket_records)')}
        if 'support_roles' not in columns:
            self.db.execute("ALTER TABLE ticket_records ADD COLUMN support_roles TEXT NOT NULL DEFAULT '[]'")
        if 'route' not in columns:
            self.db.execute("ALTER TABLE ticket_records ADD COLUMN route TEXT NOT NULL DEFAULT ''")
        self.db.commit()

    def query(self, sql, args=()):
        cursor = self.db.execute(sql, args)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def panel(self, guild, panel_id):
        rows = self.query('SELECT * FROM ticket_panels WHERE guild=? AND id=?', (guild, panel_id))
        if not rows:
            raise TicketError('That ticket panel does not belong to this server.')
        row = rows[0]
        row['config'] = json.loads(row['config'])
        return row

    def panels(self, guild):
        return self.query('SELECT id,name,config FROM ticket_panels WHERE guild=? ORDER BY id LIMIT 25', (guild,))

    def save_panel(self, guild, name, config, panel_id=None):
        if panel_id:
            self.panel(guild, panel_id)
            self.db.execute('UPDATE ticket_panels SET name=?,config=? WHERE guild=? AND id=?',
                            (name[:80], json.dumps(config), guild, panel_id))
        else:
            cursor = self.db.execute('INSERT INTO ticket_panels(guild,name,config) VALUES(?,?,?)',
                                     (guild, name[:80], json.dumps(config)))
            panel_id = cursor.lastrowid
        self.db.commit()
        return panel_id

    def create_panel(self, guild, name):
        if len(self.panels(guild)) >= 25:
            raise TicketError('This server already has 25 ticket panels.')
        config = {'enabled': False, 'category': 0, 'channel': 0, 'roles': [],
                  'title': 'Support', 'description': 'Open a private ticket to contact support.',
                  'button': 'Open ticket', 'colour': 0xD4D4D4, 'questions': [],
                  'limit': 1, 'cooldown': 300, 'naming': 'ticket-{id}',
                  'autoclose': 0, 'reminder': 0}
        return self.save_panel(guild, name, config)

    @staticmethod
    def admin(member):
        return isinstance(member, discord.Member) and (member.guild_permissions.administrator or member.guild_permissions.manage_guild)

    def staff(self, member, record):
        if not isinstance(member, discord.Member) or member.guild.id != record['guild']:
            return False
        if self.admin(member):
            return True
        snapshot = json.loads(record.get('support_roles') or '[]')
        roles = snapshot or self.panel(record['guild'], record['panel'])['config'].get('roles', [])
        return member.id == record['assigned'] or any(r.id in roles for r in member.roles)

    def record(self, guild, channel):
        rows = self.query('SELECT * FROM ticket_records WHERE guild=? AND channel=?', (guild, channel))
        if not rows:
            raise TicketError('Use this command inside a ticket channel belonging to this server.')
        return rows[0]

    def authorized(self, interaction, staff=False):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            raise TicketError('Use this command in the server’s ticket channel.')
        record = self.record(interaction.guild_id, interaction.channel_id)
        is_staff = self.staff(interaction.user, record)
        added = self.db.execute('SELECT 1 FROM ticket_members WHERE guild=? AND ticket=? AND user=?',
            (record['guild'], record['id'], interaction.user.id)).fetchone()
        if not is_staff and (staff or (interaction.user.id != record['owner'] and not added)):
            raise TicketError('Only the ticket owner, authorized participants and support staff can do that.')
        return record

    def update(self, record, **values):
        allowed = {'assigned', 'status', 'reason', 'priority', 'waiting', 'updated', 'closed', 'first_response', 'rating', 'owner', 'reminded'}
        if not values or not set(values) <= allowed:
            raise TicketError('Invalid ticket update.')
        self.db.execute('UPDATE ticket_records SET ' + ','.join(k + '=?' for k in values) + ' WHERE guild=? AND id=?',
                        (*values.values(), record['guild'], record['id']))
        self.db.commit()

    @staticmethod
    async def member(guild, user_id):
        cached = guild.get_member(user_id)
        if cached:
            return cached
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            return None

    async def participant_permissions(self, guild, channel, record, can_send):
        users = {record['owner']}
        users.update(r['user'] for r in self.query('SELECT user FROM ticket_members WHERE guild=? AND ticket=?',
                                                  (record['guild'], record['id'])))
        for user_id in users:
            member = await self.member(guild, user_id)
            if member and not self.staff(member, record):
                await channel.set_permissions(member, view_channel=True, send_messages=can_send,
                    read_message_history=True, reason='Ticket participant messaging state updated')

    async def log(self, guild, message):
        channel_id = self.get_setting(guild.id, 'ticket:logs', '0')
        channel = guild.get_channel(int(channel_id)) if str(channel_id).isdigit() else None
        if isinstance(channel, discord.TextChannel) and self.private_staff_channel(guild, channel):
            with contextlib.suppress(discord.HTTPException):
                await channel.send(message[:1900], allowed_mentions=discord.AllowedMentions.none(), silent=True)

    def private_staff_channel(self, guild, channel):
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id or channel.permissions_for(guild.default_role).view_channel:
            return False
        support = set()
        for row in self.panels(guild.id):
            config = json.loads(row['config'])
            support.update(config.get('roles', []))
            for route in config.get('routes', []):
                support.update(route.get('roles', []))
        support.update(role.id for role in guild.me.roles if role.managed)
        for role in guild.roles:
            if role.is_default() or role.id in support or role.permissions.administrator or role.permissions.manage_guild:
                continue
            if channel.permissions_for(role).view_channel:
                return False
        for target, overwrite in channel.overwrites.items():
            if isinstance(target, discord.Member) and overwrite.view_channel and target.id != guild.me.id:
                if not self.admin(target) and not any(r.id in support for r in target.roles):
                    return False
        return True

    async def open(self, interaction, panel_id, reason, route=''):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            raise TicketError('Open tickets from a server panel.')
        guild = interaction.guild
        async with self.locks[guild.id]:
            panel = self.panel(guild.id, panel_id)
            config = panel['config']
            if route:
                choices = [r for r in config.get('routes', []) if r['label'] == route]
                if not choices:
                    raise TicketError('That ticket category is no longer available. Reopen the panel.')
                config = {**config, **{k: v for k, v in choices[0].items() if k in ('category', 'roles', 'questions')}}
            enabled = self.get_setting(guild.id, 'module:tickets', '0')
            if enabled.lower() not in ('true', '1', 'yes', 'on') or not config.get('enabled'):
                raise TicketError('This ticket panel is disabled. Ask a server administrator to configure it.')
            category = guild.get_channel(int(config.get('category', 0)))
            if not isinstance(category, discord.CategoryChannel):
                raise TicketError('An administrator must select an existing ticket category first.')
            if not guild.me.guild_permissions.manage_channels:
                raise TicketError('The bot needs Manage Channels permission to create a private ticket.')
            support = [guild.get_role(int(role_id)) for role_id in config.get('roles', [])]
            support = [r for r in support if r and not r.is_default()]
            if not support:
                raise TicketError('An administrator must select an existing support role first.')
            if self.db.execute('SELECT 1 FROM ticket_blacklist WHERE guild=? AND user=?', (guild.id, interaction.user.id)).fetchone():
                raise TicketError('You cannot open tickets in this server. Contact a server administrator.')
            opened = self.db.execute("SELECT COUNT(*) FROM ticket_records WHERE guild=? AND owner=? AND status IN ('open','locked')",
                                     (guild.id, interaction.user.id)).fetchone()[0]
            if opened >= max(1, int(config.get('limit', 1))):
                raise TicketError('You have reached this server’s active ticket limit.')
            last = self.db.execute('SELECT MAX(created) FROM ticket_records WHERE guild=? AND owner=?',
                                   (guild.id, interaction.user.id)).fetchone()[0] or 0
            remaining = int(config.get('cooldown', 300)) - (time.time() - last)
            if remaining > 0:
                raise TicketError(f'Please wait {int(remaining) + 1} seconds before opening another ticket.')
            hours = config.get('hours')
            if hours:
                now = datetime.datetime.now(ZoneInfo(hours['timezone']))
                start, end = hours['start'], hours['end']
                allowed = start <= now.hour < end if start < end else now.hour >= start or now.hour < end
                if not allowed:
                    raise TicketError(f'Support is open {start:02d}:00–{end:02d}:00 ({hours["timezone"]}).')
            await interaction.response.defer(ephemeral=True, thinking=True)
            now = time.time()
            cursor = self.db.execute('INSERT INTO ticket_records(guild,owner,panel,status,reason,created,updated) VALUES(?,?,?,?,?,?,?)',
                (guild.id, interaction.user.id, panel_id, 'creating', reason[:4000], now, now))
            ticket_id = cursor.lastrowid
            self.db.commit()
            name = config.get('naming', 'ticket-{id}').replace('{id}', str(ticket_id)).replace('{user}', interaction.user.name)
            name = re.sub(r'[^a-z0-9-]', '-', name.lower()).strip('-')[:90] or f'ticket-{ticket_id}'
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True),
                interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
            }
            for role in support:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True)
            try:
                channel = await guild.create_text_channel(name, category=category, overwrites=overwrites,
                    topic=f'Private support ticket {ticket_id} • owner {interaction.user.id}', reason='Member deliberately opened a support ticket')
            except BaseException:
                self.db.execute('DELETE FROM ticket_records WHERE guild=? AND id=? AND status=?', (guild.id, ticket_id, 'creating'))
                self.db.commit()
                raise
            self.db.execute("UPDATE ticket_records SET channel=?,status='open',support_roles=?,route=? WHERE guild=? AND id=?",
                            (channel.id, json.dumps([r.id for r in support]), route, guild.id, ticket_id))
            self.db.commit()
            record = self.record(guild.id, channel.id)
            with contextlib.suppress(discord.HTTPException):
                await channel.send(embed=discord.Embed(title=f'Ticket #{ticket_id} · {panel["name"]}',
                    description=f'Owner: {interaction.user.mention}\n\n{discord.utils.escape_markdown(reason)[:3500]}', colour=config.get('colour', 0xD4D4D4)),
                    view=TicketControls(self), allowed_mentions=discord.AllowedMentions.none(), silent=True)
            await reply(interaction, f'Your private ticket is open: {channel.mention}')
            await self.log(guild, f'Ticket #{ticket_id} opened by user {interaction.user.id}; channel {channel.id}.')
            return record

    async def close_ticket(self, interaction, reason='Closed by request'):
        record = self.authorized(interaction)
        if interaction.user.id != record['owner'] and not self.staff(interaction.user, record):
            raise TicketError('Only the ticket owner or support staff can close this ticket.')
        if record['status'] == 'closed':
            raise TicketError('This ticket is already closed.')
        channel = interaction.channel
        await self.participant_permissions(interaction.guild, channel, record, False)
        self.update(record, status='closed', closed=time.time(), updated=time.time())
        await channel.send(f'Ticket closed by {interaction.user.mention}. {discord.utils.escape_mentions(reason)[:500]}',
                           view=TicketControls(self), allowed_mentions=discord.AllowedMentions.none(), silent=True)
        await self.log(interaction.guild, f'Ticket #{record["id"]} closed by user {interaction.user.id}.')
        return record

    async def transcript(self, interaction, format='txt'):
        record = self.authorized(interaction)
        # Stop before Discord upload limits and bound memory on the 1 GB host.
        maximum = min(8_000_000, max(100_000, interaction.guild.filesize_limit - 20_000))
        content = []
        size = 0
        async for message in interaction.channel.history(limit=10000, oldest_first=True):
            attachments = ' '.join(a.url for a in message.attachments)
            text = f'[{message.created_at.isoformat()}] {message.author} ({message.author.id}): {message.content}'
            if message.embeds:
                text += '\n' + '\n'.join((e.title or '') + '\n' + (e.description or '') for e in message.embeds)
            if attachments:
                text += '\nAttachments: ' + attachments
            size += len(text.encode('utf-8'))
            if size > maximum // (6 if format == 'html' else 1):
                content.append('[Export stopped at the safe upload limit. Older retained channel history remains available to authorized users.]')
                break
            content.append(text)
        output = '\n\n'.join(content)
        if format == 'html':
            output = '<!doctype html><html><head><meta charset="utf-8"><title>Private ticket transcript</title><style>body{background:#17191c;color:#eee;font:14px system-ui;max-width:1100px;margin:32px auto}pre{white-space:pre-wrap;word-break:break-word}</style></head><body><h1>Private ticket #' + str(record['id']) + '</h1><pre>' + html.escape(output) + '</pre></body></html>'
        return discord.File(io.BytesIO(output.encode('utf-8')), filename=f'ticket-{record["id"]}.{format}')

    async def dispatch(self, interaction, action):
        try:
            record = self.authorized(interaction, staff=action in ('claim', 'reopen'))
            await interaction.response.defer(ephemeral=True)
            if action == 'close':
                await self.close_ticket(interaction)
                return await reply(interaction, 'Ticket closed. History remains private in this channel.')
            if action == 'claim':
                async with self.locks[interaction.guild_id]:
                    record = self.authorized(interaction, staff=True)
                    if record['assigned'] and record['assigned'] != interaction.user.id:
                        raise TicketError('This ticket has already been claimed by another support member.')
                    self.update(record, assigned=interaction.user.id)
                return await reply(interaction, 'Ticket assigned to you.')
            if action == 'reopen':
                if record['status'] != 'closed':
                    raise TicketError('This ticket is not closed.')
                owner = await self.member(interaction.guild, record['owner'])
                if not owner:
                    raise TicketError('The ticket owner is no longer in this server.')
                await self.participant_permissions(interaction.guild, interaction.channel, record, True)
                self.update(record, status='open', closed=None, updated=time.time())
                return await reply(interaction, 'Ticket reopened.')
            if action == 'transcript':
                return await reply(interaction, 'Private transcript attached.', file=await self.transcript(interaction))
        except TicketError as exc:
            await reply(interaction, str(exc))
        except discord.HTTPException:
            await reply(interaction, 'Discord rejected that ticket action. Check the bot’s channel permissions.')

    async def on_message(self, message):
        if not message.guild or message.author.bot:
            return
        rows = self.query("SELECT * FROM ticket_records WHERE guild=? AND channel=? AND status IN ('open','locked')", (message.guild.id, message.channel.id))
        if not rows:
            return
        record = rows[0]
        values = {'updated': time.time(), 'reminded': 0}
        if self.staff(message.author, record):
            values['waiting'] = 'member'
            if not record['first_response']:
                values['first_response'] = time.time()
        elif message.author.id == record['owner']:
            values['waiting'] = 'staff'
        self.update(record, **values)

    @tasks.loop(minutes=5)
    async def maintenance(self):
        rows = self.query("SELECT * FROM ticket_records WHERE status IN ('open','locked') ORDER BY updated LIMIT 200")
        for record in rows:
            guild = self.bot.get_guild(record['guild'])
            if not guild:
                continue
            channel = guild.get_channel(record['channel'])
            if not isinstance(channel, discord.TextChannel):
                continue
            try:
                config = self.panel(guild.id, record['panel'])['config']
                elapsed = time.time() - record['updated']
                autoclose = int(config.get('autoclose', 0)) * 3600
                reminder = int(config.get('reminder', 0)) * 3600
                if autoclose and elapsed >= autoclose:
                    await self.participant_permissions(guild, channel, record, False)
                    self.update(record, status='closed', closed=time.time())
                    await channel.send('This ticket was closed after the configured inactivity period.',
                        view=TicketControls(self), allowed_mentions=discord.AllowedMentions.none(), silent=True)
                elif reminder and elapsed >= reminder and not record['reminded']:
                    await channel.send('This ticket is waiting for a response. Please update it when you can.',
                                       allowed_mentions=discord.AllowedMentions.none(), silent=True)
                    self.update(record, reminded=time.time())
            except (TicketError, discord.HTTPException, ValueError):
                log.warning('Ticket maintenance could not update ticket %s in guild %s', record['id'], record['guild'])

    async def ready(self):
        if not getattr(self, '_registered', False):
            self.bot.add_view(TicketControls(self))
            for row in self.query('SELECT id,guild FROM ticket_panels'):
                self.bot.add_view(OpenTicketView(self, row['guild'], row['id']))
            self._registered = True
        if not self.maintenance.is_running():
            self.maintenance.start()

    async def close(self):
        self.maintenance.cancel()


class TicketControls(discord.ui.View):
    def __init__(self, service):
        super().__init__(timeout=None)
        for label, action in [('Claim', 'claim'), ('Close', 'close'), ('Reopen', 'reopen'), ('Transcript', 'transcript')]:
            button = discord.ui.Button(label=label, custom_id='offhours:ticket:v1:' + action,
                style=discord.ButtonStyle.danger if action == 'close' else discord.ButtonStyle.secondary)
            async def callback(interaction, action=action):
                await service.dispatch(interaction, action)
            button.callback = callback
            self.add_item(button)


class OpeningForm(discord.ui.Modal):
    def __init__(self, service, guild_id, panel_id, route=''):
        super().__init__(title='Open a private ticket', timeout=600)
        self.service, self.guild_id, self.panel_id = service, guild_id, panel_id
        self.route = route
        self.reason = discord.ui.TextInput(label='How can we help?', style=discord.TextStyle.paragraph, max_length=1500)
        self.add_item(self.reason)
        panel = service.panel(guild_id, panel_id)
        self.questions = []
        config = panel['config']
        for item in config.get('routes', []):
            if item['label'] == route:
                config = {**config, **item}
                break
        for question in config.get('questions', [])[:4]:
            field = discord.ui.TextInput(label=str(question)[:45], style=discord.TextStyle.paragraph, max_length=500)
            self.questions.append(field)
            self.add_item(field)

    async def on_submit(self, interaction):
        try:
            if interaction.guild_id != self.guild_id:
                raise TicketError('This form belongs to a different server.')
            reason = str(self.reason) + ''.join('\n\n' + q.label + ': ' + str(q) for q in self.questions)
            await self.service.open(interaction, self.panel_id, reason, self.route)
        except TicketError as exc:
            await reply(interaction, str(exc))
        except discord.HTTPException:
            await reply(interaction, 'Discord could not create your ticket. Check the configured category and bot permissions.')


class OpenTicketView(discord.ui.View):
    def __init__(self, service, guild_id, panel_id):
        super().__init__(timeout=None)
        try:
            config = service.panel(guild_id, panel_id)['config']
            label = config.get('button', 'Open ticket')
        except TicketError:
            config = {}
            label = 'Open ticket'
        routes = config.get('routes', [])[:25]
        if routes:
            menu = discord.ui.Select(placeholder=label[:150], custom_id=f'offhours:ticket:menu:{guild_id}:{panel_id}',
                options=[discord.SelectOption(label=r['label'][:100], value=str(index), description=r.get('description', '')[:100] or None)
                         for index, r in enumerate(routes)])
            async def choose(interaction):
                try:
                    if interaction.guild_id != guild_id:
                        raise TicketError('This ticket panel belongs to another server.')
                    # Resolve current configuration rather than trusting stale displayed menu entries.
                    current = service.panel(guild_id, panel_id)['config'].get('routes', [])
                    index = int(menu.values[0])
                    if index >= len(current) or current[index]['label'] != routes[index]['label']:
                        raise TicketError('This category menu changed. Ask staff to republish the current ticket panel.')
                    await interaction.response.send_modal(OpeningForm(service, guild_id, panel_id, current[index]['label']))
                except (TicketError, ValueError) as exc:
                    await reply(interaction, str(exc))
            menu.callback = choose
            self.add_item(menu)
            return
        button = discord.ui.Button(label=label[:80], style=discord.ButtonStyle.secondary,
                                   custom_id=f'offhours:ticket:open:{guild_id}:{panel_id}')
        async def callback(interaction):
            try:
                if interaction.guild_id != guild_id:
                    raise TicketError('This ticket panel belongs to another server.')
                await interaction.response.send_modal(OpeningForm(service, guild_id, panel_id))
            except TicketError as exc:
                await reply(interaction, str(exc))
        button.callback = callback
        self.add_item(button)
