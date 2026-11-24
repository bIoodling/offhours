"""Interactive ticket configuration and real support actions using Discord's API."""
import contextlib
import datetime
import io
import json
import re
import sqlite3
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands

from services.tickets import TicketService, TicketError, TicketControls, OpenTicketView, OpeningForm, reply


class PanelTextModal(discord.ui.Modal):
    def __init__(self, editor):
        super().__init__(title='Ticket panel text')
        self.editor = editor
        config = editor.config
        self.fields = {}
        for name, label, maximum in [('title', 'Title', 200), ('description', 'Description', 3500),
                                      ('button', 'Open button label', 80), ('colour', 'Hex colour', 7),
                                      ('naming', 'Ticket name ({id}, {user})', 90)]:
            value = f'#{config.get(name, 0xD4D4D4):06x}' if name == 'colour' else str(config.get(name, ''))
            field = discord.ui.TextInput(label=label, default=value, max_length=maximum,
                style=discord.TextStyle.paragraph if name == 'description' else discord.TextStyle.short)
            self.fields[name] = field
            self.add_item(field)

    async def on_submit(self, interaction):
        if not await self.editor.interaction_check(interaction):
            return
        colour = str(self.fields['colour']).lstrip('#')
        if not re.fullmatch('[0-9A-Fa-f]{6}', colour):
            return await reply(interaction, 'Use a six-digit hex colour such as #D4D4D4.')
        for key, field in self.fields.items():
            self.editor.config[key] = int(colour, 16) if key == 'colour' else str(field)
        await interaction.response.edit_message(embed=self.editor.preview(), view=self.editor)


class TicketEditor(discord.ui.View):
    def __init__(self, service, guild, user, panel_id):
        super().__init__(timeout=900)
        self.service, self.guild, self.user, self.panel_id = service, guild, user, panel_id
        panel = service.panel(guild.id, panel_id)
        self.name, self.config = panel['name'], panel['config']
        options = [discord.SelectOption(label=row['name'][:100], value=str(row['id']), default=row['id'] == panel_id)
                   for row in service.panels(guild.id)]
        chooser = discord.ui.Select(placeholder='Choose a ticket panel', options=options, row=0)
        async def choose(interaction):
            replacement = TicketEditor(service, guild, user, int(chooser.values[0]))
            await interaction.response.edit_message(embed=replacement.preview(), view=replacement)
        chooser.callback = choose
        self.add_item(chooser)
        category = discord.ui.ChannelSelect(placeholder='Select existing ticket category', channel_types=[discord.ChannelType.category], row=1)
        async def select_category(interaction):
            self.config['category'] = category.values[0].id
            await interaction.response.edit_message(embed=self.preview(), view=self)
        category.callback = select_category
        self.add_item(category)
        roles = discord.ui.RoleSelect(placeholder='Select support roles (never @everyone)', min_values=1, max_values=10, row=2)
        async def select_roles(interaction):
            if any(r.is_default() or r.managed for r in roles.values):
                return await reply(interaction, 'Select regular support roles, excluding @everyone and integration-managed roles.')
            self.config['roles'] = [r.id for r in roles.values]
            await interaction.response.edit_message(embed=self.preview(), view=self)
        roles.callback = select_roles
        self.add_item(roles)
        destination = discord.ui.ChannelSelect(placeholder='Select existing panel destination', channel_types=[discord.ChannelType.text], row=3)
        async def select_destination(interaction):
            self.config['channel'] = destination.values[0].id
            await interaction.response.edit_message(embed=self.preview(), view=self)
        destination.callback = select_destination
        self.add_item(destination)
        for label, action in [('Enable / Disable', 'toggle'), ('Edit text', 'text'), ('Save', 'save'), ('Publish', 'publish'), ('Cancel', 'cancel')]:
            button = discord.ui.Button(label=label, custom_id='ticket-edit:' + action, row=4,
                style=discord.ButtonStyle.primary if action in ('save', 'publish') else discord.ButtonStyle.secondary)
            async def callback(interaction, action=action):
                await self.handle(interaction, action)
            button.callback = callback
            self.add_item(button)

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild.id or interaction.user.id != self.user or not self.service.admin(interaction.user):
            await reply(interaction, 'Only the administrator who opened this server’s editor can change it.')
            return False
        return True

    def preview(self):
        embed = discord.Embed(title=self.config.get('title', 'Support')[:200],
            description=self.config.get('description', '')[:3500], colour=self.config.get('colour', 0xD4D4D4))
        embed.set_footer(text=f'Private draft · {self.name} · ' + ('Enabled' if self.config.get('enabled') else 'Disabled'))
        embed.add_field(name='Ticket category', value=f'<#{self.config.get("category", 0)}>')
        embed.add_field(name='Panel destination', value=f'<#{self.config.get("channel", 0)}>')
        embed.add_field(name='Support roles', value=', '.join(f'<@&{r}>' for r in self.config.get('roles', [])) or 'Not selected', inline=False)
        return embed

    async def handle(self, interaction, action):
        if action == 'toggle':
            self.config['enabled'] = not self.config.get('enabled', False)
            return await interaction.response.edit_message(embed=self.preview(), view=self)
        if action == 'text':
            return await interaction.response.send_modal(PanelTextModal(self))
        if action == 'cancel':
            self.stop()
            return await interaction.response.edit_message(content='Unsaved ticket panel changes discarded.', embed=None, view=None)
        if action in ('save', 'publish'):
            if self.config.get('enabled'):
                category = self.guild.get_channel(int(self.config.get('category', 0)))
                if not isinstance(category, discord.CategoryChannel) or not self.config.get('roles'):
                    return await reply(interaction, 'Select an existing ticket category and support roles before enabling this panel.')
            self.service.save_panel(self.guild.id, self.name, self.config, self.panel_id)
            if self.config.get('enabled'):
                self.service.put_setting(self.guild.id, 'module:tickets', '1')
            if action == 'save':
                return await reply(interaction, 'Ticket panel saved for this server.')
            channel = self.guild.get_channel(int(self.config.get('channel', 0)))
            if not isinstance(channel, discord.TextChannel):
                return await reply(interaction, 'Select an existing destination text channel first.')
            await interaction.response.defer(ephemeral=True)
            try:
                view = OpenTicketView(self.service, self.guild.id, self.panel_id)
                embed = discord.Embed(title=self.config.get('title', 'Support'), description=self.config.get('description', ''), colour=self.config.get('colour', 0xD4D4D4))
                message = await channel.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none(), silent=True)
                self.service.bot.add_view(view, message_id=message.id)
                await reply(interaction, f'Ticket panel published in {channel.mention}.')
            except discord.HTTPException:
                await reply(interaction, 'Discord could not publish the panel. Check Send Messages and Embed Links in the selected channel.')


