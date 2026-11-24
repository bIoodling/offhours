"""Discord configuration for custom commands, schedules, sticky messages and rooms."""
import io
import json
import re
import time

import discord
from discord import app_commands

from services.automation import AutomationService, AutomationError, delay_seconds, delivery_time, reply


class CustomPermissions(discord.ui.View):
    def __init__(self, service, guild, user, name):
        super().__init__(timeout=600)
        self.service, self.guild, self.user, self.name = service, guild, user, name
        current = service.command(guild, name)
        self.roles, self.channels = json.loads(current['roles']), json.loads(current['channels'])
        roles = discord.ui.RoleSelect(placeholder='Allowed roles (clear selection for everyone)', min_values=0, max_values=25, row=0)
        async def role_selection(interaction):
            self.roles = [role.id for role in roles.values]
            await interaction.response.edit_message(content=self.summary(), view=self)
        roles.callback = role_selection
        self.add_item(roles)
        channels = discord.ui.ChannelSelect(placeholder='Allowed channels (clear selection for all)', min_values=0, max_values=25,
            channel_types=[discord.ChannelType.text, discord.ChannelType.news], row=1)
        async def channel_selection(interaction):
            self.channels = [channel.id for channel in channels.values]
            await interaction.response.edit_message(content=self.summary(), view=self)
        channels.callback = channel_selection
        self.add_item(channels)

    def summary(self):
        return f'Custom command `{self.name}` permissions\nAllowed roles: ' + (', '.join(f'<@&{r}>' for r in self.roles) or 'Everyone') + '\nAllowed channels: ' + (', '.join(f'<#{c}>' for c in self.channels) or 'All channels')

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild or interaction.user.id != self.user or not self.service.admin(interaction.user):
            await reply(interaction, 'Only the administrator who opened this editor can save these permissions.')
            return False
        return True

    @discord.ui.button(label='Save permissions', style=discord.ButtonStyle.primary, row=2)
    async def save(self, interaction, button):
        self.service.command(self.guild, self.name)
        self.service.db.execute('UPDATE automation_commands SET roles=?,channels=? WHERE guild=? AND name=?',
            (json.dumps(self.roles), json.dumps(self.channels), self.guild, self.name))
        self.service.db.commit()
        await interaction.response.edit_message(content='Custom command permissions saved for this server.', view=None)
        self.stop()

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.secondary, row=2)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(content='Unsaved permission changes discarded.', view=None)
        self.stop()


