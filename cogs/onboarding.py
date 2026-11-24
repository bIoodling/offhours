"""Native Discord Community/onboarding editor and persistent role-menu fallback.

Only existing Discord objects are selected. Draft changes never mutate Discord;
the administrator explicitly applies them from a reviewed panel.
"""
import contextlib
import functools
import json
import time

import discord
from discord import app_commands


async def private(i,text=None,**kwargs):
    kwargs.setdefault('ephemeral',True);kwargs.setdefault('allowed_mentions',discord.AllowedMentions.none())
    if text is not None:text=str(text)[:1900]
    if i.response.is_done():await i.followup.send(text,**kwargs)
    else:await i.response.send_message(text,**kwargs)


def permitted(i):
    return i.guild and isinstance(i.user,discord.Member) and i.user.guild_permissions.manage_guild


def safe_role(guild,role):
    if not role or role.managed or role.is_default() or role>=guild.me.top_role:return False
    perms=role.permissions
    return not any((perms.administrator,perms.manage_guild,perms.manage_roles,perms.manage_channels,
                    perms.manage_webhooks,perms.ban_members,perms.kick_members,perms.moderate_members))


class OnboardingService:
    def __init__(self,bot,connection,get_setting,put_setting):
        self.bot,self.db,self.get,self.put=bot,connection,get_setting,put_setting
        connection.execute('CREATE TABLE IF NOT EXISTS onboarding_selections(guild INTEGER,user INTEGER,prompt INTEGER,roles TEXT,PRIMARY KEY(guild,user,prompt))')
        connection.commit()
        self.registered=set()

    def draft(self,guild):
        raw=self.get(guild.id,'onboarding:draft','')
        if raw:
            try:return json.loads(raw)
            except (ValueError,TypeError):pass
        return {'enabled':False,'mode':0,'default_channels':[],'prompts':[],
                'rules_channel':guild.rules_channel.id if guild.rules_channel else 0,
                'updates_channel':guild.public_updates_channel.id if guild.public_updates_channel else 0,
                'verification_level':guild.verification_level.value,'community':'COMMUNITY' in guild.features}

    def save(self,guild,data):self.put(guild.id,'onboarding:draft',json.dumps(data))

    async def load_current(self,guild):
        current=await guild.onboarding()
        data=self.draft(guild)
        data.update(enabled=current.enabled,mode=current.mode.value,default_channels=sorted(current.default_channel_ids),
            prompts=[{'title':p.title,'single_select':p.single_select,'required':p.required,'in_onboarding':p.in_onboarding,
                      'type':p.type.value,'options':[{'title':o.title,'description':o.description or '',
                      'roles':sorted(o.role_ids),'channels':sorted(o.channel_ids)} for o in p.options]} for p in current.prompts])
        data['rules_channel']=guild.rules_channel.id if guild.rules_channel else 0
        data['updates_channel']=guild.public_updates_channel.id if guild.public_updates_channel else 0
        data['verification_level']=guild.verification_level.value
        data['community']='COMMUNITY' in guild.features
        self.save(guild,data)
        return data

    def preview(self,guild):
        data=self.draft(guild)
        embed=discord.Embed(title=guild.name+' / onboarding draft',colour=0xD4D4D4,
            description='Draft saved for this server. Discord changes occur only when you select **Apply reviewed draft**.')
        embed.add_field(name='Native onboarding',value='Enabled' if data['enabled'] else 'Disabled')
        embed.add_field(name='Community',value='Enabled' if data.get('community') else 'Disabled')
        embed.add_field(name='Verification level',value=str(data.get('verification_level',0)))
        embed.add_field(name='Rules / updates',value=f'<#{data.get("rules_channel",0)}> / <#{data.get("updates_channel",0)}>',inline=False)
        channels=' '.join(f'<#{c}>' for c in data['default_channels'])
        embed.add_field(name='Default channels',value=channels[:1024] or 'None selected',inline=False)
        for index,prompt in enumerate(data['prompts'][:5]):
            options='\n'.join(f'{n+1}. {o["title"]}: '+(', '.join(f'<@&{r}>' for r in o.get('roles',[])) or 'no roles') for n,o in enumerate(prompt['options']))
            embed.add_field(name=f'{index+1}. {prompt["title"]}'[:256],value=options[:1024] or 'No options',inline=False)
        if len(data['prompts'])>5:embed.add_field(name='Additional prompts',value=str(len(data['prompts'])-5))
        embed.set_footer(text='Server Guide requires Discord Server Settings → Onboarding → Server Guide. No icon or layout changes.')
        return embed

    def validate(self,guild,data):
        if len(data['prompts'])>10:raise ValueError('Use at most ten onboarding prompts.')
        channels=[]
        for identifier in data['default_channels']:
            channel=guild.get_channel(int(identifier))
            if not isinstance(channel,discord.TextChannel):raise ValueError('Every default channel must be an existing text channel in this server.')
            channels.append(channel)
        prompts=[]
        for p in data['prompts']:
            if not 1<=len(p['title'])<=100 or not 1<=len(p['options'])<=50:raise ValueError('Every prompt requires a title and 1–50 options.')
            options=[]
            for o in p['options']:
                if not 1<=len(o['title'])<=100:raise ValueError('Every option needs a title of at most 100 characters.')
                roles=[guild.get_role(int(r)) for r in o.get('roles',[])]
                if any(not safe_role(guild,r) for r in roles):raise ValueError('An option references an unsafe, missing or unmanageable role. Choose existing ordinary roles below the bot.')
                linked=[guild.get_channel(int(c)) for c in o.get('channels',[])]
                if any(not channel for channel in linked):raise ValueError('An option references a channel outside this server.')
                options.append(discord.OnboardingPromptOption(title=o['title'],description=o.get('description') or None,roles=roles,channels=linked))
            prompts.append(discord.OnboardingPrompt(type=discord.OnboardingPromptType(p.get('type',0)),title=p['title'],options=options,
                single_select=p.get('single_select',True),required=p.get('required',False),in_onboarding=p.get('in_onboarding',True)))
        if data['enabled'] and data.get('mode',0)==0:
            writable=sum(c.permissions_for(guild.default_role).view_channel and c.permissions_for(guild.default_role).send_messages for c in channels)
            if len(channels)<7 or writable<5:
                raise ValueError('Discord default-mode onboarding requires at least seven default channels, with at least five writable by @everyone. Select existing suitable channels or use the bot role-menu alternative; Offhours will not create channels or change permissions automatically.')
        return channels,prompts

    async def apply(self,i):
        if not permitted(i):return await private(i,'Manage Server permission is required.')
        if not i.guild.me.guild_permissions.manage_guild or not i.guild.me.guild_permissions.manage_roles:
            return await private(i,'The bot requires Manage Server and Manage Roles to apply native onboarding.')
        data=self.draft(i.guild)
        try:channels,prompts=self.validate(i.guild,data)
        except ValueError as exc:return await private(i,str(exc))
        await i.response.defer(ephemeral=True,thinking=True)
        community_applied=False
        try:
            rules=i.guild.get_channel(int(data.get('rules_channel',0)))
            updates=i.guild.get_channel(int(data.get('updates_channel',0)))
            if data.get('community') and (not isinstance(rules,discord.TextChannel) or not isinstance(updates,discord.TextChannel)):
                return await private(i,'Select existing rules and Community updates text channels before enabling Community.')
            kwargs={'verification_level':discord.VerificationLevel(data.get('verification_level',0)),
                    'reason':f'Offhours reviewed Community draft by {i.user.id}'}
            if rules:kwargs['rules_channel']=rules
            if updates:kwargs['public_updates_channel']=updates
            if data.get('community') and 'COMMUNITY' not in i.guild.features:
                kwargs.update(community=True,explicit_content_filter=discord.ContentFilter.all_members)
                if data.get('verification_level',0)<1:kwargs['verification_level']=discord.VerificationLevel.low
            # Turning Community off is never an implicit side effect of an old draft.
            await i.guild.edit(**kwargs)
            community_applied=True
            await i.guild.edit_onboarding(prompts=prompts,default_channels=channels,enabled=data['enabled'],
                mode=discord.OnboardingMode(data.get('mode',0)),reason=f'Offhours reviewed onboarding draft by {i.user.id}')
            current=await i.guild.onboarding()
            verified=current.enabled==data['enabled'] and set(current.default_channel_ids)==set(data['default_channels']) and len(current.prompts)==len(prompts)
            self.put(i.guild_id,'onboarding:last_verified',str(int(time.time())) if verified else '0')
            await private(i,'Community settings were accepted by Discord. Native onboarding '+('was read back and matched the enabled state, default channels and prompt count.' if verified else 'was accepted, but readback did not match every checked field; inspect /onboarding status.'))
        except discord.HTTPException as exc:
            message='Community settings were applied; native onboarding was not confirmed.' if community_applied else 'Discord did not confirm applying this draft.'
            await private(i,f'{message} HTTP {exc.status}. Check Community eligibility and channel requirements. You can publish the working bot role-menu alternative without changing native onboarding.')

    def fallback(self,guild):
        raw=self.get(guild.id,'onboarding:fallback_snapshot','')
        try:return json.loads(raw) if raw else {'prompts':[]}
        except (ValueError,TypeError):return {'prompts':[]}

    def register_fallback(self,guild):
        view=RoleMenu(self,guild,self.fallback(guild),self.get(guild.id,'onboarding:fallback_version','1'))
        if view.children:
            self.bot.add_view(view)
            self.registered.add(guild.id)
        return view

    async def ready(self):
        for guild in self.bot.guilds:
            if self.get(guild.id,'onboarding:fallback_published','0')=='1' and guild.id not in self.registered:
                self.register_fallback(guild)


