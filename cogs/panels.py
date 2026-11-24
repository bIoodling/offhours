"""Dedicated reusable panel editors and persistent public interactions.

All configuration and interaction records are scoped by guild. Saving a draft
never creates channels, roles or categories and publishing requires a click.
"""
import asyncio
import copy
import datetime
import html
import io
import ipaddress
import json
import logging
import random
import re
import time
from urllib.parse import urlsplit

import discord
from discord import app_commands

log = logging.getLogger('offhours.panels')
KINDS = {
    'welcome': ('setup-join-panel', 'Welcome to {server.name}', '{user.mention} joined. Member #{server.member_count}.'),
    'leave': ('setup-leave-panel', 'Member left', '{user.name} left {server.name}.'),
    'invite': ('setup-invite-panel', 'Invite tracker', '{user.name} joined. Inviter: {inviter.name}.'),
    'level': ('setup-level-panel', 'Level up', '{user.mention} reached level {level}.'),
    'boost': ('setup-boost-panel', 'Thank you for boosting', '{user.mention} boosted {server.name}.'),
    'verify': ('setup-verify-panel', 'Verification', 'Press Verify to receive the configured member role.'),
    'reaction_roles': ('setup-reaction-roles', 'Choose your roles', 'Use the role menu below to update your choices.'),
    'suggestions': ('setup-suggestions', 'Server suggestions', 'Share a suggestion with the server.'),
    'giveaways': ('setup-giveaways', 'Giveaways', 'Use the button to see active giveaways.'),
    'starboard': ('setup-starboard', 'Starboard', 'Messages reaching the configured star threshold appear in this channel.'),
    'logs': ('setup-logs', 'Moderation log', '{event}'),
    'live': ('setup-live-alerts', 'Live notifications', '{event}'),
    'video': ('setup-video-alerts', 'Video notifications', '{event}'),
    'rules': ('setup-rules-panel', 'Server rules', 'Be respectful. Follow Discord’s Terms of Service and Community Guidelines.'),
    'roles': ('setup-roles', 'Choose your roles', 'Use the role selector to update your roles.'),
}
VARIABLES = ['{user}', '{user.mention}', '{user.name}', '{user.id}', '{user.avatar}', '{user.banner}',
             '{server.name}', '{server.icon}', '{server.member_count}', '{member.number}',
             '{member.joined_at}', '{member.created_at}', '{inviter.name}', '{inviter.mention}',
             '{inviter.invites}', '{invite.code}', '{level}', '{xp}', '{ticket.id}', '{ticket.owner}',
             '{ticket.category}', '{ticket.reason}', '{timestamp}', '{event}']
TEXT_FIELDS = {
    'title': ('Title', 200), 'description': ('Description', 3500), 'content': ('Text outside embed', 1900),
    'colour': ('Embed colour (#RRGGBB)', 7), 'author': ('Author text', 256),
    'author_icon': ('Author icon URL', 1000), 'footer': ('Footer text', 1000),
    'footer_icon': ('Footer icon URL', 1000), 'thumbnail': ('Thumbnail URL', 1000),
    'banner': ('Banner URL / member banner fallback', 1000), 'image': ('Image URL', 1000),
    'star_threshold': ('Starboard reaction threshold', 3),
    'button_label': ('Public button label', 80), 'button_style': ('Button style: secondary/primary/success/danger', 10),
    'role_placeholder': ('Role menu placeholder', 150),
}
TOGGLES = {
    'enabled': 'Feature enabled', 'show_avatar': 'Member avatar', 'show_banner': 'Member banner',
    'timestamp': 'Timestamp', 'member_number': 'Member number', 'created_at': 'Account creation date',
    'joined_at': 'Server join date', 'inviter': 'Inviter', 'invite_count': 'Invite count',
    'server_icon': 'Server icon', 'mention_users': 'Allow member mentions',
    'mention_roles': 'Allow selected role mentions', 'mention_everyone': 'Allow @everyone mentions',
    'exclusive_roles': 'Allow only one selected self-role',
}