class DeleteConfirmation(discord.ui.View):
    def __init__(self, service, user, guild, channel):
        super().__init__(timeout=60)
        self.service, self.user, self.guild, self.channel = service, user, guild, channel

    @discord.ui.button(label='Permanently delete this ticket channel', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        if interaction.user.id != self.user or interaction.guild_id != self.guild or interaction.channel_id != self.channel:
            return await reply(interaction, 'This confirmation belongs to another user or ticket.')
        try:
            record = self.service.authorized(interaction, staff=True)
            if not interaction.user.guild_permissions.manage_channels:
                raise TicketError('Manage Channels permission is required to delete a ticket channel.')
            if record['status'] != 'closed':
                raise TicketError('Close the ticket before deleting it.')
            await interaction.response.defer(ephemeral=True)
            destination_id = self.service.get_setting(self.guild, 'ticket:transcripts', '0')
            destination = interaction.guild.get_channel(int(destination_id)) if str(destination_id).isdigit() else None
            if destination:
                if not self.service.private_staff_channel(interaction.guild, destination):
                    raise TicketError('The configured transcript archive is public. Select a private staff channel before deleting.')
                await destination.send(f'Private transcript for ticket #{record["id"]}.', file=await self.service.transcript(interaction, 'html'),
                    allowed_mentions=discord.AllowedMentions.none(), silent=True)
            await interaction.channel.delete(reason=f'Confirmed ticket deletion by {interaction.user.id}')
            self.service.update(record, status='deleted', updated=time.time())
            self.stop()
        except TicketError as exc:
            await reply(interaction, str(exc))
        except discord.HTTPException:
            await reply(interaction, 'Discord rejected deletion or transcript archival. The ticket record has not been marked deleted.')


def install(bot, connection, get_setting, put_setting):
    service = TicketService(bot, connection, get_setting, put_setting)
    bot.ticket_service = service
    tickets = app_commands.Group(name='ticket', description='Private support tickets')
    config = app_commands.Group(name='ticket-config', description='Configure support tickets for this server', default_permissions=discord.Permissions(manage_guild=True))
    bot.tree.add_command(tickets)
    bot.tree.add_command(config)
    bot.add_listener(service.ready, 'on_ready')
    bot.add_listener(service.on_message, 'on_message')

    async def admin(interaction):
        if not interaction.guild or not service.admin(interaction.user):
            raise TicketError('Manage Server permission is required in this server.')

    async def error(interaction, exc):
        if isinstance(exc, TicketError):
            return await reply(interaction, str(exc))
        if isinstance(exc, app_commands.CommandInvokeError) and isinstance(exc.original, TicketError):
            return await reply(interaction, str(exc.original))
        if isinstance(exc, app_commands.CommandInvokeError) and isinstance(exc.original, discord.HTTPException):
            return await reply(interaction, 'Discord rejected that ticket operation. Check the bot’s channel permissions.')
        raise exc
    tickets.on_error = error
    config.on_error = error

    @bot.tree.command(name='setup-tickets', description='Privately configure existing channels, support roles and ticket panels')
    @app_commands.default_permissions(manage_guild=True)
    async def setup(interaction: discord.Interaction):
        if not interaction.guild or not service.admin(interaction.user):
            return await reply(interaction, 'Manage Server permission is required.')
        rows = service.panels(interaction.guild_id)
        panel_id = rows[0]['id'] if rows else service.create_panel(interaction.guild_id, 'General support')
        editor = TicketEditor(service, interaction.guild, interaction.user.id, panel_id)
        await interaction.response.send_message(embed=editor.preview(), view=editor, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @tickets.command(name='new', description='Open a private ticket using a configured panel')
    async def new(interaction: discord.Interaction, panel: int):
        if not interaction.guild:
            return await reply(interaction, 'Use this command in a server.')
        await interaction.response.send_modal(OpeningForm(service, interaction.guild_id, panel))

    @new.autocomplete('panel')
    async def panel_autocomplete(interaction, current):
        return [app_commands.Choice(name=f'{row["name"]} (#{row["id"]})'[:100], value=row['id'])
                for row in service.panels(interaction.guild_id) if current.casefold() in row['name'].casefold()]

    @tickets.command(name='close', description='Close this ticket while preserving its private history')
    async def close(interaction: discord.Interaction, reason: str = 'Closed by request'):
        service.authorized(interaction)
        await interaction.response.defer(ephemeral=True)
        await service.close_ticket(interaction, reason)
        await reply(interaction, 'Ticket closed. History remains private.')

    @tickets.command(name='close-request', description='Ask the owner or support staff to close this ticket')
    async def close_request(interaction: discord.Interaction, reason: str = 'Issue resolved'):
        record = service.authorized(interaction)
        await interaction.channel.send(f'Closure requested by {interaction.user.mention}: {discord.utils.escape_mentions(reason)[:500]}',
            view=TicketControls(service), allowed_mentions=discord.AllowedMentions.none(), silent=True)
        await reply(interaction, 'Closure request posted with ticket controls.')

    for name, description in [('reopen', 'Reopen a closed ticket (support staff)'), ('claim', 'Assign this ticket to yourself (support staff)')]:
        async def callback(interaction: discord.Interaction, _action=name):
            await service.dispatch(interaction, _action)
        # Default closure parameters must not become Discord slash parameters.
        def make_callback(action):
            async def callback(interaction: discord.Interaction):
                await service.dispatch(interaction, action)
            return callback
        tickets.add_command(app_commands.Command(name=name, description=description, callback=make_callback(name)))

    @tickets.command(name='delete', description='Confirm permanent deletion of a closed ticket channel')
    async def delete(interaction: discord.Interaction):
        record = service.authorized(interaction, staff=True)
        if not interaction.user.guild_permissions.manage_channels:
            raise TicketError('Manage Channels permission is required.')
        if record['status'] != 'closed':
            raise TicketError('Close this ticket first. Export a transcript before deletion if no archive is configured.')
        await reply(interaction, 'This permanently deletes this closed ticket channel and its Discord history. Confirm within 60 seconds.',
                    view=DeleteConfirmation(service, interaction.user.id, interaction.guild_id, interaction.channel_id))

    @tickets.command(name='unclaim', description='Remove this ticket’s staff assignment')
    async def unclaim(interaction: discord.Interaction):
        record = service.authorized(interaction, staff=True)
        if record['assigned'] not in (None, interaction.user.id) and not service.admin(interaction.user):
            raise TicketError('Only the assigned staff member or a server administrator can unclaim this ticket.')
        service.update(record, assigned=None)
        await reply(interaction, 'Ticket assignment removed.')

    @tickets.command(name='add', description='Give an existing server member access to this ticket')
    async def add(interaction: discord.Interaction, member: discord.Member):
        record = service.authorized(interaction, staff=True)
        await interaction.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True,
            reason=f'Ticket participant added by {interaction.user.id}')
        connection.execute('INSERT OR IGNORE INTO ticket_members VALUES(?,?,?)', (interaction.guild_id, record['id'], member.id))
        connection.commit()
        await reply(interaction, 'Member granted access to this ticket.')

    @tickets.command(name='remove', description='Remove an added participant’s access to this ticket')
    async def remove(interaction: discord.Interaction, member: discord.Member):
        record = service.authorized(interaction, staff=True)
        if member.id == record['owner'] or service.staff(member, record) or member.id == interaction.guild.me.id:
            raise TicketError('The ticket owner, authorized staff and bot cannot be removed using this command.')
        await interaction.channel.set_permissions(member, view_channel=False, reason=f'Ticket participant removed by {interaction.user.id}')
        connection.execute('DELETE FROM ticket_members WHERE guild=? AND ticket=? AND user=?', (interaction.guild_id, record['id'], member.id))
        connection.commit()
        await reply(interaction, 'Participant’s ticket access removed.')

    @tickets.command(name='rename', description='Deliberately rename this ticket channel')
    async def rename(interaction: discord.Interaction, name: str):
        service.authorized(interaction, staff=True)
        if not interaction.user.guild_permissions.manage_channels:
            raise TicketError('Manage Channels permission is required to rename a ticket channel.')
        await interaction.channel.edit(name=name[:100], reason=f'Ticket renamed by {interaction.user.id}')
        await reply(interaction, 'Ticket channel renamed.')

    @tickets.command(name='priority', description='Set the support priority for this ticket')
    @app_commands.choices(priority=[app_commands.Choice(name=x.title(), value=x) for x in ('low', 'normal', 'high', 'urgent')])
    async def priority(interaction: discord.Interaction, priority: app_commands.Choice[str]):
        record = service.authorized(interaction, staff=True)
        service.update(record, priority=priority.value)
        await reply(interaction, f'Priority set to {priority.value}.')

    @tickets.command(name='transfer', description='Reassign this ticket to an authorized support member')
    async def transfer(interaction: discord.Interaction, member: discord.Member):
        record = service.authorized(interaction, staff=True)
        candidate = dict(record, assigned=None)
        if not service.staff(member, candidate):
            raise TicketError('Choose a member with one of the configured support roles or Manage Server permission.')
        await interaction.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True,
            reason=f'Ticket reassigned by {interaction.user.id}')
        service.update(record, assigned=member.id)
        await reply(interaction, 'Ticket reassigned.')

    @tickets.command(name='note', description='Save or privately read staff-only ticket notes')
    async def note(interaction: discord.Interaction, text: str = ''):
        record = service.authorized(interaction, staff=True)
        if text:
            connection.execute('INSERT INTO ticket_notes(guild,ticket,author,content,created) VALUES(?,?,?,?,?)',
                               (interaction.guild_id, record['id'], interaction.user.id, text[:4000], time.time()))
            connection.commit()
            return await reply(interaction, 'Private staff note saved.')
        rows = service.query('SELECT author,content FROM ticket_notes WHERE guild=? AND ticket=? ORDER BY id DESC LIMIT 10', (interaction.guild_id, record['id']))
        await reply(interaction, '\n\n'.join(f'Staff {r["author"]}: {r["content"][:300]}' for r in rows) or 'No staff notes recorded.')

    @tickets.command(name='transcript', description='Export this private ticket as TXT or HTML')
    @app_commands.choices(format=[app_commands.Choice(name='TXT', value='txt'), app_commands.Choice(name='HTML', value='html')])
    async def transcript(interaction: discord.Interaction, format: str = 'txt'):
        service.authorized(interaction)
        if format not in ('txt', 'html'):
            raise TicketError('Choose TXT or HTML.')
        await interaction.response.defer(ephemeral=True, thinking=True)
        await reply(interaction, 'Private transcript attached.', file=await service.transcript(interaction, format))

    @tickets.command(name='list', description='List your tickets, or this server’s tickets if you are support staff')
    async def list_tickets(interaction: discord.Interaction):
        if not interaction.guild:
            raise TicketError('Use this command in a server.')
        rows = service.query('SELECT * FROM ticket_records WHERE guild=? ORDER BY id DESC LIMIT 100', (interaction.guild_id,))
        visible = [r for r in rows if r['owner'] == interaction.user.id or service.staff(interaction.user, r)]
        await reply(interaction, '\n'.join(f'#{r["id"]} · {r["status"]} · <#{r["channel"]}> · {r["priority"]}' for r in visible[:20]) or 'No visible tickets in this server.')

    @tickets.command(name='info', description='Inspect this ticket’s owner, assignment, state and response time')
    async def info(interaction: discord.Interaction):
        record = service.authorized(interaction)
        response = int(record['first_response'] - record['created']) if record['first_response'] else None
        await reply(interaction, f'Ticket #{record["id"]}\nOwner: <@{record["owner"]}>\nAssigned: {record["assigned"] or "Unclaimed"}\n'
            f'Status: {record["status"]}\nPriority: {record["priority"]}\nWaiting for: {record["waiting"]}\n'
            f'Created: <t:{int(record["created"])}:F>\nFirst staff response: {str(response) + " seconds" if response is not None else "Not yet recorded"}')

    def add_lock(name, enabled):
        async def callback(interaction: discord.Interaction):
            record = service.authorized(interaction, staff=True)
            if record['status'] == 'closed':
                raise TicketError('Reopen this closed ticket before changing its lock.')
            owner = await service.member(interaction.guild, record['owner'])
            if not owner:
                raise TicketError('The ticket owner is no longer in this server.')
            await service.participant_permissions(interaction.guild, interaction.channel, record, not enabled)
            service.update(record, status='locked' if enabled else 'open')
            await reply(interaction, f'Ticket {name} applied.')
        tickets.add_command(app_commands.Command(name=name, description=f'{name.title()} ticket owner messages (support staff)', callback=callback))
    add_lock('lock', True)
    add_lock('unlock', False)

    @tickets.command(name='wait', description='Set whether this ticket is waiting for staff or the member')
    @app_commands.choices(for_whom=[app_commands.Choice(name='Staff', value='staff'), app_commands.Choice(name='Member', value='member')])
    async def wait(interaction: discord.Interaction, for_whom: app_commands.Choice[str]):
        record = service.authorized(interaction, staff=True)
        service.update(record, waiting=for_whom.value)
        await reply(interaction, f'Ticket is waiting for {for_whom.value}.')

    @tickets.command(name='rating', description='Rate the support in your closed ticket from one to five')
    async def rating(interaction: discord.Interaction, stars: app_commands.Range[int, 1, 5]):
        record = service.authorized(interaction)
        if record['owner'] != interaction.user.id or record['status'] != 'closed':
            raise TicketError('Only the owner of a closed ticket can rate its support.')
        service.update(record, rating=stars)
        await reply(interaction, f'Support rating saved: {stars}/5.')

    @config.command(name='panels', description='List, create or delete a ticket panel configuration')
    @app_commands.choices(action=[app_commands.Choice(name=x.title(), value=x) for x in ('list', 'create', 'delete')])
    async def panels(interaction: discord.Interaction, action: str = 'list', name: str = '', panel: int = 0):
        await admin(interaction)
        if action == 'create':
            if not name.strip():
                raise TicketError('Provide a name for the new panel.')
            try:
                panel_id = service.create_panel(interaction.guild_id, name.strip())
            except sqlite3.IntegrityError:
                raise TicketError('A panel with that name already exists in this server.')
            editor = TicketEditor(service, interaction.guild, interaction.user.id, panel_id)
            return await interaction.response.send_message(embed=editor.preview(), view=editor, ephemeral=True)
        if action == 'delete':
            service.panel(interaction.guild_id, panel)
            count = connection.execute("SELECT COUNT(*) FROM ticket_records WHERE guild=? AND panel=? AND status NOT IN ('deleted','closed')", (interaction.guild_id, panel)).fetchone()[0]
            if count:
                raise TicketError('Close all active tickets from this panel before deleting its configuration.')
            # Preserve the config needed to authorize historical tickets: disable instead of destroying it.
            row = service.panel(interaction.guild_id, panel)
            row['config']['enabled'] = False
            service.save_panel(interaction.guild_id, row['name'], row['config'], panel)
            return await reply(interaction, 'Panel disabled; retained configuration protects historical ticket access.')
        rows = service.panels(interaction.guild_id)
        await reply(interaction, '\n'.join(f'#{r["id"]}: {r["name"]}' for r in rows) or 'No ticket panels. Use /setup-tickets to configure one.')

    @config.command(name='categories', description='Select an existing category for a ticket panel')
    async def categories(interaction: discord.Interaction, panel: int, category: discord.CategoryChannel):
        await admin(interaction)
        row = service.panel(interaction.guild_id, panel)
        row['config']['category'] = category.id
        service.save_panel(interaction.guild_id, row['name'], row['config'], panel)
        await reply(interaction, 'Existing ticket category selected; no channel was created or moved.')

    @config.command(name='support-role', description='Add or remove an existing support role for a panel')
    async def support_role(interaction: discord.Interaction, panel: int, role: discord.Role, enabled: bool):
        await admin(interaction)
        if role.is_default() or role.managed:
            raise TicketError('Select an existing support role other than @everyone or a managed integration role.')
        row = service.panel(interaction.guild_id, panel)
        ids = set(row['config'].get('roles', []))
        ids.add(role.id) if enabled else ids.discard(role.id)
        row['config']['roles'] = sorted(ids)
        service.save_panel(interaction.guild_id, row['name'], row['config'], panel)
        await reply(interaction, 'Support role configuration saved. Existing ticket permissions are unchanged.')

    def private_destination(name, key):
        async def callback(interaction: discord.Interaction, channel: discord.TextChannel):
            await admin(interaction)
            if not service.private_staff_channel(interaction.guild, channel):
                raise TicketError('Select a private staff channel that @everyone cannot view.')
            put_setting(interaction.guild_id, key, str(channel.id))
            await reply(interaction, f'Private {name} destination saved.')
        config.add_command(app_commands.Command(name=name, description=f'Select an existing private {name} channel', callback=callback))
    private_destination('logs', 'ticket:logs')
    private_destination('transcripts', 'ticket:transcripts')

    def number_config(name, label, minimum, maximum, unit=''):
        async def callback(interaction: discord.Interaction, panel: int, value: int):
            await admin(interaction)
            if not minimum <= value <= maximum:
                raise TicketError(f'Choose a value from {minimum} to {maximum} {unit}.')
            row = service.panel(interaction.guild_id, panel)
            row['config'][name] = value
            service.save_panel(interaction.guild_id, row['name'], row['config'], panel)
            await reply(interaction, f'{label} saved: {value} {unit}.')
        config.add_command(app_commands.Command(name=name, description=label, callback=callback))
    number_config('limit', 'Set active tickets allowed per member', 1, 10)
    number_config('cooldown', 'Set the ticket opening cooldown in seconds', 0, 86400, 'seconds')
    number_config('autoclose', 'Set inactivity closure hours (zero disables)', 0, 8760, 'hours')
    number_config('reminder', 'Set inactivity reminder hours (zero disables)', 0, 8760, 'hours')

    @config.command(name='blacklist-add', description='Block a member from opening tickets in this server')
    async def blacklist_add(interaction: discord.Interaction, member: discord.Member, reason: str):
        await admin(interaction)
        if service.admin(member):
            raise TicketError('Server administrators cannot be ticket-blacklisted.')
        connection.execute('INSERT OR REPLACE INTO ticket_blacklist VALUES(?,?,?)', (interaction.guild_id, member.id, reason[:1000]))
        connection.commit()
        await reply(interaction, 'Member added to this server’s ticket blacklist.')

    @config.command(name='blacklist-remove', description='Allow a ticket-blacklisted user to open tickets again')
    async def blacklist_remove(interaction: discord.Interaction, user_id: str):
        await admin(interaction)
        if not user_id.isdecimal():
            raise TicketError('Provide a numeric Discord user ID.')
        cursor = connection.execute('DELETE FROM ticket_blacklist WHERE guild=? AND user=?', (interaction.guild_id, int(user_id)))
        connection.commit()
        await reply(interaction, 'Ticket blacklist entry removed.' if cursor.rowcount else 'That user was not ticket-blacklisted in this server.')

    @config.command(name='blacklist-list', description='Privately inspect this server’s ticket blacklist')
    async def blacklist_list(interaction: discord.Interaction):
        await admin(interaction)
        rows = service.query('SELECT user,reason FROM ticket_blacklist WHERE guild=? LIMIT 30', (interaction.guild_id,))
        await reply(interaction, '\n'.join(f'{r["user"]}: {r["reason"][:100]}' for r in rows) or 'Ticket blacklist is empty.')

    @config.command(name='hours', description='Set support working hours; equal start/end removes restrictions')
    async def hours(interaction: discord.Interaction, panel: int, start_hour: app_commands.Range[int, 0, 23], end_hour: app_commands.Range[int, 0, 23], timezone: str = 'UTC'):
        await admin(interaction)
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            raise TicketError('Use an IANA timezone such as Europe/London, America/New_York or UTC.')
        row = service.panel(interaction.guild_id, panel)
        row['config']['hours'] = None if start_hour == end_hour else {'start': start_hour, 'end': end_hour, 'timezone': timezone}
        service.save_panel(interaction.guild_id, row['name'], row['config'], panel)
        await reply(interaction, 'Support working hours saved for this ticket panel.')

    @config.command(name='naming', description='Set ticket channel names using {id} and {user}')
    async def naming(interaction: discord.Interaction, panel: int, template: str):
        await admin(interaction)
        if len(template) > 90 or '{id}' not in template:
            raise TicketError('Use a template up to 90 characters containing {id}; {user} is optional.')
        row = service.panel(interaction.guild_id, panel)
        row['config']['naming'] = template
        service.save_panel(interaction.guild_id, row['name'], row['config'], panel)
        await reply(interaction, 'Ticket naming template saved.')

    @config.command(name='questions', description='Set up to four opening-form questions separated with |; empty clears')
    async def questions(interaction: discord.Interaction, panel: int, questions: str = ''):
        await admin(interaction)
        parts = [q.strip() for q in questions.split('|') if q.strip()]
        if len(parts) > 4 or any(len(q) > 45 for q in parts):
            raise TicketError('Provide up to four questions, each at most 45 characters.')
        row = service.panel(interaction.guild_id, panel)
        row['config']['questions'] = parts
        service.save_panel(interaction.guild_id, row['name'], row['config'], panel)
        await reply(interaction, f'Opening form saved with {len(parts)} custom questions.')

    @config.command(name='responses', description='Save, list, delete or send a canned support response')
    @app_commands.choices(action=[app_commands.Choice(name=x.title(), value=x) for x in ('list', 'save', 'delete', 'send')])
    async def responses(interaction: discord.Interaction, action: str, name: str = '', text: str = ''):
        if action == 'send':
            service.authorized(interaction, staff=True)
        else:
            await admin(interaction)
        if action == 'list':
            rows = service.query('SELECT name FROM ticket_responses WHERE guild=? ORDER BY name LIMIT 50', (interaction.guild_id,))
            return await reply(interaction, ', '.join(r['name'] for r in rows) or 'No saved support responses.')
        if not re.fullmatch('[a-zA-Z0-9_-]{1,40}', name):
            raise TicketError('Choose a response name with 1–40 letters, numbers, underscores or hyphens.')
        if action == 'save':
            if not text:
                raise TicketError('Provide the response text to save.')
            connection.execute('INSERT OR REPLACE INTO ticket_responses VALUES(?,?,?)', (interaction.guild_id, name, text[:1800]))
            connection.commit()
            return await reply(interaction, 'Canned support response saved.')
        if action == 'delete':
            cursor = connection.execute('DELETE FROM ticket_responses WHERE guild=? AND name=?', (interaction.guild_id, name))
            connection.commit()
            return await reply(interaction, 'Saved response deleted.' if cursor.rowcount else 'No saved response has that name.')
        rows = service.query('SELECT content FROM ticket_responses WHERE guild=? AND name=?', (interaction.guild_id, name))
        if not rows:
            raise TicketError('No saved response has that name in this server.')
        await interaction.channel.send(rows[0]['content'], allowed_mentions=discord.AllowedMentions.none(), silent=True)
        await reply(interaction, 'Saved support response sent in this ticket.')

    @config.command(name='stats', description='Read this server’s ticket totals, response times and ratings')
    async def stats(interaction: discord.Interaction):
        await admin(interaction)
        rows = service.query('SELECT status,COUNT(*) AS count FROM ticket_records WHERE guild=? GROUP BY status', (interaction.guild_id,))
        metrics = service.query('SELECT AVG(first_response-created) AS response,AVG(rating) AS rating FROM ticket_records WHERE guild=?', (interaction.guild_id,))[0]
        await reply(interaction, '\n'.join(f'{r["status"]}: {r["count"]}' for r in rows) +
            f'\nAverage first response: {round(metrics["response"] or 0)} seconds\nAverage rating: {round(metrics["rating"], 2) if metrics["rating"] else "No ratings"}')

    @config.command(name='staff-stats', description='Read ticket assignments, resolved counts and ratings by support member')
    async def staff_stats(interaction: discord.Interaction, member: discord.Member = None):
        await admin(interaction)
        sql = 'SELECT assigned,COUNT(*) AS total,SUM(status IN (\'closed\',\'deleted\')) AS resolved,AVG(rating) AS rating FROM ticket_records WHERE guild=? AND assigned IS NOT NULL'
        args = [interaction.guild_id]
        if member:
            sql += ' AND assigned=?'
            args.append(member.id)
        rows = service.query(sql + ' GROUP BY assigned ORDER BY total DESC LIMIT 20', args)
        await reply(interaction, '\n'.join(f'<@{r["assigned"]}>: {r["total"]} assigned, {r["resolved"]} closed, rating {round(r["rating"], 2) if r["rating"] else "unrated"}' for r in rows) or 'No ticket assignments recorded.')

    @config.command(name='export', description='Privately export this server’s ticket configuration and metadata')
    async def export(interaction: discord.Interaction):
        await admin(interaction)
        data = {'guild': interaction.guild_id, 'panels': service.panels(interaction.guild_id),
                'tickets': service.query('SELECT * FROM ticket_records WHERE guild=? ORDER BY id LIMIT 5000', (interaction.guild_id,)),
                'blacklist': service.query('SELECT * FROM ticket_blacklist WHERE guild=?', (interaction.guild_id,))}
        payload = json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8')
        if len(payload) > min(interaction.guild.filesize_limit - 1000, 8_000_000):
            raise TicketError('Export exceeds the safe upload size. Export individual ticket transcripts instead.')
        await reply(interaction, 'Private ticket metadata export attached. Message transcripts are exported separately.',
                    file=discord.File(io.BytesIO(payload), filename=f'tickets-{interaction.guild_id}.json'))

    @config.command(name='reset', description='Disable a ticket panel and restore its text defaults without deleting tickets')
    async def reset(interaction: discord.Interaction, panel: int):
        await admin(interaction)
        row = service.panel(interaction.guild_id, panel)
        row['config'].update(enabled=False, title='Support', description='Open a private ticket to contact support.', button='Open ticket', colour=0xD4D4D4)
        service.save_panel(interaction.guild_id, row['name'], row['config'], panel)
        await reply(interaction, 'Panel disabled and default display text restored. Existing tickets and access configuration are preserved.')

    @config.command(name='menu', description='Add or remove a category option in a ticket-opening dropdown')
    async def menu(interaction: discord.Interaction, panel: int, label: str, category: discord.CategoryChannel, enabled: bool = True,
                   description: str = '', support_role: discord.Role = None):
        await admin(interaction)
        if len(label) > 80 or not label.strip():
            raise TicketError('Choose a category label from one to 80 characters.')
        if support_role and (support_role.is_default() or support_role.managed):
            raise TicketError('Select a regular support role other than @everyone.')
        row = service.panel(interaction.guild_id, panel)
        routes = [r for r in row['config'].get('routes', []) if r['label'] != label]
        if enabled:
            if len(routes) >= 25:
                raise TicketError('A ticket dropdown supports at most 25 category choices.')
            item = {'label': label, 'description': description[:100], 'category': category.id}
            if support_role:
                item['roles'] = [support_role.id]
            routes.append(item)
        row['config']['routes'] = routes
        service.save_panel(interaction.guild_id, row['name'], row['config'], panel)
        await reply(interaction, 'Ticket dropdown saved. Publish this panel from /setup-tickets to update its public menu.')

    return service