def install(bot, connection, get_setting, put_setting):
    service = AutomationService(bot, connection, get_setting, put_setting)
    bot.automation_service = service
    group = app_commands.Group(name='automation', description='Custom commands, automatic responses and scheduled messages',
                               default_permissions=discord.Permissions(manage_guild=True))
    rooms = app_commands.Group(name='tempvoice', description='Configure and manage temporary voice rooms')
    bot.tree.add_command(group)
    bot.tree.add_command(rooms)
    bot.add_listener(service.on_message, 'on_message')
    bot.add_listener(service.voice_state, 'on_voice_state_update')
    bot.add_listener(service.ready, 'on_ready')

    def admin(interaction):
        if not interaction.guild or not service.admin(interaction.user):
            raise AutomationError('Manage Server permission is required in this server.')

    async def error(interaction, exc):
        original = getattr(exc, 'original', exc)
        if isinstance(original, AutomationError):
            return await reply(interaction, str(original))
        if isinstance(original, discord.HTTPException):
            return await reply(interaction, 'Discord rejected that operation. Check the bot’s selected-channel permissions.')
        raise exc
    group.on_error = error
    rooms.on_error = error

    def validate_command(name, response):
        name = name.casefold().strip()
        if not re.fullmatch(r'[a-z0-9_-]{1,32}', name):
            raise AutomationError('Use a custom command name with 1–32 lowercase letters, numbers, underscores or hyphens.')
        if bot.get_command(name):
            raise AutomationError('That name belongs to an existing built-in command. Choose a different name.')
        if not response.strip() or len(response) > 1900:
            raise AutomationError('Provide response text from one to 1900 characters.')
        return name

    @group.command(name='list', description='List configured automation counts and enabled features')
    async def overview(interaction: discord.Interaction):
        admin(interaction)
        counts = {}
        for label, table in [('Custom commands', 'automation_commands'), ('Response triggers', 'automation_triggers'), ('Sticky channels', 'automation_sticky'), ('Scheduled messages', 'automation_schedules'), ('Temporary rooms', 'automation_voice_rooms')]:
            counts[label] = connection.execute(f'SELECT COUNT(*) FROM {table} WHERE guild=?', (interaction.guild_id,)).fetchone()[0]
        features = ['custom', 'autoresponder', 'sticky', 'schedules', 'tempvoice']
        await reply(interaction, '\n'.join(f'{label}: {count}' for label, count in counts.items()) + '\n\n' +
            '\n'.join(f'{feature}: {"enabled" if service.enabled(interaction.guild_id, feature) else "disabled"}' for feature in features))

    @group.command(name='enable', description='Enable one configured automation feature or the automation module')
    @app_commands.choices(feature=[app_commands.Choice(name=x.title(), value=x) for x in ('all', 'custom', 'autoresponder', 'sticky', 'schedules', 'tempvoice')])
    async def enable(interaction: discord.Interaction, feature: str = 'all'):
        admin(interaction)
        if feature not in ('all', 'custom', 'autoresponder', 'sticky', 'schedules', 'tempvoice'):
            raise AutomationError('Choose an available automation feature.')
        service.enable(interaction.guild_id, None if feature == 'all' else feature)
        await reply(interaction, 'Automation module enabled.' if feature == 'all' else f'{feature.title()} enabled for this server.')

    @group.command(name='disable', description='Disable automation without deleting saved configuration')
    @app_commands.choices(feature=[app_commands.Choice(name=x.title(), value=x) for x in ('all', 'custom', 'autoresponder', 'sticky', 'schedules', 'tempvoice')])
    async def disable(interaction: discord.Interaction, feature: str = 'all'):
        admin(interaction)
        if feature not in ('all', 'custom', 'autoresponder', 'sticky', 'schedules', 'tempvoice'):
            raise AutomationError('Choose an available automation feature.')
        put_setting(interaction.guild_id, 'module:automation' if feature == 'all' else 'automation:' + feature, '0')
        await reply(interaction, 'Automation disabled; saved configuration and existing temporary rooms are preserved.' if feature == 'all' else f'{feature.title()} disabled; saved configuration is preserved.')

    @group.command(name='custom-create', description='Create a real server-specific prefix command with response templates')
    async def custom_create(interaction: discord.Interaction, name: str, response: str, embed: bool = False,
                            cooldown: app_commands.Range[int, 1, 3600] = 10):
        admin(interaction)
        name = validate_command(name, response)
        if connection.execute('SELECT 1 FROM automation_commands WHERE guild=? AND name=?', (interaction.guild_id, name)).fetchone():
            raise AutomationError('That custom command already exists. Use custom-edit to change it.')
        if connection.execute('SELECT COUNT(*) FROM automation_commands WHERE guild=?', (interaction.guild_id,)).fetchone()[0] >= 100:
            raise AutomationError('This server has reached the 100 custom-command safety limit.')
        connection.execute('INSERT INTO automation_commands(guild,name,response,embed,cooldown) VALUES(?,?,?,?,?)',
                           (interaction.guild_id, name, response, int(embed), cooldown))
        connection.commit()
        service.enable(interaction.guild_id, 'custom')
        await reply(interaction, f'Created `{get_setting(interaction.guild_id, "prefix", "?")}{name}` for this server. Configure role/channel restrictions with custom-permissions.')

    @group.command(name='custom-edit', description='Change an existing custom command’s actual response and cooldown')
    async def custom_edit(interaction: discord.Interaction, name: str, response: str, embed: bool = False,
                          cooldown: app_commands.Range[int, 1, 3600] = 10):
        admin(interaction)
        name = validate_command(name, response)
        service.command(interaction.guild_id, name)
        connection.execute('UPDATE automation_commands SET response=?,embed=?,cooldown=? WHERE guild=? AND name=?',
                           (response, int(embed), cooldown, interaction.guild_id, name))
        connection.commit()
        await reply(interaction, 'Custom command response updated. Its saved permissions are preserved.')

    @group.command(name='custom-delete', description='Delete a custom command from this server')
    async def custom_delete(interaction: discord.Interaction, name: str):
        admin(interaction)
        record = service.command(interaction.guild_id, name)
        connection.execute('DELETE FROM automation_commands WHERE guild=? AND name=?', (interaction.guild_id, record['name']))
        connection.commit()
        await reply(interaction, 'Custom command deleted from this server.')

    @group.command(name='custom-list', description='List this server’s actual saved custom commands')
    async def custom_list(interaction: discord.Interaction):
        admin(interaction)
        rows = service.query('SELECT name,cooldown,enabled FROM automation_commands WHERE guild=? ORDER BY name LIMIT 100', (interaction.guild_id,))
        prefix = get_setting(interaction.guild_id, 'prefix', '?')
        await reply(interaction, '\n'.join(f'`{prefix}{r["name"]}` · {r["cooldown"]}s cooldown · {"enabled" if r["enabled"] else "disabled"}' for r in rows) or 'No custom commands configured.')

    @group.command(name='custom-preview', description='Privately render a saved custom response with the current member’s variables')
    async def custom_preview(interaction: discord.Interaction, name: str, arguments: str = ''):
        admin(interaction)
        record = service.command(interaction.guild_id, name)
        text = service.render(record['response'], member=interaction.user, guild=interaction.guild, channel=interaction.channel, args=arguments)
        if record['embed']:
            await interaction.response.send_message(content='Private response preview', embed=discord.Embed(description=text, colour=0xD4D4D4), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        else:
            await reply(interaction, text)

    @group.command(name='custom-permissions', description='Select allowed roles and channels through private dropdown menus')
    async def custom_permissions(interaction: discord.Interaction, name: str):
        admin(interaction)
        record = service.command(interaction.guild_id, name)
        view = CustomPermissions(service, interaction.guild_id, interaction.user.id, record['name'])
        await reply(interaction, view.summary(), view=view)

    @group.command(name='trigger-add', description='Add a real text autoresponse rule')
    @app_commands.choices(match=[app_commands.Choice(name=x.title(), value=x) for x in ('exact', 'contains', 'starts')])
    async def trigger_add(interaction: discord.Interaction, trigger: str, response: str, match: str = 'exact',
                          channel: discord.TextChannel = None, role: discord.Role = None, cooldown: app_commands.Range[int, 10, 3600] = 30):
        admin(interaction)
        if not 2 <= len(trigger.strip()) <= 200 or not 1 <= len(response.strip()) <= 1900 or match not in ('exact', 'contains', 'starts'):
            raise AutomationError('Use a trigger from two to 200 characters and response up to 1900 characters.')
        if connection.execute('SELECT COUNT(*) FROM automation_triggers WHERE guild=?', (interaction.guild_id,)).fetchone()[0] >= 100:
            raise AutomationError('This server has reached the 100 autoresponse-rule safety limit.')
        cursor = connection.execute('INSERT INTO automation_triggers(guild,trigger,response,match,channel,role,cooldown) VALUES(?,?,?,?,?,?,?)',
            (interaction.guild_id, trigger.strip(), response, match, channel.id if channel else 0, role.id if role else 0, cooldown))
        connection.commit()
        service.enable(interaction.guild_id, 'autoresponder')
        await reply(interaction, f'Autoresponse rule #{cursor.lastrowid} created and enabled for this server.')

    @group.command(name='trigger-edit', description='Change a saved autoresponse trigger and response')
    @app_commands.choices(match=[app_commands.Choice(name=x.title(), value=x) for x in ('exact', 'contains', 'starts')])
    async def trigger_edit(interaction: discord.Interaction, rule: int, trigger: str, response: str, match: str = 'exact',
                           channel: discord.TextChannel = None, role: discord.Role = None, cooldown: app_commands.Range[int, 10, 3600] = 30):
        admin(interaction)
        if not 2 <= len(trigger.strip()) <= 200 or not 1 <= len(response.strip()) <= 1900 or match not in ('exact', 'contains', 'starts'):
            raise AutomationError('Use a trigger from two to 200 characters and response up to 1900 characters.')
        cursor = connection.execute('UPDATE automation_triggers SET trigger=?,response=?,match=?,channel=?,role=?,cooldown=? WHERE guild=? AND id=?',
            (trigger.strip(), response, match, channel.id if channel else 0, role.id if role else 0, cooldown, interaction.guild_id, rule))
        connection.commit()
        await reply(interaction, 'Autoresponse rule updated.' if cursor.rowcount else 'No autoresponse rule with that ID exists in this server.')

    @group.command(name='trigger-remove', description='Delete one autoresponse rule from this server')
    async def trigger_remove(interaction: discord.Interaction, rule: int):
        admin(interaction)
        cursor = connection.execute('DELETE FROM automation_triggers WHERE guild=? AND id=?', (interaction.guild_id, rule))
        connection.commit()
        await reply(interaction, 'Autoresponse rule deleted.' if cursor.rowcount else 'No autoresponse rule with that ID exists in this server.')

    @group.command(name='trigger-list', description='List saved text autoresponse rules and their restrictions')
    async def trigger_list(interaction: discord.Interaction):
        admin(interaction)
        rows = service.query('SELECT * FROM automation_triggers WHERE guild=? ORDER BY id LIMIT 50', (interaction.guild_id,))
        await reply(interaction, '\n'.join(f'#{r["id"]} · {r["match"]}: {r["trigger"][:65]} · channel {r["channel"] or "all"}' for r in rows) or 'No autoresponse rules configured.')

    @group.command(name='autoresponder', description='Enable or disable automatic text responses without deleting rules')
    async def autoresponder(interaction: discord.Interaction, enabled: bool):
        admin(interaction)
        if enabled:
            service.enable(interaction.guild_id, 'autoresponder')
        else:
            put_setting(interaction.guild_id, 'automation:autoresponder', '0')
        await reply(interaction, 'Autoresponder ' + ('enabled.' if enabled else 'disabled.'))

    @group.command(name='sticky', description='Keep a configured bot message near the bottom of an existing channel')
    async def sticky(interaction: discord.Interaction, channel: discord.TextChannel, message: str,
                     cooldown: app_commands.Range[int, 30, 3600] = 60):
        admin(interaction)
        if not message.strip() or len(message) > 1900:
            raise AutomationError('Provide sticky text from one to 1900 characters.')
        await interaction.response.defer(ephemeral=True)
        async with service.locks[('sticky', interaction.guild_id, channel.id)]:
            old = service.query('SELECT * FROM automation_sticky WHERE guild=? AND channel=?', (interaction.guild_id, channel.id))
            connection.execute('INSERT INTO automation_sticky(guild,channel,content,message,cooldown,last) VALUES(?,?,?,?,?,?) ON CONFLICT(guild,channel) DO UPDATE SET content=excluded.content,cooldown=excluded.cooldown',
                (interaction.guild_id, channel.id, message, None, cooldown, 0))
            connection.commit()
            record = service.query('SELECT * FROM automation_sticky WHERE guild=? AND channel=?', (interaction.guild_id, channel.id))[0]
            posted = await service.post_sticky(channel, record)
        service.enable(interaction.guild_id, 'sticky')
        await reply(interaction, f'Sticky message saved and posted: {posted.jump_url}')

    @group.command(name='unsticky', description='Stop maintaining a sticky message without deleting human messages')
    async def unsticky(interaction: discord.Interaction, channel: discord.TextChannel, remove_bot_message: bool = False):
        admin(interaction)
        async with service.locks[('sticky', interaction.guild_id, channel.id)]:
            rows = service.query('SELECT * FROM automation_sticky WHERE guild=? AND channel=?', (interaction.guild_id, channel.id))
            if not rows:
                raise AutomationError('No sticky message is configured in this channel.')
            connection.execute('DELETE FROM automation_sticky WHERE guild=? AND channel=?', (interaction.guild_id, channel.id))
            connection.commit()
            if remove_bot_message and rows[0]['message']:
                try:
                    old = await channel.fetch_message(rows[0]['message'])
                    if old.author.id == bot.user.id:
                        await old.delete()
                except discord.NotFound:
                    pass
        await reply(interaction, 'Sticky maintenance stopped for this channel.')

    @group.command(name='scheduled-message', description='Schedule a real announcement for later, optionally repeating')
    async def scheduled_message(interaction: discord.Interaction, channel: discord.TextChannel, when: str, message: str, repeat: str = ''):
        admin(interaction)
        if not message.strip() or len(message) > 1900:
            raise AutomationError('Provide announcement text from one to 1900 characters.')
        if not channel.permissions_for(interaction.user).send_messages:
            raise AutomationError('You cannot post messages in the selected channel.')
        due = delivery_time(when)
        interval = delay_seconds(repeat, minimum=300) if repeat else 0
        if connection.execute("SELECT COUNT(*) FROM automation_schedules WHERE guild=? AND status IN ('active','paused','sending')", (interaction.guild_id,)).fetchone()[0] >= 100:
            raise AutomationError('This server has reached the 100 active/paused schedule safety limit.')
        cursor = connection.execute('INSERT INTO automation_schedules(guild,channel,creator,content,due,repeat,created) VALUES(?,?,?,?,?,?,?)',
            (interaction.guild_id, channel.id, interaction.user.id, message, due, interval, time.time()))
        connection.commit()
        service.enable(interaction.guild_id, 'schedules')
        await reply(interaction, f'Scheduled announcement #{cursor.lastrowid} for <t:{int(due)}:F> in {channel.mention}.' +
                    (f' Repeats every {interval} seconds.' if interval else ''))

    @group.command(name='schedules', description='List persisted announcement deadlines, delivery counts and paused errors')
    async def schedules(interaction: discord.Interaction):
        admin(interaction)
        rows = service.query('SELECT * FROM automation_schedules WHERE guild=? ORDER BY due DESC LIMIT 20', (interaction.guild_id,))
        await reply(interaction, '\n'.join(f'#{r["id"]} · {r["status"]} · <#{r["channel"]}> · <t:{int(r["due"])}:F> · sent {r["deliveries"]}' +
                    (' · ' + r['last_error'][:100] if r['last_error'] else '') for r in rows) or 'No scheduled announcements.')

    @group.command(name='schedule-cancel', description='Cancel a pending or recurring announcement')
    async def schedule_cancel(interaction: discord.Interaction, schedule: int):
        admin(interaction)
        async with service.locks[('schedule', interaction.guild_id)]:
            record = service.schedule(interaction.guild_id, schedule)
            if record['status'] in ('completed', 'cancelled'):
                raise AutomationError('This schedule is already completed or cancelled.')
            connection.execute("UPDATE automation_schedules SET status='cancelled' WHERE guild=? AND id=?", (interaction.guild_id, schedule))
            connection.commit()
        await reply(interaction, 'Scheduled announcement cancelled. Previously delivered messages remain unchanged.')

    @group.command(name='schedule-resume', description='Resume a paused schedule after reviewing any uncertain previous delivery')
    async def schedule_resume(interaction: discord.Interaction, schedule: int, when: str):
        admin(interaction)
        due = delivery_time(when)
        async with service.locks[('schedule', interaction.guild_id)]:
            record = service.schedule(interaction.guild_id, schedule)
            if record['status'] != 'paused':
                raise AutomationError('Only a paused schedule can be resumed.')
            connection.execute("UPDATE automation_schedules SET status='active',due=?,last_error='' WHERE guild=? AND id=?", (due, interaction.guild_id, schedule))
            connection.commit()
        service.enable(interaction.guild_id, 'schedules')
        await reply(interaction, f'Schedule resumed with its next delivery at <t:{int(due)}:F>.')

    @group.command(name='export', description='Privately export this server’s message automation configuration')
    async def export(interaction: discord.Interaction):
        admin(interaction)
        data = {name: service.query('SELECT * FROM ' + table + ' WHERE guild=?', (interaction.guild_id,)) for name, table in
                [('commands', 'automation_commands'), ('triggers', 'automation_triggers'), ('sticky', 'automation_sticky'), ('schedules', 'automation_schedules')]}
        payload = json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8')
        if len(payload) > min(interaction.guild.filesize_limit - 1000, 8_000_000):
            raise AutomationError('Automation export exceeds the safe upload limit.')
        await reply(interaction, 'Private automation export attached.', file=discord.File(io.BytesIO(payload), filename='automation.json'))

    @rooms.command(name='enable', description='Explicitly enable temporary rooms from an existing voice lobby and category')
    @app_commands.default_permissions(manage_guild=True)
    async def voice_enable(interaction: discord.Interaction, lobby: discord.VoiceChannel, category: discord.CategoryChannel,
                           user_limit: app_commands.Range[int, 0, 99] = 0, maximum_rooms: app_commands.Range[int, 1, 50] = 10):
        admin(interaction)
        permissions = lobby.permissions_for(interaction.guild.me)
        if not interaction.guild.me.guild_permissions.manage_channels or not permissions.move_members or not permissions.connect:
            raise AutomationError('The bot needs Manage Channels, Move Members and Connect for this lobby.')
        put_setting(interaction.guild_id, 'tempvoice:lobby', str(lobby.id))
        put_setting(interaction.guild_id, 'tempvoice:category', str(category.id))
        put_setting(interaction.guild_id, 'tempvoice:limit', str(user_limit))
        put_setting(interaction.guild_id, 'tempvoice:max_rooms', str(maximum_rooms))
        service.enable(interaction.guild_id, 'tempvoice')
        await reply(interaction, f'Temporary rooms enabled: joining {lobby.mention} deliberately creates a private room with the lobby’s access rules in {category.mention}. Only empty rooms created and recorded by this bot are automatically deleted.')

    @rooms.command(name='disable', description='Stop creating temporary rooms while retaining safe cleanup of existing rooms')
    @app_commands.default_permissions(manage_guild=True)
    async def voice_disable(interaction: discord.Interaction):
        admin(interaction)
        put_setting(interaction.guild_id, 'automation:tempvoice', '0')
        await reply(interaction, 'Temporary room creation disabled. Existing bot-owned rooms are cleaned up when empty.')

    def owned_room(interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not interaction.user.voice or not interaction.user.voice.channel:
            raise AutomationError('Join your temporary voice room first.')
        channel = interaction.user.voice.channel
        record = service.voice_room(interaction.guild_id, channel.id)
        if record['owner'] != interaction.user.id and not service.admin(interaction.user):
            raise AutomationError('Only this temporary room’s owner or a server administrator can change it.')
        return record, channel

    @rooms.command(name='name', description='Rename your bot-created temporary room')
    async def room_name(interaction: discord.Interaction, name: str):
        record, channel = owned_room(interaction)
        if not 1 <= len(name.strip()) <= 100:
            raise AutomationError('Use a room name from one to 100 characters.')
        await channel.edit(name=name.strip(), reason=f'Temporary room owner {interaction.user.id} requested rename')
        await reply(interaction, 'Your temporary room was renamed.')

    @rooms.command(name='limit', description='Set the member limit in your temporary voice room')
    async def room_limit(interaction: discord.Interaction, members: app_commands.Range[int, 0, 99]):
        record, channel = owned_room(interaction)
        await channel.edit(user_limit=members, reason=f'Temporary room owner {interaction.user.id} requested member limit')
        await reply(interaction, 'Temporary room member limit updated.' if members else 'Temporary room member limit removed.')

    @rooms.command(name='lock', description='Restrict joining your temporary room to its owner and trusted members')
    async def room_lock(interaction: discord.Interaction):
        record, channel = owned_room(interaction)
        permissions = dict(channel.overwrites)
        trusted = {record['owner'], interaction.guild.me.id}
        trusted.update(r['user'] for r in service.query('SELECT user FROM automation_voice_trusted WHERE guild=? AND channel=?', (interaction.guild_id, channel.id)))
        for target, overwrite in permissions.items():
            if isinstance(target, discord.Role) or target.id not in trusted:
                overwrite.connect = False
        everyone = permissions.get(interaction.guild.default_role, discord.PermissionOverwrite())
        everyone.connect = False
        permissions[interaction.guild.default_role] = everyone
        for user_id in trusted:
            member = interaction.guild.get_member(user_id)
            if member:
                overwrite = permissions.get(member, discord.PermissionOverwrite())
                overwrite.view_channel, overwrite.connect, overwrite.speak = True, True, True
                permissions[member] = overwrite
        await channel.edit(overwrites=permissions, reason=f'Temporary room owner {interaction.user.id} requested lock')
        connection.execute('UPDATE automation_voice_rooms SET locked=1 WHERE guild=? AND channel=?', (interaction.guild_id, channel.id))
        connection.commit()
        await reply(interaction, 'Temporary room locked. Server administrators retain Discord’s administrative access.')

    @rooms.command(name='unlock', description='Restore the lobby-derived joining permissions of your temporary room')
    async def room_unlock(interaction: discord.Interaction):
        record, channel = owned_room(interaction)
        permissions = service.decode_permissions(interaction.guild, record['permissions'])
        owner = interaction.guild.get_member(record['owner'])
        if owner:
            permissions[owner] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True)
        for row in service.query('SELECT user FROM automation_voice_trusted WHERE guild=? AND channel=?', (interaction.guild_id, channel.id)):
            member = interaction.guild.get_member(row['user'])
            if member:
                permissions[member] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True)
        await channel.edit(overwrites=permissions, reason=f'Temporary room owner {interaction.user.id} requested unlock')
        connection.execute('UPDATE automation_voice_rooms SET locked=0 WHERE guild=? AND channel=?', (interaction.guild_id, channel.id))
        connection.commit()
        await reply(interaction, 'Temporary room joining permissions restored.')

    @rooms.command(name='trust', description='Allow an existing server member to join your locked temporary room')
    async def room_trust(interaction: discord.Interaction, member: discord.Member):
        record, channel = owned_room(interaction)
        await channel.set_permissions(member, view_channel=True, connect=True, speak=True,
                                      reason=f'Temporary room owner {interaction.user.id} trusted this member')
        connection.execute('INSERT OR IGNORE INTO automation_voice_trusted VALUES(?,?,?)', (interaction.guild_id, channel.id, member.id))
        connection.commit()
        await reply(interaction, 'Member allowed to join your temporary room.')

    @rooms.command(name='untrust', description='Remove a member’s trusted joining access to your temporary room')
    async def room_untrust(interaction: discord.Interaction, member: discord.Member):
        record, channel = owned_room(interaction)
        if member.id in (record['owner'], interaction.guild.me.id):
            raise AutomationError('The room owner and bot cannot be removed from trusted access.')
        originals = service.decode_permissions(interaction.guild, record['permissions'])
        if record['locked']:
            await channel.set_permissions(member, connect=False, reason='Temporary room trusted access removed')
        else:
            await channel.set_permissions(member, overwrite=originals.get(member), reason='Temporary room trusted access removed')
        connection.execute('DELETE FROM automation_voice_trusted WHERE guild=? AND channel=? AND user=?', (interaction.guild_id, channel.id, member.id))
        connection.commit()
        await reply(interaction, 'Member’s trusted access removed. Server-level permissions still apply.')

    @rooms.command(name='claim', description='Claim ownership when a temporary room’s previous owner has left it')
    async def room_claim(interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not interaction.user.voice or not interaction.user.voice.channel:
            raise AutomationError('Join the temporary room you want to claim first.')
        channel = interaction.user.voice.channel
        record = service.voice_room(interaction.guild_id, channel.id)
        if any(member.id == record['owner'] for member in channel.members) and not service.admin(interaction.user):
            raise AutomationError('The current room owner is still in the room.')
        await channel.set_permissions(interaction.user, view_channel=True, connect=True, speak=True, reason='Abandoned temporary room claimed')
        connection.execute('UPDATE automation_voice_rooms SET owner=? WHERE guild=? AND channel=?', (interaction.user.id, interaction.guild_id, channel.id))
        connection.commit()
        await reply(interaction, 'You now own this temporary room.')

    @rooms.command(name='info', description='Show the owner and state of your current temporary room')
    async def room_info(interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not interaction.user.voice or not interaction.user.voice.channel:
            raise AutomationError('Join a temporary room first.')
        channel = interaction.user.voice.channel
        record = service.voice_room(interaction.guild_id, channel.id)
        await reply(interaction, f'Temporary room: {channel.mention}\nOwner: <@{record["owner"]}>\nLocked: {bool(record["locked"])}\nMembers: {len(channel.members)}\nLimit: {channel.user_limit or "None"}')

    @rooms.command(name='list', description='List this server’s rooms actually created and recorded by the bot')
    @app_commands.default_permissions(manage_guild=True)
    async def room_list(interaction: discord.Interaction):
        admin(interaction)
        rows = service.query('SELECT * FROM automation_voice_rooms WHERE guild=? ORDER BY created DESC LIMIT 50', (interaction.guild_id,))
        await reply(interaction, '\n'.join(f'<#{r["channel"]}> · owner <@{r["owner"]}> · {"locked" if r["locked"] else "open"}' for r in rows) or 'No bot-created temporary rooms exist in this server.')

    @rooms.command(name='naming', description='Set future temporary room names using {user}')
    @app_commands.default_permissions(manage_guild=True)
    async def room_naming(interaction: discord.Interaction, template: str):
        admin(interaction)
        if not 1 <= len(template.strip()) <= 80:
            raise AutomationError('Use a room naming template from one to 80 characters.')
        put_setting(interaction.guild_id, 'tempvoice:naming', template.strip())
        await reply(interaction, 'Naming template saved for future temporary rooms.')

    return service