def defaults(kind):
    return dict(title=KINDS[kind][1], description=KINDS[kind][2], content='', colour='D4D4D4',
                enabled=False, channel_id=0, destination_id=0, role_ids=[], author='', author_icon='',
                footer='', footer_icon='', thumbnail='', banner='', image='', show_avatar=True,
                show_banner=False, timestamp=False, member_number=False, created_at=False,
                joined_at=False, inviter=False, invite_count=False, server_icon=False,
                mention_users=False, mention_roles=False, mention_everyone=False,
                exclusive_roles=False, star_threshold=3, button_label='', button_style='secondary',
                role_placeholder='Choose your roles')


def safe_image(url):
    if not url:
        return ''
    if any(v in url for v in ('{user.avatar}', '{user.banner}', '{server.icon}')):
        return url
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or len(url) > 1000:
        raise ValueError('Images must use a public HTTPS URL without credentials.')
    host = parsed.hostname.rstrip('.').lower()
    if host == 'localhost' or host.endswith(('.local', '.internal', '.localhost')):
        raise ValueError('Use a public HTTPS image host.')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ValueError('Use a public HTTPS image host.')
    return url


async def reply(i, message, **kwargs):
    kwargs.setdefault('ephemeral', True)
    kwargs.setdefault('allowed_mentions', discord.AllowedMentions.none())
    if i.response.is_done():
        return await i.followup.send(message[:1900], **kwargs)
    return await i.response.send_message(message[:1900], **kwargs)


def can_manage(i):
    return bool(i.guild and isinstance(i.user, discord.Member) and i.user.guild_permissions.manage_guild)


class EditField(discord.ui.Modal):
    def __init__(self, editor, key):
        label, limit = TEXT_FIELDS[key]
        super().__init__(title=label[:45])
        self.editor, self.key = editor, key
        self.value = discord.ui.TextInput(label=label[:45], default=str(editor.config.get(key, ''))[:limit],
            max_length=limit, required=False, style=discord.TextStyle.paragraph if limit > 1000 else discord.TextStyle.short)
        self.add_item(self.value)

    async def on_submit(self, i):
        if not await self.editor.interaction_check(i):
            return
        value = str(self.value)
        try:
            if self.key == 'colour' and not re.fullmatch(r'#?[a-fA-F0-9]{6}', value):
                raise ValueError('Use a six-digit hex colour, such as #D4D4D4.')
            if self.key in ('author_icon', 'footer_icon', 'thumbnail', 'banner', 'image'):
                safe_image(value)
            if self.key == 'star_threshold':
                if not value.isdecimal() or not 1 <= int(value) <= 100:
                    raise ValueError('The star threshold must be between 1 and 100.')
                value = int(value)
            if self.key == 'button_style' and value not in ('secondary','primary','success','danger'):
                raise ValueError('Choose secondary, primary, success or danger.')
            self.editor.config[self.key] = value
            self.editor.last_field = self.key
            await i.response.edit_message(embed=self.editor.preview(i.user), view=self.editor)
        except ValueError as exc:
            await reply(i, str(exc))


class VariableMenu(discord.ui.View):
    def __init__(self, editor):
        super().__init__(timeout=180)
        self.editor = editor
        select = discord.ui.Select(placeholder='Insert into ' + editor.last_field,
            options=[discord.SelectOption(label=v, value=v) for v in VARIABLES])
        async def insert(i):
            if not await editor.interaction_check(i):
                return
            key = editor.last_field if editor.last_field in ('title', 'description', 'content', 'author', 'footer') else 'description'
            limit = TEXT_FIELDS[key][1]
            editor.config[key] = (str(editor.config.get(key, '')) + select.values[0])[:limit]
            await i.response.edit_message(content='Variable inserted. Continue editing below.', embed=editor.preview(i.user), view=editor)
        select.callback = insert
        self.add_item(select)


