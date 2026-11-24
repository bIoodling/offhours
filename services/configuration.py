"""Guild-scoped configuration; selecting Discord objects never changes their layout."""
import asyncio
import datetime
import io
import json
import os
import re
import time

import discord
from discord import app_commands

MODULES = ('music', 'moderation', 'security', 'levels', 'welcome', 'leave', 'verification',
           'selfroles', 'invites', 'tickets', 'ai', 'social', 'suggestions', 'giveaways',
           'starboard', 'automation', 'logs', 'analytics', 'boost', 'polls')
DEFAULT_ON = {'music', 'moderation'}
CHANNEL_KEYS = ('welcome', 'rules', 'verify', 'announcements', 'general', 'media', 'clips',
                'post-your-setup', 'logs', 'reports', 'live', 'video', 'levels', 'suggestions', 'starboard')
ROLE_KEYS = ('regular', 'pending', 'live', 'video', 'announcements', 'giveaways', 'quarantine', 'autorole')


def administrator(member):
    return isinstance(member, discord.Member) and (member.id == member.guild.owner_id or member.guild_permissions.administrator)


def enabled(get, guild, module):
    return get(guild, 'module:' + module, '1' if module in DEFAULT_ON else '0') == '1'


def channel_for(guild, get, slot):
    value = get(guild.id, 'channel:' + slot, '0')
    return guild.get_channel(int(value)) if value.isdecimal() else None


def role_for(guild, get, slot):
    value = get(guild.id, 'role:' + slot, '0')
    return guild.get_role(int(value)) if value.isdecimal() else None


def sensitive_setting(key):
    # Token *budgets* are configuration, whereas credential token keys are not.
    return bool(re.search(r'(?:^|[:_.-])(?:token|secret|api_key|password|credentials?)(?:$|[:_.-])', key.lower()))


