"""Administrator selected existing roles; persistent, guild-local member menus."""
import contextlib
import json
import re

import discord
from discord import app_commands
from services.configuration import administrator, enabled, private


ELEVATED_PERMISSIONS = ('administrator', 'manage_guild', 'manage_roles', 'manage_channels',
                        'manage_webhooks', 'ban_members', 'kick_members', 'moderate_members',
                        'manage_messages', 'mention_everyone')


def safe_reward_role(role, bot_member):
    return bool(role and not role.managed and not role.is_default() and role < bot_member.top_role
                and not any(getattr(role.permissions, key, False) for key in ELEVATED_PERMISSIONS))


class RoleMenu(discord.ui.View):
    def __init__(self, service, guild_id, category, persistent=False):
        super().__init__(timeout=None)
        self.service=service
        self.category=category
        rows=service.choices(guild_id,category) if guild_id else []
        options=[discord.SelectOption(label=label[:100],value=str(role)) for label,role in rows[:25]]
        select=discord.ui.Select(placeholder='Choose '+category+' roles', min_values=0,
            max_values=max(1,len(options)) if category=='notifications' else 1,
            options=options or [discord.SelectOption(label='No roles configured',value='0')],
            custom_id='offhours:selfroles:v2:'+category)
        async def callback(i):
            if not i.guild or not enabled(service.get,i.guild_id,'selfroles'):
                return await private(i,'Self-roles are disabled in this server.')
            rows=service.choices(i.guild_id,category)
            allowed={role for _,role in rows}
            wanted={int(x) for x in select.values if x.isdecimal()}
            if not wanted<=allowed:
                return await private(i,'This menu is out of date. Open a new one with /colour, /region or /notifications.')
            if category=='colour':
                row=service.con.execute('SELECT xp FROM xp WHERE guild=? AND user=?',(i.guild_id,i.user.id)).fetchone()
                if row and row[0]>=750:
                    return await private(i,'Starter colours are replaced by XP reward colours after level 5.')
            add=[i.guild.get_role(x) for x in wanted]
            if any(not safe_reward_role(r, i.guild.me) for r in add):
                return await private(i,'A selected role cannot safely be assigned. Ask a server administrator to update it.')
            remove=[r for r in i.user.roles if r.id in allowed and r.id not in wanted]
            await i.response.defer(ephemeral=True)
            try:
                if remove:await i.user.remove_roles(*remove,reason='Member updated configured self-roles')
                if add:await i.user.add_roles(*add,reason='Member selected configured self-roles')
                await private(i,'Your '+category+' roles were updated.')
            except discord.HTTPException:
                await private(i,'Discord refused a role change. Check the bot’s role hierarchy.')
        select.callback=callback
        self.add_item(select)


class SelfRoles:
    def __init__(self,bot,con,get,put):
        self.bot,self.con,self.get,self.put=bot,con,get,put

    def choices(self,guild,category):
        prefix='selfrole:'+category+':'
        return [(key[len(prefix):],int(value)) for key,value in self.con.execute(
            'SELECT key,value FROM settings WHERE guild=? AND key LIKE ? ORDER BY key',(guild,prefix+'%')) if value.isdecimal() and int(value)>0]

    async def show(self,i,category):
        if not enabled(self.get,i.guild_id,'selfroles'):
            return await private(i,'Self-roles are disabled. An administrator can enable them in /setup.')
        if not self.choices(i.guild_id,category):
            return await private(i,'No '+category+' roles are configured. An administrator can select existing roles with /role selfrole.')
        await private(i,'Choose your '+category+' roles:',view=RoleMenu(self,i.guild_id,category))


def install(bot,con,get,put):
    service=SelfRoles(bot,con,get,put)
    for name,category in [('colour','colour'),('region','region'),('notifications','notifications')]:
        bot.tree.remove_command(name)
        def make(category):
            async def callback(i:discord.Interaction):await service.show(i,category)
            return callback
        bot.tree.add_command(app_commands.Command(name=name,description='Choose your configured '+category+' roles',callback=make(category)))
    group=app_commands.Group(name='role',description='Configure existing self-roles and automatic role assignment')
    bot.tree.add_command(group)

    @group.command(name='selfrole',description='Add or remove an existing role from a member selection menu')
    @app_commands.choices(category=[app_commands.Choice(name=x,value=x) for x in ('colour','region','notifications')])
    async def selfrole(i:discord.Interaction,category:app_commands.Choice[str],label:str,role:discord.Role,active:bool=True):
        if not administrator(i.user):return await private(i,'Server Administrator permission is required.')
        if not 1<=len(label.strip())<=60 or any(x in label for x in ('%', '_', ':')):
            return await private(i,'Use a label of 1–60 characters without colons, percent signs or underscores.')
        if active and not safe_reward_role(role, i.guild.me):
            return await private(i,'Choose a non-managed role below the bot without moderation or administrative permissions.')
        choices = service.choices(i.guild_id,category.value)
        if active and any(existing_role == role.id and existing_label != label.strip() for existing_label, existing_role in choices):
            return await private(i,'That role is already a choice in this menu. Edit its existing label rather than adding a duplicate.')
        if active and len(choices)>=25 and label.strip() not in {name for name, _ in choices}:
            return await private(i,'Each self-role menu supports up to 25 choices.')
        key='selfrole:'+category.value+':'+label.strip()
        if active:put(i.guild_id,key,role.id)
        else:con.execute('DELETE FROM settings WHERE guild=? AND key=?',(i.guild_id,key));con.commit()
        await private(i,'Self-role choice saved for this server.')

    @group.command(name='menu',description='Publish one of this server’s self-role menus')
    @app_commands.choices(category=[app_commands.Choice(name=x,value=x) for x in ('colour','region','notifications')])
    async def menu(i:discord.Interaction,category:app_commands.Choice[str],channel:discord.TextChannel):
        if not administrator(i.user):return await private(i,'Server Administrator permission is required.')
        if not service.choices(i.guild_id,category.value):return await private(i,'Configure choices with /role selfrole first.')
        await channel.send('Choose your '+category.value+' roles:',view=RoleMenu(service,i.guild_id,category.value),allowed_mentions=discord.AllowedMentions.none())
        await private(i,'Role menu published.')

    @group.command(name='autorole',description='Select an existing role assigned to new human members')
    async def autorole(i:discord.Interaction,role:discord.Role,active:bool=True):
        if not administrator(i.user):return await private(i,'Server Administrator permission is required.')
        if active and not safe_reward_role(role, i.guild.me):
            return await private(i,'Choose an assignable role without elevated administration permissions.')
        put(i.guild_id,'role:autorole',role.id if active else 0);await private(i,'Autorole setting saved.')

    async def ready():
        if getattr(service,'registered',False):return
        service.registered=True
        for category in ('colour','region','notifications'):bot.add_view(RoleMenu(service,0,category,True))
    bot.add_listener(ready,'on_ready')
    return service