class PanelEditor(discord.ui.View):
    def __init__(self, service, i, kind):
        super().__init__(timeout=900)
        self.service, self.actor, self.guild_id, self.kind = service, i.user.id, i.guild_id, kind
        self.config = service.load(i.guild_id, kind)
        self.last_field = 'description'
        self.busy = False
        fields = discord.ui.Select(placeholder='Edit text, images, colour or insert a variable', row=1,
            options=[discord.SelectOption(label=label, value=key) for key, (label, _) in TEXT_FIELDS.items()]
            + [discord.SelectOption(label='Insert variable', value='variables'),
               discord.SelectOption(label='Change destination channel', value='destination')])
        async def choose_field(interaction):
            key = fields.values[0]
            if key == 'variables':
                return await interaction.response.edit_message(view=VariableMenu(self))
            if key == 'destination':
                return await interaction.response.send_message('Select the channel for event output or submitted suggestions.',
                    ephemeral=True, view=DestinationMenu(self))
            await interaction.response.send_modal(EditField(self, key))
        fields.callback = choose_field
        self.add_item(fields)
        flags = discord.ui.Select(placeholder='Toggle visibility, mentions or enabled state', row=2,
            options=[discord.SelectOption(label=v, value=k) for k, v in TOGGLES.items()])
        async def toggle(interaction):
            key = flags.values[0]
            if key in ('mention_everyone','mention_roles') and not self.config.get(key,False) and not interaction.user.guild_permissions.mention_everyone:
                return await reply(interaction, 'Mention Everyone permission is required to enable unrestricted group mentions.')
            self.config[key] = not self.config.get(key, False)
            await interaction.response.edit_message(embed=self.preview(interaction.user), view=self)
        flags.callback = toggle
        self.add_item(flags)
        channel = discord.ui.ChannelSelect(placeholder='Choose existing channel for this panel', row=3,
            channel_types=[discord.ChannelType.text, discord.ChannelType.news])
        async def channel_pick(interaction):
            self.config['channel_id'] = channel.values[0].id
            await interaction.response.edit_message(embed=self.preview(interaction.user), view=self)
        channel.callback = channel_pick
        self.add_item(channel)
        roles = discord.ui.RoleSelect(placeholder='Choose existing verification/self/notification roles', row=4, min_values=0, max_values=25)
        async def role_pick(interaction):
            picked = [interaction.guild.get_role(r.id) for r in roles.values]
            for role in picked:
                if not role or role.managed or role.is_default() or role >= interaction.guild.me.top_role or (
                        interaction.user.id != interaction.guild.owner_id and role >= interaction.user.top_role):
                    return await reply(interaction, 'Choose ordinary roles below your role and the bot role.')
                if role.permissions.administrator or role.permissions.manage_guild or role.permissions.manage_roles:
                    return await reply(interaction, 'Administrative roles cannot be configured as public panel roles.')
            self.config['role_ids'] = [r.id for r in picked]
            await interaction.response.edit_message(embed=self.preview(interaction.user), view=self)
        roles.callback = role_pick
        self.add_item(roles)

    async def interaction_check(self, i):
        if i.guild_id != self.guild_id or i.user.id != self.actor or not can_manage(i):
            await reply(i, 'Only the administrator who opened this editor may use it.')
            return False
        return True

    def preview(self, member):
        embed = self.service.render(self.kind, self.config, member, preview=True)
        enabled = 'enabled' if self.config.get('enabled') else 'disabled'
        selected = self.config.get('channel_id', 0)
        suffix = f'Private preview • {self.kind} • {enabled} • channel {selected or "not selected"} • unsaved changes'
        embed.set_footer(text=(str(self.config.get('footer', '')) + '\n' + suffix)[-2048:])
        return embed

    @discord.ui.button(label='Save', style=discord.ButtonStyle.success, row=0)
    async def save(self, i, button):
        self.service.save(self.guild_id, self.kind, self.config)
        await i.response.edit_message(content='Configuration saved for this server.', embed=self.preview(i.user), view=self)

    @discord.ui.button(label='Publish', style=discord.ButtonStyle.primary, row=0)
    async def publish(self, i, button):
        if self.busy:
            return await reply(i, 'A publish request is already running.')
        self.busy = True
        await i.response.defer(ephemeral=True)
        try:
            self.service.save(self.guild_id, self.kind, self.config)
            message = await self.service.publish(self.kind, self.config, i.user)
            await reply(i, f'Panel published: {message.jump_url}')
        except ValueError as exc:
            await reply(i, str(exc))
        except discord.HTTPException:
            await reply(i, 'Discord did not accept this panel. Check the chosen channel and bot permissions.')
        finally:
            self.busy = False

    @discord.ui.button(label='Preview', style=discord.ButtonStyle.secondary, row=0)
    async def show_preview(self, i, button):
        await i.response.edit_message(content='Private preview only. No roles or statistics have changed.', embed=self.preview(i.user), view=self)

    @discord.ui.button(label='Reset', style=discord.ButtonStyle.secondary, row=0)
    async def reset(self, i, button):
        self.config = defaults(self.kind)
        await i.response.edit_message(content='Defaults loaded into this draft. Save to keep them.', embed=self.preview(i.user), view=self)

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.danger, row=0)
    async def cancel(self, i, button):
        self.stop()
        await i.response.edit_message(content='Editor closed. Unsaved changes discarded.', embed=None, view=None)