async def private(i, text=None, **kwargs):
    kwargs.update(ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    if i.response.is_done():
        return await i.followup.send(text, **kwargs)
    return await i.response.send_message(text, **kwargs)


def command_module(path):
    root = path.split()[0] if path.split() else ''
    if root in {'mod','ban','unban','kick','mute','unmute','timeout','untimeout','warn','warnings',
                'purge','softban','tempban','lock','unlock','lockdown','slowmode','nick','deafen','undeafen','clean','report'}:
        return 'moderation'
    if root in {'role','roles','colour','region','notifications'}:
        return 'selfroles'
    if root in {'suggest','suggestion'}:
        return 'suggestions'
    if root == 'starboard':
        return 'starboard'
    if root == 'poll':
        return 'polls'
    if root == 'analytics':
        return 'analytics'
    if root in {'play', 'music'}:
        return 'music'
    if root in {'ticket', 'ticket-config'}:
        return 'tickets'
    if root in {'chatgpt', 'ai'}:
        return 'ai'
    if root in {'level', 'levels', 'rank', 'leaderboard'}:
        return 'levels'
    if root in {'invites', 'social', 'giveaway', 'security', 'automation', 'logs', 'stats'}:
        return {'giveaway': 'giveaways', 'stats': 'analytics'}.get(root, root)
    return None


def allowed_command(i, get, path):
    if not i.guild or not isinstance(i.user, discord.Member):
        return False, 'Use this command in a server.'
    if administrator(i.user):
        return True, ''
    # Configuration callbacks still enforce their own required permissions.
    module = command_module(path)
    if module and not enabled(get, i.guild.id, module):
        return False, f'{module.title()} is disabled in this server. An administrator can enable it in /setup.'
    policy = get(i.guild.id, 'command:' + path, '')
    if policy:
        try:
            data = json.loads(policy)
        except (ValueError, TypeError):
            return False, 'This command has an invalid server policy. Ask an administrator to reconfigure it.'
        if not isinstance(data,dict) or any(not isinstance(data.get(k,[]),list) or any(type(x) is not int for x in data.get(k,[])) for k in ('allowed_roles','blocked_roles','allowed_channels','blocked_channels')):
            return False, 'This command has an invalid server policy. Ask an administrator to reconfigure it.'
        roles = {r.id for r in i.user.roles}
        if data.get('disabled'):
            return False, 'This command is disabled in this server.'
        if roles & set(data.get('blocked_roles', [])) or i.channel_id in data.get('blocked_channels', []):
            return False, 'Server command policy denies this action.'
        if data.get('allowed_roles') and not roles & set(data['allowed_roles']):
            return False, 'Your roles are not allowed to use this command.'
        if data.get('allowed_channels') and i.channel_id not in data['allowed_channels']:
            return False, 'Use an allowed channel for this command.'
    return True, ''


class ServerCommandTree(app_commands.CommandTree):
    async def interaction_check(self, i):
        path = i.command.qualified_name if i.command else ''
        get = getattr(self.client, 'get_setting', lambda *a: a[-1] if len(a) > 2 else '')
        ok, reason = allowed_command(i, get, path)
        if not ok:
            await private(i, reason)
        return ok

    async def on_error(self, i, error):
        if isinstance(error, app_commands.CommandOnCooldown):
            return await private(i, f'Try again in {error.retry_after:.0f} seconds.')
        if isinstance(error, app_commands.CheckFailure):
            return await private(i, 'Your server permissions do not allow this action.')
        import logging
        logging.getLogger('offhours').warning('Slash action failed command=%s type=%s',
            i.command.qualified_name if i.command else 'unknown', type(error).__name__)
        await private(i, 'The action failed. Check permissions, role hierarchy and the private host console.')


class ConfigView(discord.ui.View):
    def __init__(self, service, guild, author, module='music'):
        super().__init__(timeout=900)
        self.service, self.guild, self.author, self.module = service, guild, author, module
        select = discord.ui.Select(placeholder='Choose a feature', row=0,
            options=[discord.SelectOption(label=x.title(), value=x, default=x == module) for x in MODULES])
        async def selected(i):
            self.module = select.values[0]
            await i.response.edit_message(embed=self.preview(), view=ConfigView(service, guild, author, self.module))
        select.callback = selected
        self.add_item(select)
        slots = discord.ui.Select(placeholder='Choose a channel setting', row=1,
            options=[discord.SelectOption(label=x, value=x) for x in CHANNEL_KEYS])
        async def choose_slot(i):
            await i.response.send_message('Select an existing channel for ' + slots.values[0],
                view=ObjectPicker(service, guild, author, 'channel:' + slots.values[0], False), ephemeral=True)
        slots.callback = choose_slot
        self.add_item(slots)
        roles = discord.ui.Select(placeholder='Choose a role setting', row=2,
            options=[discord.SelectOption(label=x, value=x) for x in ROLE_KEYS])
        async def choose_role(i):
            await i.response.send_message('Select an existing role for ' + roles.values[0],
                view=ObjectPicker(service, guild, author, 'role:' + roles.values[0], True), ephemeral=True)
        roles.callback = choose_role
        self.add_item(roles)

    async def interaction_check(self, i):
        return await self.service.check(i, self.guild.id, self.author)

    def preview(self):
        status = enabled(self.service.get, self.guild.id, self.module)
        embed = discord.Embed(title=self.guild.name + ' / configuration', colour=0xD4D4D4,
            description=f'**{self.module.title()}**: {"enabled" if status else "disabled"}\n'
                'Choose existing channels and roles. Changes are saved to this server only.\n'
                'No channels, categories or roles are created, moved, renamed or deleted.')
        channel_slots = [f'{k}: <#{self.service.get(self.guild.id, "channel:" + k)}>'
                         for k in CHANNEL_KEYS if self.service.get(self.guild.id, 'channel:' + k)]
        embed.add_field(name='Saved destinations', value='\n'.join(channel_slots)[:1024] or 'No destinations configured.', inline=False)
        return embed

    @discord.ui.button(label='Enable / Disable feature', style=discord.ButtonStyle.primary, row=3)
    async def toggle(self, i, button):
        value = not enabled(self.service.get, self.guild.id, self.module)
        self.service.put(self.guild.id, 'module:' + self.module, '1' if value else '0')
        await i.response.edit_message(embed=self.preview(), view=self)

    @discord.ui.button(label='Feature options', row=3)
    async def options(self, i, button):
        if self.module == 'music':
            view = MusicOptions(self.service, self.guild, self.author)
            return await i.response.edit_message(embed=view.preview(), view=view)
        await i.response.send_modal(OptionsModal(self.service, self.guild, self.author, self.module))

    @discord.ui.button(label='Export settings', row=3)
    async def export(self, i, button):
        await private(i, file=discord.File(io.BytesIO(self.service.export(self.guild.id)), filename='offhours-settings.json'))


class ObjectPicker(discord.ui.View):
    def __init__(self, service, guild, author, key, role):
        super().__init__(timeout=300)
        self.service, self.guild, self.author = service, guild, author
        picker = discord.ui.RoleSelect(placeholder='Select existing role', min_values=0, max_values=1) if role else discord.ui.ChannelSelect(
            placeholder='Select existing channel', min_values=0, max_values=1,
            channel_types=[discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.voice])
        async def save(i):
            value = picker.values[0] if picker.values else None
            obj = guild.get_role(value.id) if role and value else guild.get_channel(value.id) if value else None
            if value and not obj:
                return await private(i, 'Select an object from this server.')
            if role and obj and (obj.is_default() or obj.managed or obj >= guild.me.top_role or obj.permissions.administrator or obj.permissions.manage_roles):
                return await private(i, 'Choose a non-managed role below the bot without Administrator or Manage Roles permissions.')
            service.put(guild.id, key, str(obj.id) if obj else '0')
            await i.response.edit_message(content='Saved ' + key + ' for this server.', view=None)
        picker.callback = save
        self.add_item(picker)

    async def interaction_check(self, i):
        return await self.service.check(i, self.guild.id, self.author)


class MusicOptions(discord.ui.View):
    """Native selectors keep voice and DJ configuration out of numeric-ID forms."""
    def __init__(self, service, guild, author):
        super().__init__(timeout=900)
        self.service, self.guild, self.author = service, guild, author
        saved_channels = {int(x) for x in service.get(guild.id,'music:channels','').split(',') if x.isdecimal()}
        saved_roles = {int(x) for x in service.get(guild.id,'music:dj_roles','').split(',') if x.isdecimal()}
        channels = discord.ui.ChannelSelect(placeholder='Allowed voice channels (clear for all)', min_values=0, max_values=25,
            channel_types=[discord.ChannelType.voice], row=0,
            default_values=[c for c in guild.voice_channels if c.id in saved_channels][:25])
        async def choose_channels(i):
            selected = [guild.get_channel(c.id) for c in channels.values]
            if any(not isinstance(c,discord.VoiceChannel) for c in selected):
                return await private(i,'Choose existing voice channels from this server.')
            service.put(guild.id,'music:channels',','.join(str(c.id) for c in selected))
            await i.response.edit_message(embed=self.preview(),view=self)
        channels.callback=choose_channels
        self.add_item(channels)
        roles = discord.ui.RoleSelect(placeholder='DJ override roles (clear for none)', min_values=0, max_values=25, row=1,
            default_values=[r for r in guild.roles if r.id in saved_roles][:25])
        async def choose_roles(i):
            selected=[guild.get_role(r.id) for r in roles.values]
            if any(not r or r.is_default() for r in selected):
                return await private(i,'Choose existing roles other than @everyone. Set the minimum level to zero to allow everyone.')
            service.put(guild.id,'music:dj_roles',','.join(str(r.id) for r in selected))
            await i.response.edit_message(embed=self.preview(),view=self)
        roles.callback=choose_roles
        self.add_item(roles)

    async def interaction_check(self,i):
        return await self.service.check(i,self.guild.id,self.author)

    def preview(self):
        raw=self.service.get(self.guild.id,'music:channels','')
        channels='All existing voice channels' if not raw else 'No voice channels allowed' if raw=='0' else ', '.join(f'<#{x}>' for x in raw.split(',') if x.isdecimal())
        roles=', '.join(f'<@&{x}>' for x in self.service.get(self.guild.id,'music:dj_roles','').split(',') if x.isdecimal()) or 'None'
        embed=discord.Embed(title=self.guild.name+' / music configuration',colour=0xD4D4D4,
            description='Select existing voice channels and DJ override roles. Changes save immediately for this server.')
        embed.add_field(name='Allowed voice channels',value=channels[:1024],inline=False)
        embed.add_field(name='DJ roles',value=roles[:1024],inline=False)
        embed.add_field(name='Minimum level',value=self.service.get(self.guild.id,'music:min_level','50'))
        return embed

    @discord.ui.button(label='Level requirement',row=2)
    async def level(self,i,button):
        await i.response.send_modal(OptionsModal(self.service,self.guild,self.author,'music'))

    @discord.ui.button(label='Allow all voice channels',row=2)
    async def all_channels(self,i,button):
        self.service.put(self.guild.id,'music:channels','')
        await i.response.edit_message(embed=self.preview(),view=MusicOptions(self.service,self.guild,self.author))

    @discord.ui.button(label='Disallow all voice channels',row=2)
    async def no_channels(self,i,button):
        self.service.put(self.guild.id,'music:channels','0')
        await i.response.edit_message(embed=self.preview(),view=MusicOptions(self.service,self.guild,self.author))

    @discord.ui.button(label='Back',row=2)
    async def back(self,i,button):
        view=ConfigView(self.service,self.guild,self.author,'music')
        await i.response.edit_message(embed=view.preview(),view=view)


OPTIONS = {
    'music': [('Minimum level (0–150)', 'music:min_level', '50')],
    'levels': [('XP cooldown seconds', 'levels:cooldown', '45'), ('XP multiplier (0–10)', 'levels:multiplier', '1'), ('Voice XP per minute (0–20)', 'levels:voice_xp', '0')],
    'security': [('Minimum account age (hours)', 'security:minimum_age_hours', '0'), ('Mention threshold', 'security:mention_limit', '8')],
    'social': [('Twitch login', 'social:twitch_login', ''), ('YouTube channel ID', 'social:youtube_channel', ''), ('TikTok username (metadata only unless provider configured)', 'social:tiktok_username', '')],
    'ai': [('OpenAI model (blank=host default)', 'ai:model', ''), ('Daily requests per server (1–100)', 'ai:daily_requests', '20')],
}


class OptionsModal(discord.ui.Modal):
    def __init__(self, service, guild, author, module):
        super().__init__(title=module.title() + ' options')
        self.service, self.guild, self.author, self.module = service, guild, author, module
        self.fields = []
        values = OPTIONS.get(module, [('Command prefix', 'prefix', '?'), ('Timezone (IANA)', 'timezone', 'UTC')])
        for label, key, default in values:
            field = discord.ui.TextInput(label=label[:45], default=service.get(guild.id, key, default), required=False, max_length=500)
            self.fields.append((key, field))
            self.add_item(field)

    async def on_submit(self, i):
        if not await self.service.check(i, self.guild.id, self.author):
            return
        import zoneinfo
        try:
            for key, field in self.fields:
                value = field.value.strip()
                if key == 'timezone':
                    zoneinfo.ZoneInfo(value)
                if key == 'prefix' and not 1 <= len(value) <= 5:
                    raise ValueError('Use a prefix with 1–5 characters.')
                if key in ('music:channels', 'music:dj_roles') and value:
                    ids = [int(x.strip()) for x in value.split(',')]
                    if any(not (self.guild.get_role(x) if 'roles' in key else self.guild.get_channel(x)) for x in ids):
                        raise ValueError('Every selected ID must belong to this server.')
                limits={'music:min_level':(0,150),'levels:cooldown':(5,3600),'levels:voice_xp':(0,20),
                    'ai:daily_requests':(1,100),'security:minimum_age_hours':(0,1000),'security:mention_limit':(1,1000)}
                if key in limits:
                    minimum,maximum=limits[key]
                    if not minimum <= int(value) <= maximum:
                        raise ValueError(f'{key} must be between {minimum} and {maximum}.')
                if key == 'levels:multiplier' and not 0 <= float(value) <= 10:
                    raise ValueError('Multiplier must be between 0 and 10.')
                if key=='ai:model' and value:
                    allowed={m.strip() for m in os.getenv('OPENAI_ALLOWED_MODELS',os.getenv('OPENAI_MODEL','gpt-4.1-mini')).split(',') if m.strip()}
                    if value not in allowed:
                        raise ValueError('Choose a model permitted by the host: '+', '.join(sorted(allowed)))
            for key, field in self.fields:
                self.service.put(self.guild.id, key, field.value.strip())
        except (ValueError, zoneinfo.ZoneInfoNotFoundError) as exc:
            return await private(i, str(exc)[:500])
        await private(i, 'Options saved for this server.')


class EntryView(discord.ui.View):
    def __init__(self, service, guild, author):
        super().__init__(timeout=300)
        self.service, self.guild, self.author = service, guild, author

    @discord.ui.button(label='Open private configuration', style=discord.ButtonStyle.primary)
    async def open(self, i, button):
        if await self.service.check(i, self.guild.id, self.author):
            view = ConfigView(self.service, self.guild, self.author)
            await private(i, embed=view.preview(), view=view)


class Configuration:
    def __init__(self, bot, con, get, put):
        self.bot, self.con, self.get, self.put = bot, con, get, put
        con.execute('CREATE TABLE IF NOT EXISTS config_backups (guild INTEGER, id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, data TEXT)')
        con.commit()

    async def check(self, i, guild_id, author=None):
        if not i.guild or i.guild.id != guild_id or (author and author != i.user.id) or not administrator(i.user):
            await private(i, 'Only an administrator of this server can change this configuration.')
            return False
        return True

    def export(self, guild):
        data = {k: v for k, v in self.con.execute('SELECT key,value FROM settings WHERE guild=?', (guild,))
                if not sensitive_setting(k) and k != 'preset:legacy_snapshot'}
        return json.dumps({'schema': 1, 'guild': str(guild), 'settings': data}, indent=2).encode()

    def import_data(self, guild, payload):
        data = json.loads(payload)
        if not isinstance(data,dict) or data.get('schema') != 1 or str(data.get('guild')) != str(guild.id):
            raise ValueError('The backup must belong to this server and use schema 1.')
        settings = data.get('settings')
        if not isinstance(settings, dict) or len(settings) > 5000:
            raise ValueError('Invalid settings backup.')
        for key, value in settings.items():
            if not isinstance(key, str) or not 1 <= len(key) <= 200 or not isinstance(value, str) or len(value) > 50000:
                raise ValueError('Invalid setting entry.')
            if sensitive_setting(key):
                raise ValueError('Credentials cannot be imported through Discord.')
            if key.startswith(('migration:', 'preset:')):
                # These are installation metadata, not user-editable configuration.
                continue
            if key.startswith('channel:') and value not in ('', '0') and (not value.isdecimal() or not guild.get_channel(int(value))):
                raise ValueError('Backup references a channel outside this server.')
            if key.startswith('role:') and value not in ('', '0') and (not value.isdecimal() or not guild.get_role(int(value))):
                raise ValueError('Backup references a role outside this server.')
            numeric_limits={'music:min_level':(0,150),'levels:cooldown':(5,3600),'levels:voice_xp':(0,20),
                'ai:daily_requests':(1,100),'ai:daily_tokens':(8000,200000),'security:minimum_age_hours':(0,1000),
                'security:mention_limit':(1,1000),'starboard:threshold':(1,100)}
            if key in numeric_limits:
                lower,upper=numeric_limits[key]
                if not value.isdecimal() or not lower<=int(value)<=upper:
                    raise ValueError('Backup contains an out-of-range numeric setting: '+key)
            if key.startswith('module:') and value not in ('0','1'):
                raise ValueError('Module flags must be 0 or 1.')
            if key in ('music:channels','music:dj_roles') and value not in ('','0'):
                ids=value.split(',')
                if any(not x.isdecimal() for x in ids):
                    raise ValueError('Music selections must contain existing Discord object IDs.')
                if key=='music:channels' and any(not isinstance(guild.get_channel(int(x)),discord.VoiceChannel) for x in ids):
                    raise ValueError('Music channels must be existing voice channels in this server.')
                if key=='music:dj_roles' and any(not guild.get_role(int(x)) or guild.get_role(int(x)).is_default() for x in ids):
                    raise ValueError('DJ roles must be existing roles other than @everyone in this server.')
            if key=='ai:model' and value:
                allowed={m.strip() for m in os.getenv('OPENAI_ALLOWED_MODELS',os.getenv('OPENAI_MODEL','gpt-4.1-mini')).split(',') if m.strip()}
                if value not in allowed:
                    raise ValueError('The backup selects an AI model not permitted by the host.')
            if key.startswith('command:'):
                policy=json.loads(value)
                if not isinstance(policy,dict) or not set(policy)<={'disabled','allowed_roles','blocked_roles','allowed_channels','blocked_channels'}:
                    raise ValueError('Invalid command policy in backup.')
                if 'disabled' in policy and type(policy['disabled']) is not bool:
                    raise ValueError('Command disabled flag must be a boolean.')
                for field in ('allowed_roles','blocked_roles','allowed_channels','blocked_channels'):
                    ids=policy.get(field,[])
                    if not isinstance(ids,list) or any(type(x) is not int or not (guild.get_role(x) if field.endswith('roles') else guild.get_channel(x)) for x in ids):
                        raise ValueError('Command policy references an unavailable server object.')
        self.con.executemany('INSERT INTO settings(guild,key,value) VALUES(?,?,?) ON CONFLICT(guild,key) DO UPDATE SET value=excluded.value',
                             [(guild.id, k, v) for k, v in settings.items() if not k.startswith(('migration:','preset:'))])
        self.con.commit()


def install(bot, con, get, put):
    service = Configuration(bot, con, get, put)
    bot.get_setting = get

    @bot.tree.command(name='setup', description='Configure this server using existing channels and roles')
    @app_commands.default_permissions(administrator=True)
    async def setup(i: discord.Interaction):
        if await service.check(i, i.guild_id):
            view = ConfigView(service, i.guild, i.user.id)
            await private(i, embed=view.preview(), view=view)

    bot.remove_command('setup')
    from discord.ext import commands
    @bot.command(name='setup')
    @commands.guild_only()
    async def prefix_setup(ctx):
        if not administrator(ctx.author):
            return await ctx.reply('Server Administrator permission is required.', delete_after=15)
        await ctx.reply('Configure this server privately:', view=EntryView(service, ctx.guild, ctx.author.id),
                        allowed_mentions=discord.AllowedMentions.none(), delete_after=300)

    group = bot.tree.get_command('config')
    if not group:
        group = app_commands.Group(name='config', description='Server configuration')
        bot.tree.add_command(group)

    @group.command(name='overview', description='Open this server’s configuration panel')
    async def overview(i: discord.Interaction):
        await setup.callback(i)

    @group.command(name='modules', description='Show enabled and disabled features')
    async def modules(i: discord.Interaction):
        if await service.check(i, i.guild_id):
            await private(i, '\n'.join(f'{x}: {"enabled" if enabled(get,i.guild_id,x) else "disabled"}' for x in MODULES))

    @group.command(name='module', description='Enable or disable one server feature')
    @app_commands.choices(feature=[app_commands.Choice(name=x, value=x) for x in MODULES])
    async def module(i: discord.Interaction, feature: app_commands.Choice[str], active: bool):
        if await service.check(i, i.guild_id):
            put(i.guild_id, 'module:' + feature.value, '1' if active else '0')
            await private(i, 'Feature configuration saved.')

    @group.command(name='prefix', description='Change this server’s text command prefix')
    async def prefix(i: discord.Interaction, value: str):
        if await service.check(i, i.guild_id):
            if not 1 <= len(value.strip()) <= 5:
                return await private(i, 'Use 1–5 nonblank characters.')
            put(i.guild_id, 'prefix', value.strip())
            await private(i, 'Prefix saved.')

    @group.command(name='timezone', description='Set this server’s IANA timezone')
    async def timezone(i: discord.Interaction, name: str):
        if await service.check(i, i.guild_id):
            import zoneinfo
            try:
                zoneinfo.ZoneInfo(name)
            except (ValueError, zoneinfo.ZoneInfoNotFoundError):
                return await private(i, 'Use an IANA timezone such as Europe/London.')
            put(i.guild_id, 'timezone', name)
            await private(i, 'Timezone saved.')

    @group.command(name='permissions', description='Inspect a command’s server policy')
    async def permissions(i: discord.Interaction, command: str):
        if await service.check(i, i.guild_id):
            await private(i, get(i.guild_id, 'command:' + command.strip().lstrip('/'), 'No additional restrictions.'))

    @group.command(name='command', description='Enable or disable a command for non-administrators')
    async def command(i: discord.Interaction, command: str, active: bool):
        if await service.check(i, i.guild_id):
            path = command.strip().lstrip('/')
            if path not in {x.qualified_name for x in bot.tree.walk_commands() if not isinstance(x, app_commands.Group)}:
                return await private(i, 'Choose an existing full command path.')
            policy = json.loads(get(i.guild_id, 'command:' + path, '{}'))
            policy['disabled'] = not active
            put(i.guild_id, 'command:' + path, json.dumps(policy))
            await private(i, 'Command policy saved.')

    def policy_command(name, kind, role):
        async def save(i, path, obj, active):
            if not await service.check(i, i.guild_id):
                return
            path = path.strip().lstrip('/')
            if path not in {x.qualified_name for x in bot.tree.walk_commands() if not isinstance(x, app_commands.Group)}:
                return await private(i, 'Choose an existing full command path.')
            if (obj.guild.id != i.guild_id):
                return await private(i, 'Choose an object from this server.')
            policy = json.loads(get(i.guild_id, 'command:' + path, '{}'))
            ids = set(policy.get(kind, []))
            ids.add(obj.id) if active else ids.discard(obj.id)
            policy[kind] = sorted(ids)
            put(i.guild_id, 'command:' + path, json.dumps(policy))
            await private(i, 'Command restrictions saved.')
        if role:
            async def callback(i: discord.Interaction, command: str, role: discord.Role, active: bool = True):
                await save(i, command, role, active)
        else:
            async def callback(i: discord.Interaction, command: str, channel: discord.TextChannel, active: bool = True):
                await save(i, command, channel, active)
        group.add_command(app_commands.Command(name=name, description='Configure a server command restriction', callback=callback))
    for name, kind, role in [('allowed-role','allowed_roles',True),('blocked-role','blocked_roles',True),
                             ('allowed-channel','allowed_channels',False),('blocked-channel','blocked_channels',False)]:
        policy_command(name, kind, role)

    @group.command(name='backup', description='Save a server settings snapshot')
    async def backup(i: discord.Interaction):
        if await service.check(i, i.guild_id):
            cursor = con.execute('INSERT INTO config_backups(guild,created,data) VALUES(?,?,?)',
                                 (i.guild_id, time.time(), service.export(i.guild_id).decode()))
            con.execute('DELETE FROM config_backups WHERE guild=? AND id NOT IN (SELECT id FROM config_backups WHERE guild=? ORDER BY id DESC LIMIT 20)',(i.guild_id,i.guild_id))
            con.commit()
            await private(i, f'Settings backup #{cursor.lastrowid} saved.')

    @group.command(name='backups', description='List saved snapshots for this server')
    async def backups(i: discord.Interaction):
        if await service.check(i, i.guild_id):
            rows = con.execute('SELECT id,created FROM config_backups WHERE guild=? ORDER BY id DESC LIMIT 20',(i.guild_id,)).fetchall()
            await private(i, '\n'.join(f'#{n} — <t:{int(t)}:F>' for n,t in rows) or 'No settings backups saved.')

    @group.command(name='restore', description='Restore one of this server’s settings snapshots')
    async def restore(i: discord.Interaction, backup_id: int):
        if await service.check(i, i.guild_id):
            row = con.execute('SELECT data FROM config_backups WHERE guild=? AND id=?',(i.guild_id,backup_id)).fetchone()
            if not row:
                return await private(i, 'Backup not found in this server.')
            try:
                service.import_data(i.guild, row[0])
            except ValueError as exc:
                return await private(i, str(exc))
            await private(i, 'Server settings restored. XP and moderation records were not changed.')

    @group.command(name='export', description='Download this server’s configuration')
    async def export(i: discord.Interaction):
        if await service.check(i, i.guild_id):
            await private(i, file=discord.File(io.BytesIO(service.export(i.guild_id)), filename='offhours-settings.json'))

    @group.command(name='import', description='Import a configuration backup belonging to this server')
    async def import_settings(i: discord.Interaction, attachment: discord.Attachment):
        if await service.check(i, i.guild_id):
            if attachment.size > 512000:
                return await private(i, 'Maximum backup size is 500 KB.')
            try:
                service.import_data(i.guild, await attachment.read())
            except (ValueError, TypeError):
                return await private(i, 'Invalid backup or references to another server.')
            await private(i, 'Server configuration imported.')

    return service