class BoundView(discord.ui.View):
    def __init__(self,service,guild,author):
        super().__init__(timeout=900)
        self.service,self.guild,self.author=service,guild,author
    async def interaction_check(self,i):
        if i.guild_id!=self.guild.id or i.user.id!=self.author or not permitted(i):
            await private(i,'Only the administrator who opened this editor can use it.');return False
        return True


class Editor(BoundView):
    def __init__(self,service,guild,author):
        super().__init__(service,guild,author)
        defaults=discord.ui.ChannelSelect(placeholder='Select existing default text channels',channel_types=[discord.ChannelType.text,discord.ChannelType.news],min_values=0,max_values=25,row=0)
        async def chosen(i):
            data=service.draft(guild);data['default_channels']=[c.id for c in defaults.values if guild.get_channel(c.id)]
            service.save(guild,data);await i.response.edit_message(embed=service.preview(guild),view=Editor(service,guild,author))
        defaults.callback=chosen;self.add_item(defaults)
        data=service.draft(guild)
        if data['prompts']:
            prompts=discord.ui.Select(placeholder='Edit a prompt and its option roles',row=1,
                options=[discord.SelectOption(label=p['title'][:100],value=str(n)) for n,p in enumerate(data['prompts'][:10])])
            async def selected(i):await i.response.send_message('Choose an option to configure its existing roles:',view=PromptEditor(service,guild,author,int(prompts.values[0])),ephemeral=True)
            prompts.callback=selected;self.add_item(prompts)

    @discord.ui.button(label='Enable / Disable native',row=2)
    async def toggle(self,i,button):
        data=self.service.draft(self.guild);data['enabled']=not data['enabled'];self.service.save(self.guild,data)
        await i.response.edit_message(embed=self.service.preview(self.guild),view=self)

    @discord.ui.button(label='Apply reviewed draft',style=discord.ButtonStyle.success,row=2)
    async def apply(self,i,button):await self.service.apply(i)

    @discord.ui.button(label='Community channels',row=2)
    async def channels(self,i,button):await private(i,'Select existing Community channels:',view=CommunityChannels(self.service,self.guild,self.author))

    @discord.ui.button(label='Add prompt',row=3)
    async def prompt(self,i,button):await i.response.send_modal(PromptModal(self.service,self.guild,self.author))

    @discord.ui.button(label='Preview draft',row=3)
    async def preview(self,i,button):await i.response.edit_message(embed=self.service.preview(self.guild),view=Editor(self.service,self.guild,self.author))