class DestinationMenu(discord.ui.View):
    def __init__(self, editor):
        super().__init__(timeout=180)
        select = discord.ui.ChannelSelect(placeholder='Existing destination channel', channel_types=[discord.ChannelType.text, discord.ChannelType.news])
        async def callback(i):
            if not await editor.interaction_check(i):
                return
            editor.config['destination_id'] = select.values[0].id
            await i.response.edit_message(content='Destination selected. Save in the main editor to keep this change.', view=None)
        select.callback = callback
        self.add_item(select)


class VerifyPanel(discord.ui.View):
    def __init__(self, service, config=None):
        super().__init__(timeout=None)
        self.service = service
        if config:
            self.verify.label = config.get('button_label') or 'Verify'
            self.verify.style = getattr(discord.ButtonStyle, config.get('button_style','secondary'), discord.ButtonStyle.secondary)

    @discord.ui.button(label='Verify', style=discord.ButtonStyle.success, custom_id='offhours:panel:verify:v2')
    async def verify(self, i, button):
        if not i.guild or not isinstance(i.user, discord.Member):
            return await reply(i, 'Use verification in the server.')
        config = self.service.load(i.guild_id, 'verify')
        if not self.service.is_enabled(i.guild_id, 'verify', config):
            return await reply(i, 'Verification is disabled for this server.')
        roles = [i.guild.get_role(int(r)) for r in config.get('role_ids', [])]
        if not roles or any(not r or r.managed or r >= i.guild.me.top_role or r.permissions.administrator or r.permissions.manage_roles for r in roles):
            return await reply(i, 'Verification roles are not configured or are not assignable.')
        await i.response.defer(ephemeral=True)
        try:
            await i.user.add_roles(*roles, reason='Member used configured verification panel')
            pending = self.service.get(i.guild_id, 'role:pending', '0')
            pending_role = i.guild.get_role(int(pending)) if pending.isdecimal() else None
            if pending_role and pending_role in i.user.roles:
                await i.user.remove_roles(pending_role, reason='Completed verification')
            await reply(i, 'Discord confirmed your verification roles.')
        except discord.HTTPException:
            await reply(i, 'Discord could not apply verification roles. Staff must check permissions and role hierarchy.')


class SelfRoles(discord.ui.View):
    def __init__(self, service, guild=None, kind='reaction_roles'):
        super().__init__(timeout=None)
        self.service = service
        roles = []
        if guild:
            roles = [guild.get_role(int(r)) for r in service.load(guild.id, kind).get('role_ids', [])]
            roles = [r for r in roles if r]
        config = service.load(guild.id, kind) if guild else {}
        select = discord.ui.Select(placeholder=str(config.get('role_placeholder') or 'Choose your roles')[:150], min_values=0, max_values=max(1, len(roles)),
            options=[discord.SelectOption(label=r.name[:100], value=str(r.id)) for r in roles] or [discord.SelectOption(label='No roles configured', value='0')],
            custom_id='offhours:panel:selfroles:v2:' + kind)
        async def callback(i):
            if not i.guild or not isinstance(i.user, discord.Member):
                return await reply(i, 'Use this menu in its server.')
            config = service.load(i.guild_id, kind)
            if not service.is_enabled(i.guild_id, kind, config):
                return await reply(i, 'Self roles are disabled for this server.')
            allowed = {int(r) for r in config.get('role_ids', [])}
            chosen = {int(x) for x in i.data.get('values', []) if str(x).isdecimal()}
            if not chosen <= allowed or config.get('exclusive_roles') and len(chosen) > 1:
                return await reply(i, 'Choose only currently configured roles; this panel may allow only one role.')
            roles_to_use = [i.guild.get_role(r) for r in allowed]
            if any(not r or r.managed or r >= i.guild.me.top_role or r.permissions.administrator or r.permissions.manage_guild or r.permissions.manage_roles for r in roles_to_use):
                return await reply(i, 'One of these roles is protected or no longer available. Contact an administrator.')
            await i.response.defer(ephemeral=True)
            try:
                remove = [r for r in i.user.roles if r.id in allowed - chosen]
                add = [r for r in roles_to_use if r.id in chosen and r not in i.user.roles]
                if remove:
                    await i.user.remove_roles(*remove, reason='Member changed self roles')
                if add:
                    await i.user.add_roles(*add, reason='Member selected self roles')
                await reply(i, 'Discord confirmed your selected roles.')
            except discord.HTTPException:
                await reply(i, 'Discord could not finish updating your roles. Contact an administrator.')
        select.callback = callback
        self.add_item(select)


class SuggestionsPanel(discord.ui.View):
    def __init__(self, service, config=None):
        super().__init__(timeout=None)
        self.service = service
        if config:
            self.submit.label = config.get('button_label') or 'Submit suggestion'
            self.submit.style = getattr(discord.ButtonStyle, config.get('button_style','secondary'), discord.ButtonStyle.secondary)

    @discord.ui.button(label='Submit suggestion', style=discord.ButtonStyle.primary, custom_id='offhours:panel:suggest:v2')
    async def submit(self, i, button):
        if not i.guild or not self.service.is_enabled(i.guild_id, 'suggestions'):
            return await reply(i, 'Suggestions are disabled.')
        community = getattr(self.service.bot, 'community_service', None)
        if community is None:
            return await reply(i, 'The suggestions service is unavailable. Ask an administrator to check the host configuration.')
        from services.community import SuggestionModal
        await i.response.send_modal(SuggestionModal(community))


class GiveawaysPanel(discord.ui.View):
    def __init__(self, service, config=None):
        super().__init__(timeout=None)
        self.service = service
        if config:
            self.active.label = config.get('button_label') or 'Active giveaways'
            self.active.style = getattr(discord.ButtonStyle, config.get('button_style','secondary'), discord.ButtonStyle.secondary)

    @discord.ui.button(label='Active giveaways', style=discord.ButtonStyle.primary, custom_id='offhours:panel:giveaways:v2')
    async def active(self, i, button):
        rows = self.service.db.execute('SELECT id,prize,ends,channel,message FROM community_giveaways WHERE guild=? AND status=? ORDER BY ends LIMIT 15', (i.guild_id, 'active')).fetchall()
        await reply(i, '\n'.join(f'#{n}: {prize[:80]} — <t:{int(ends)}:R> — https://discord.com/channels/{i.guild_id}/{channel}/{message}' for n, prize, ends, channel, message in rows) or 'No active giveaways in this server.')