class CommunityChannels(BoundView):
    def __init__(self,service,guild,author):
        super().__init__(service,guild,author)
        for index,(key,label) in enumerate([('rules_channel','Rules channel'),('updates_channel','Community updates channel')]):
            picker=discord.ui.ChannelSelect(placeholder=label,channel_types=[discord.ChannelType.text,discord.ChannelType.news],min_values=1,max_values=1,row=index)
            async def chosen(i,key=key,picker=picker):
                data=service.draft(guild);data[key]=picker.values[0].id;service.save(guild,data)
                await private(i,'Saved '+key+' to this server’s draft.')
            picker.callback=chosen;self.add_item(picker)


class PromptModal(discord.ui.Modal):
    def __init__(self,service,guild,author):
        super().__init__(title='Add onboarding prompt')
        self.service,self.guild,self.author=service,guild,author
        self.title_field=discord.ui.TextInput(label='Question',max_length=100)
        self.options=discord.ui.TextInput(label='Options, one per line',style=discord.TextStyle.paragraph,max_length=2000)
        self.multiple=discord.ui.TextInput(label='Allow multiple selections? yes/no',default='no',max_length=3)
        self.required=discord.ui.TextInput(label='Required in native onboarding? yes/no',default='no',max_length=3)
        for item in (self.title_field,self.options,self.multiple,self.required):self.add_item(item)
    async def on_submit(self,i):
        if i.guild_id!=self.guild.id or i.user.id!=self.author or not permitted(i):return await private(i,'This editor is no longer authorized.')
        data=self.service.draft(self.guild);options=[x.strip() for x in self.options.value.splitlines() if x.strip()]
        if len(data['prompts'])>=10 or not 1<=len(options)<=25 or any(len(x)>100 for x in options):return await private(i,'Use up to ten prompts, each with 1–25 options of at most 100 characters.')
        data['prompts'].append({'title':self.title_field.value,'single_select':self.multiple.value.lower()!='yes',
            'required':self.required.value.lower()=='yes','in_onboarding':True,'type':0,
            'options':[{'title':x,'description':'','roles':[],'channels':[]} for x in options]})
        self.service.save(self.guild,data)
        await private(i,'Prompt saved to draft. Select it in /onboarding edit to link existing roles.')