class PanelService:
    def __init__(self, bot, db, get_setting, put_setting):
        self.bot, self.db, self.get, self.put = bot, db, get_setting, put_setting
        self.task = None
        self.lock = asyncio.Lock()
        self.db.execute('CREATE TABLE IF NOT EXISTS panel_assets(guild INTEGER,kind TEXT,data BLOB,filename TEXT,PRIMARY KEY(guild,kind))')
        self.db.commit()

    def load(self, guild_id, kind):
        result = defaults(kind)
        try:
            value = json.loads(self.get(guild_id, 'panel:' + kind, '{}'))
            if isinstance(value, dict):
                result.update(value)
        except (ValueError, TypeError):
            log.warning('Invalid panel configuration guild=%s kind=%s', guild_id, kind)
        return result

    def save(self, guild_id, kind, config):
        self.put(guild_id, 'panel:' + kind, json.dumps(config, ensure_ascii=False))
        module = {'verify':'verification','reaction_roles':'selfroles','roles':'selfroles','invite':'invites','level':'levels','live':'social','video':'social'}.get(kind,kind)
        if module != 'rules':
            siblings = {'live':'video','video':'live','reaction_roles':'roles','roles':'reaction_roles'}
            sibling_enabled = self.load(guild_id, siblings[kind]).get('enabled') if kind in siblings else False
            self.put(guild_id, 'module:' + module, '1' if config.get('enabled') or sibling_enabled else '0')
        slot = {'level':'levels','verify':'verify','reaction_roles':'selfroles','roles':'selfroles'}.get(kind,kind)
        destination = int(config.get('destination_id') or config.get('channel_id') or 0)
        if destination:
            self.put(guild_id, 'channel:' + slot, str(destination))
        selected_roles = config.get('role_ids', [])
        if kind == 'verify' and selected_roles:
            self.put(guild_id, 'role:regular', str(selected_roles[0]))
        if kind in ('live','video') and selected_roles:
            self.put(guild_id, 'role:' + kind, str(selected_roles[0]))
        if kind in ('suggestions','starboard'):
            self.put(guild_id, kind + ':channel', str(config.get('destination_id') or config.get('channel_id') or 0))
        if kind == 'starboard':
            self.put(guild_id, 'starboard:threshold', str(config.get('star_threshold',3)))

    def is_enabled(self, guild_id, kind, config=None, require_panel=True):
        config = config or self.load(guild_id, kind)
        module = {'verify':'verification','reaction_roles':'selfroles','roles':'selfroles','invite':'invites','level':'levels','live':'social','video':'social'}.get(kind,kind)
        return (bool(config.get('enabled')) or not require_panel) and self.get(guild_id, 'module:' + module, '1' if config.get('enabled') else '0') == '1'

    def variables(self, member, **extra):
        guild = member.guild
        variables = {
            'user': member.mention, 'user.mention': member.mention, 'user.name': member.display_name,
            'user.id': str(member.id), 'user.avatar': member.display_avatar.url,
            'user.banner': getattr(getattr(member, 'banner', None), 'url', ''),
            'server.name': guild.name, 'server.icon': guild.icon.url if guild.icon else '',
            'server.member_count': str(guild.member_count or 0), 'member.number': str(guild.member_count or 0),
            'member.joined_at': discord.utils.format_dt(member.joined_at, 'F') if member.joined_at else 'Unknown',
            'member.created_at': discord.utils.format_dt(member.created_at, 'F'),
            'inviter.name': 'Unknown', 'inviter.mention': 'Unknown', 'inviter.invites': 'Unknown',
            'invite.code': 'Unknown', 'level': '0', 'xp': '0', 'ticket.id': '', 'ticket.owner': '',
            'ticket.category': '', 'ticket.reason': '', 'timestamp': discord.utils.format_dt(discord.utils.utcnow(), 'F'),
            'event': '',
        }
        variables.update({k: str(v) for k, v in extra.items()})
        return variables

    def text(self, template, variables):
        # Only fixed variable substitutions; never Python format attribute traversal.
        return re.sub(r'\{([a-z_.]+)\}', lambda match: variables.get(match[1], match[0]), str(template))

    def render(self, kind, config, member, preview=False, **extra):
        variables = self.variables(member, **extra)
        colour = str(config.get('colour', 'D4D4D4')).lstrip('#')
        if not re.fullmatch('[a-fA-F0-9]{6}', colour):
            colour = 'D4D4D4'
        embed = discord.Embed(title=self.text(config.get('title', ''), variables)[:256] or None,
            description=self.text(config.get('description', ''), variables)[:4096] or None, colour=int(colour, 16))
        if config.get('author'):
            embed.set_author(name=self.text(config['author'], variables)[:256], icon_url=self.text(config.get('author_icon', ''), variables) or None)
        if config.get('footer'):
            embed.set_footer(text=self.text(config['footer'], variables)[:2048], icon_url=self.text(config.get('footer_icon', ''), variables) or None)
        thumbnail = self.text(config.get('thumbnail', ''), variables)
        if not thumbnail and config.get('show_avatar'):
            thumbnail = member.display_avatar.url
        if not thumbnail and config.get('server_icon'):
            thumbnail = variables['server.icon']
        if thumbnail.startswith('https://'):
            embed.set_thumbnail(url=thumbnail)
        image = self.text(config.get('image', '') or config.get('banner', ''), variables)
        if config.get('show_banner'):
            image = variables['user.banner'] or image
        if image.startswith(('https://', 'attachment://')):
            embed.set_image(url=image)
        if config.get('timestamp'):
            embed.timestamp = discord.utils.utcnow()
        for flag, title, key in [
            ('member_number', 'Member number', 'member.number'), ('created_at', 'Account created', 'member.created_at'),
            ('joined_at', 'Joined server', 'member.joined_at'), ('inviter', 'Inviter', 'inviter.name'),
            ('invite_count', 'Inviter’s invites', 'inviter.invites'),
        ]:
            if config.get(flag):
                embed.add_field(name=title, value=variables[key][:1024] or 'Unknown')
        if preview:
            embed.set_footer(text='Private preview — no events simulated and no statistics changed')
        if len(embed) > 5600 and embed.description:
            embed.description = embed.description[:max(1, len(embed.description) - (len(embed) - 5600))]
        return embed

    def asset_files(self, guild_id, kind, config):
        if str(config.get('image','')).startswith('attachment://'):
            row = self.db.execute('SELECT data,filename FROM panel_assets WHERE guild=? AND kind=?', (guild_id,kind)).fetchone()
            if row:
                return [discord.File(io.BytesIO(row[0]), filename=row[1])]
        return []

    async def store_asset(self, guild_id, kind, image):
        if image.content_type not in ('image/png','image/jpeg','image/webp','image/gif') or image.size > 3*1024*1024:
            raise ValueError('Attach a PNG, JPEG, WebP or GIF under 3 MB.')
        extension = {'image/png':'png','image/jpeg':'jpg','image/webp':'webp','image/gif':'gif'}[image.content_type]
        filename = f'panel-{kind}.{extension}'
        data = await image.read()
        self.db.execute('INSERT INTO panel_assets(guild,kind,data,filename) VALUES(?,?,?,?) ON CONFLICT(guild,kind) DO UPDATE SET data=excluded.data,filename=excluded.filename', (guild_id,kind,data,filename))
        self.db.commit()
        return 'attachment://' + filename

    def mentions(self, config, guild):
        roles = [guild.get_role(int(r)) for r in config.get('role_ids', [])]
        return discord.AllowedMentions(everyone=bool(config.get('mention_everyone')),
            users=bool(config.get('mention_users')), roles=[r for r in roles if r] if config.get('mention_roles') else False)

    def view(self, kind, guild):
        config = self.load(guild.id, kind)
        return {'verify': lambda: VerifyPanel(self,config), 'reaction_roles': lambda: SelfRoles(self, guild), 'roles': lambda: SelfRoles(self, guild, kind='roles'),
                'suggestions': lambda: SuggestionsPanel(self,config), 'giveaways': lambda: GiveawaysPanel(self,config)}.get(kind, lambda: None)()

    async def publish(self, kind, config, member):
        guild = member.guild
        channel = guild.get_channel(int(config.get('channel_id') or 0))
        if not isinstance(channel, discord.TextChannel):
            raise ValueError('Select an existing text channel first.')
        if not channel.permissions_for(member).send_messages:
            raise ValueError('You cannot publish in the selected channel.')
        if kind == 'verify' and not config.get('role_ids'):
            raise ValueError('Select an existing verification role first.')
        if kind in ('reaction_roles', 'roles') and not config.get('role_ids'):
            raise ValueError('Select existing self roles first.')
        variables = self.variables(member)
        message = await channel.send(content=self.text(config.get('content', ''), variables)[:1900] or None,
            embed=self.render(kind, config, member), view=self.view(kind, guild), files=self.asset_files(guild.id, kind, config),
            allowed_mentions=self.mentions(config, guild), silent=True)
        config['message_id'] = message.id
        self.save(guild.id, kind, config)
        return message

    async def emit(self, kind, member, **variables):
        config = self.load(member.guild.id, kind)
        if not self.is_enabled(member.guild.id, kind, config):
            return None
        channel = member.guild.get_channel(int(config.get('destination_id') or config.get('channel_id') or 0))
        if not isinstance(channel, discord.TextChannel):
            return None
        if config.get('show_banner') and 'user.banner' not in variables:
            try:
                user = await self.bot.fetch_user(member.id)
                variables['user.banner'] = user.banner.url if user.banner else ''
            except discord.HTTPException:
                variables['user.banner'] = ''
        return await channel.send(content=self.text(config.get('content', ''), self.variables(member, **variables))[:1900] or None,
            embed=self.render(kind, config, member, **variables), files=self.asset_files(member.guild.id, kind, config), allowed_mentions=self.mentions(config, member.guild), silent=True)

    async def close(self):
        return None  # The community service owns its own persistent scheduler.