class PromptEditor(BoundView):
    def __init__(self,service,guild,author,index):
        super().__init__(service,guild,author)
        self.index=index
        data=service.draft(guild)
        prompt=data['prompts'][index]
        picker=discord.ui.Select(placeholder='Choose an option',options=[discord.SelectOption(label=o['title'][:100],value=str(n)) for n,o in enumerate(prompt['options'][:25])],row=0)
        async def selected(i):await private(i,'Select existing safe roles for '+prompt['options'][int(picker.values[0])]['title'],
            view=OptionRoles(service,guild,author,index,int(picker.values[0])))
        picker.callback=selected;self.add_item(picker)

    @discord.ui.button(label='Remove this draft prompt',style=discord.ButtonStyle.danger,row=1)
    async def remove(self,i,button):
        data=self.service.draft(self.guild)
        if self.index>=len(data['prompts']):return await private(i,'This draft prompt no longer exists.')
        data['prompts'].pop(self.index);self.service.save(self.guild,data)
        await i.response.edit_message(content='Prompt removed from draft. Native Discord onboarding is unchanged until Apply.',view=None)


class OptionRoles(BoundView):
    def __init__(self,service,guild,author,prompt_index,option_index):
        super().__init__(service,guild,author)
        picker=discord.ui.RoleSelect(placeholder='Select existing ordinary roles',min_values=0,max_values=5)
        async def selected(i):
            roles=[guild.get_role(role.id) for role in picker.values]
            if any(not safe_role(guild,role) for role in roles):return await private(i,'Choose non-managed, non-privileged roles below the bot’s highest role.')
            data=service.draft(guild)
            if prompt_index>=len(data['prompts']) or option_index>=len(data['prompts'][prompt_index]['options']):return await private(i,'This option no longer exists. Reopen the editor.')
            data['prompts'][prompt_index]['options'][option_index]['roles']=[r.id for r in roles];service.save(guild,data)
            await i.response.edit_message(content='Existing option roles saved to this server’s draft.',view=None)
        picker.callback=selected;self.add_item(picker)


class RoleMenu(discord.ui.View):
    def __init__(self,service,guild,snapshot,version):
        super().__init__(timeout=None)
        for index,prompt in enumerate(snapshot['prompts'][:5]):
            valid=[(n,o) for n,o in enumerate(prompt['options']) if o.get('roles')]
            if not valid:continue
            options=[discord.SelectOption(label=o['title'][:100],value=str(n),description=(o.get('description') or '')[:100] or None) for n,o in valid[:25]]
            picker=discord.ui.Select(placeholder=prompt['title'][:150],options=options,min_values=0,
                max_values=1 if prompt.get('single_select',True) else len(options),row=index,custom_id=f'offhours:onboarding:v1:{guild.id}:{version}:{index}')
            async def selected(i,index=index,picker=picker):
                if i.guild_id!=guild.id or not isinstance(i.user,discord.Member):return await private(i,'Use this role menu in its own server.')
                if version!=service.get(guild.id,'onboarding:fallback_version','1'):return await private(i,'This role menu was replaced. Use the most recently published role menu.')
                if service.get(guild.id,'module:selfroles','0')!='1':return await private(i,'Self-role selection is disabled in this server.')
                current=service.fallback(guild)['prompts']
                if index>=len(current):return await private(i,'This prompt was changed. Ask an administrator to republish the role menu.')
                question=current[index]
                requested=set()
                try:
                    for raw in picker.values:
                        requested.update(question['options'][int(raw)].get('roles',[]))
                except (IndexError,ValueError):return await private(i,'This option is no longer available. Reopen the current menu.')
                desired=[guild.get_role(int(r)) for r in requested]
                if any(not safe_role(guild,role) for role in desired):return await private(i,'One selected role is unavailable or unsafe. Ask an administrator to update the menu.')
                allowed={int(r) for option in question['options'] for r in option.get('roles',[])}
                old=[r for r in i.user.roles if r.id in allowed and r.id not in requested and safe_role(guild,r)]
                await i.response.defer(ephemeral=True)
                try:
                    if old:await i.user.remove_roles(*old,reason='Updated onboarding role-menu selection')
                    new=[r for r in desired if r not in i.user.roles]
                    if new:await i.user.add_roles(*new,reason='Onboarding role-menu selection')
                    service.db.execute('INSERT OR REPLACE INTO onboarding_selections VALUES(?,?,?,?)',(guild.id,i.user.id,index,json.dumps(sorted(requested))));service.db.commit()
                    await private(i,'Your selections were updated. You can change them here later.')
                except discord.HTTPException as exc:await private(i,f'Discord denied a role change (HTTP {exc.status}). Your selection was not confirmed.')
            picker.callback=selected;self.add_item(picker)