def install(bot, connection, get_setting, put_setting):
    service = PanelService(bot, connection, get_setting, put_setting)
    for kind, (name, _, _) in KINDS.items():
        def make_callback(kind):
            async def callback(i: discord.Interaction, image: discord.Attachment = None):
                if not can_manage(i):
                    return await reply(i, 'Manage Server permission is required to configure panels.')
                view = PanelEditor(service, i, kind)
                if image:
                    try:
                        view.config['image'] = await service.store_asset(i.guild_id, kind, image)
                    except ValueError as exc:
                        return await reply(i, str(exc))
                await i.response.send_message(embed=view.preview(i.user), files=service.asset_files(i.guild_id, kind, view.config), view=view, ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none())
            return callback
        callback = app_commands.guild_only()(app_commands.default_permissions(manage_guild=True)(make_callback(kind)))
        bot.tree.add_command(app_commands.Command(name=name, description=f'Configure the {kind.replace("_"," ")} panel with a private editor', callback=callback))

    group = app_commands.Group(name='panel', description='Publish, manage and back up server panels', default_permissions=discord.Permissions(manage_guild=True), guild_only=True)

    @group.command(name='list', description='List saved panel channels and enabled states')
    async def list_panels(i: discord.Interaction):
        if not can_manage(i):
            return await reply(i, 'Manage Server required.')
        await reply(i, '\n'.join(f'{kind}: {"enabled" if (config := service.load(i.guild_id,kind)).get("enabled") else "disabled"}, channel {config.get("channel_id") or "not configured"}' for kind in KINDS))

    @group.command(name='export', description='Export this server’s saved panel configuration privately')
    async def export(i: discord.Interaction):
        if not can_manage(i):
            return await reply(i, 'Manage Server required.')
        data = json.dumps({kind: service.load(i.guild_id, kind) for kind in KINDS}, ensure_ascii=False, indent=2).encode()
        await reply(i, 'Panel configuration for this server.', file=discord.File(io.BytesIO(data), filename=f'panels-{i.guild_id}.json'))

    @group.command(name='image', description='Store a panel image as a persistent Discord attachment')
    @app_commands.choices(kind=[app_commands.Choice(name=k, value=k) for k in KINDS])
    async def upload_image(i: discord.Interaction, kind: app_commands.Choice[str], image: discord.Attachment):
        if not can_manage(i):
            return await reply(i, 'Manage Server required.')
        await i.response.defer(ephemeral=True)
        try:
            config = service.load(i.guild_id, kind.value)
            config['image'] = await service.store_asset(i.guild_id, kind.value, image)
            service.save(i.guild_id, kind.value, config)
            await reply(i, 'Image saved to persistent server storage. Publish from the panel editor to display it.')
        except ValueError as exc:
            await reply(i, str(exc))

    bot.tree.add_command(group)

    async def ready():
        if getattr(bot, '_offhours_panels_ready', False):
            return
        bot._offhours_panels_ready = True
        bot.add_view(VerifyPanel(service))
        bot.add_view(SuggestionsPanel(service))
        bot.add_view(GiveawaysPanel(service))
        for guild in bot.guilds:
            for kind in ('reaction_roles','roles'):
                config = service.load(guild.id, kind)
                if config.get('message_id'):
                    bot.add_view(SelfRoles(service, guild, kind=kind), message_id=int(config['message_id']))
    bot.add_listener(ready, 'on_ready')

    async def member_update(before, after):
        if not before.premium_since and after.premium_since:
            try:
                await service.emit('boost', after)
            except discord.HTTPException:
                log.warning('Boost panel failed guild=%s', after.guild.id)
    bot.add_listener(member_update, 'on_member_update')

    async def member_remove(member):
        if not member.bot:
            try:
                await service.emit('leave', member)
            except discord.HTTPException:
                log.warning('Leave panel failed guild=%s', member.guild.id)
    bot.add_listener(member_remove, 'on_member_remove')

    return service