def install(bot,connection,get_setting,put_setting):
    service=OnboardingService(bot,connection,get_setting,put_setting)
    group=app_commands.Group(name='onboarding',description='Configure native onboarding and existing-role menus',guild_only=True)
    bot.tree.remove_command('onboarding');bot.tree.add_command(group)
    def register(name,description):
        def decorator(fn):
            @functools.wraps(fn)
            async def callback(i,*args,**kwargs):
                if not permitted(i):return await private(i,'Manage Server permission is required.')
                try:await fn(i,*args,**kwargs)
                except ValueError as exc:await private(i,str(exc))
                except discord.HTTPException as exc:await private(i,f'Discord rejected this request (HTTP {exc.status}). No success was assumed.')
            group.add_command(app_commands.Command(name=name,description=description,callback=callback))
            return fn
        return decorator

    @register('edit','Open the private native onboarding draft editor')
    async def edit(i:discord.Interaction):
        if not get_setting(i.guild_id,'onboarding:draft',''):
            await i.response.defer(ephemeral=True)
            await service.load_current(i.guild)
        await private(i,embed=service.preview(i.guild),view=Editor(service,i.guild,i.user.id))

    @register('status','Read actual Discord Community and native onboarding state')
    async def status(i:discord.Interaction):
        await i.response.defer(ephemeral=True)
        current=await i.guild.onboarding()
        await private(i,f'Discord Community: {"enabled" if "COMMUNITY" in i.guild.features else "disabled"}\nNative onboarding: {"enabled" if current.enabled else "disabled"}\nDefault channels: {len(current.default_channel_ids)}\nNative prompts: {len(current.prompts)}\nRules: {i.guild.rules_channel.mention if i.guild.rules_channel else "not selected"}\nUpdates: {i.guild.public_updates_channel.mention if i.guild.public_updates_channel else "not selected"}\nVerification: {i.guild.verification_level.name}\nServer Guide: not configured or verified by this bot; use Discord Server Settings → Onboarding → Server Guide.')

    @register('load-current','Replace the saved draft with current native Discord settings')
    async def load_current(i:discord.Interaction):
        await i.response.defer(ephemeral=True);await service.load_current(i.guild)
        await private(i,'Current native settings loaded into the saved draft.',embed=service.preview(i.guild),view=Editor(service,i.guild,i.user.id))

    @register('template','Save colour, region and notification question templates to the draft')
    async def template(i:discord.Interaction):
        data=service.draft(i.guild)
        questions=[('Choose your colour',['Silver','Slate','Blue','Sage','Rose','Red'],True),
                   ('Where are you from?',['Europe','North America','South America','Asia','Africa','Oceania'],True),
                   ('What notifications would you like?',['Live notifications','Video notifications','Announcements','Giveaways'],False)]
        existing={p['title'] for p in data['prompts']}
        for title,labels,single in questions:
            if title in existing:continue
            data['prompts'].append({'title':title,'single_select':single,'required':False,'in_onboarding':True,'type':0,
                                   'options':[{'title':label,'description':'','roles':[],'channels':[]} for label in labels]})
        if len(data['prompts'])>10:raise ValueError('This would exceed ten draft prompts. Remove an unused prompt first.')
        service.save(i.guild,data)
        await private(i,'Question templates saved without creating roles. Use /onboarding edit to select existing roles for each answer.',embed=service.preview(i.guild))

    @register('community','Save Community enablement and verification settings to the draft')
    async def community(i:discord.Interaction,enabled:bool,verification_level:app_commands.Range[int,0,4]=1):
        data=service.draft(i.guild)
        if not enabled and 'COMMUNITY' in i.guild.features:raise ValueError('Disabling Community is intentionally manual: Discord Server Settings → Community → Overview → Disable Community. Existing channels and roles will not be changed by Offhours.')
        data['community']=enabled;data['verification_level']=verification_level;service.save(i.guild,data)
        await private(i,'Community draft saved. Select existing rules/updates channels in /onboarding edit, review, then Apply.')

    @register('publish-roles','Publish persistent existing-role menus as a native onboarding alternative')
    async def publish_roles(i:discord.Interaction,channel:discord.TextChannel):
        data=service.draft(i.guild)
        service.validate(i.guild,{**data,'enabled':False})
        version=str(time.time_ns())
        view=RoleMenu(service,i.guild,data,version)
        if not view.children:raise ValueError('Link existing safe roles to at least one draft prompt option before publishing.')
        if not i.guild.me.guild_permissions.manage_roles:raise ValueError('The bot needs Manage Roles to apply member selections.')
        await i.response.defer(ephemeral=True)
        message=await channel.send('Choose or update your server roles below. Clear a selection to remove its optional roles.',
            view=view,allowed_mentions=discord.AllowedMentions.none(),silent=True)
        put_setting(i.guild_id,'onboarding:fallback_snapshot',json.dumps(data));put_setting(i.guild_id,'onboarding:fallback_version',version)
        service.registered.add(i.guild_id)
        put_setting(i.guild_id,'onboarding:fallback_published','1');put_setting(i.guild_id,'onboarding:fallback_message',str(message.id))
        put_setting(i.guild_id,'onboarding:fallback_channel',str(channel.id));put_setting(i.guild_id,'module:selfroles','1')
        await private(i,'Persistent role menu published. Native onboarding was not changed.')

    @register('guide','Show the exact manual Server Guide action and current working alternative')
    async def guide(i:discord.Interaction):
        channel=get_setting(i.guild_id,'onboarding:fallback_channel','')
        await private(i,'Server Guide editing is not exposed by the supported bot API used here. In Discord, open Server Settings → Onboarding → Server Guide, add welcome text, resource pages and new-member tasks, then publish. '
            +'For a bot-based alternative, /onboarding template → /onboarding edit links existing roles, and /onboarding publish-roles publishes persistent menus members can update later.'
            +(f' Current role-menu channel: <#{channel}>.' if channel.isdecimal() else ''))

    bot.add_listener(service.ready,'on_ready')
    return service
