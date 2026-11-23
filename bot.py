import os, asyncio, logging, sqlite3, time, re, random, math, io, datetime
from pathlib import Path
import aiohttp
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN=os.getenv('DISCORD_TOKEN','')
GUILD_ID=int(os.getenv('GUILD_ID','0'))
OWNER_ID=int(os.getenv('OWNER_ID','0'))
TWITCH_CLIENT_ID=os.getenv('TWITCH_CLIENT_ID','')
TWITCH_CLIENT_SECRET=os.getenv('TWITCH_CLIENT_SECRET','')
TWITCH_LOGIN=os.getenv('TWITCH_LOGIN','hikamunii')
CUSTOM_XP=True  # offhours handles levels; disable Maki XP to avoid duplicates
WELCOME_BANNER_URL=os.getenv('WELCOME_BANNER_URL','')
logging.basicConfig(level=logging.INFO)
log=logging.getLogger('offhours')
DB=Path(os.getenv('OFFHOURS_DB_PATH', str(Path(__file__).with_name('offhours.sqlite3'))))
DB.parent.mkdir(parents=True, exist_ok=True)
con=sqlite3.connect(DB)
con.execute('CREATE TABLE IF NOT EXISTS xp (guild INTEGER, user INTEGER, xp INTEGER DEFAULT 0, last REAL DEFAULT 0, PRIMARY KEY(guild,user))')
con.execute('CREATE TABLE IF NOT EXISTS warnings (id INTEGER PRIMARY KEY AUTOINCREMENT, guild INTEGER, user INTEGER, moderator INTEGER, reason TEXT, timestamp INTEGER)')
con.execute('CREATE TABLE IF NOT EXISTS colours (guild INTEGER, user INTEGER, role TEXT, PRIMARY KEY(guild,user))')
con.execute('CREATE TABLE IF NOT EXISTS settings (guild INTEGER, key TEXT, value TEXT, PRIMARY KEY(guild,key))')
con.commit()

def setting(g,k,default=''):
    row=con.execute('SELECT value FROM settings WHERE guild=? AND key=?',(g,k)).fetchone()
    return row[0] if row else default

def set_setting(g,k,v):
    con.execute('INSERT OR REPLACE INTO settings VALUES (?,?,?)',(g,k,str(v))); con.commit()

intents=discord.Intents.default()
intents.members=True
intents.message_content=True
bot=commands.Bot(command_prefix='?', intents=intents, help_command=None, allowed_mentions=discord.AllowedMentions(everyone=False,roles=False))

CATEGORIES=[
 ('01  INFORMATION',['welcome','rules','verify','announcements']),
 ('02  HIKA',['my-setup','my-gear','live-notifications']),
 ('03  COMMUNITY',['general','media','post-your-setup','clips']),
 ('04  VOICE',['general','gaming','afk']),
 ('05  STAFF',['staff-chat','mod-logs','reports'])]
COLOURS={'slate':0x8996A6,'silver':0xC6C8CB,'blue':0x829CB7,'lavender':0xA594C1,'sage':0x8BA99C,'rose':0xBA929D}
ROLE_SPECS=[('owner',0xF5F5F5),('admin',0x9DAEC2),('moderator',0x8DAFA6),('bots',0x8A92A8),('booster',0xC0A0B4),('regular',0x9BA7B4),('pending',0x777F8B)] + [(f'level {i}',[0xE5E5E5,0x6FADCE,0x88BEA3,0xD3AA7A,0xD48FA8,0xC7A1EB][((i//5)-1)%6]) for i in range(5,151,5)] + [(f'colour · {k}',v) for k,v in COLOURS.items()]
LEVELS=[(i,f'level {i}') for i in range(5,151,5)]
# XP = 30 * level^2; 750 XP to level 5 (~1-3 hours of active conversation).
def level(xp): return min(150,math.isqrt(max(0,xp)//30))
def xp_for_level(lvl): return 30*lvl*lvl
last_messages={}
link_re=re.compile(r'(https?://|www\.|discord\.gg/|tenor\.com/|giphy\.com/)',re.I)
def owner_only(ctx): return ctx.guild is not None and ctx.author.id==OWNER_ID and ctx.guild.id==GUILD_ID

def staff_only():
    async def predicate(ctx):
        return ctx.guild is not None and (ctx.author.id==OWNER_ID or ctx.author.guild_permissions.manage_messages)
    return commands.check(predicate)

async def log_action(guild, msg):
    channel=discord.utils.get(guild.text_channels,name='mod-logs')
    if channel:
        try: await channel.send(msg,allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException: pass

class VerifyView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label='Verify',style=discord.ButtonStyle.success,custom_id='offhours:verify:v1')
    async def verify(self,interaction:discord.Interaction,button:discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user,discord.Member): return
        member=discord.utils.get(interaction.guild.roles,name='regular')
        unverified=discord.utils.get(interaction.guild.roles,name='pending')
        if not member: return await interaction.response.send_message('Verification is not configured yet.',ephemeral=True)
        try:
            await interaction.user.add_roles(member,reason='Verified via button')
            if unverified and unverified in interaction.user.roles: await interaction.user.remove_roles(unverified,reason='Verified via button')
            await interaction.response.send_message('verified. welcome to offhours.',ephemeral=True)
            await log_action(interaction.guild,f'✓ verified: {interaction.user.mention}')
        except discord.Forbidden:
            await interaction.response.send_message('I cannot assign the member role. Move my bot role above member in Server Settings.',ephemeral=True)

class ColourSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder='choose a role colour',min_values=1,max_values=1,
                         options=[discord.SelectOption(label=k,value=k) for k in COLOURS],custom_id='offhours:colour:select')
    async def callback(self,interaction):
        if not isinstance(interaction.user,discord.Member): return
        member=interaction.user; g=interaction.guild
        if not discord.utils.get(member.roles,name='regular'):
            return await interaction.response.send_message('verify first.',ephemeral=True)
        if member_level(g.id,member.id)>=5:
            return await interaction.response.send_message('level rewards replace starter colours after level 5.',ephemeral=True)
        for r in member.roles:
            if r.name.startswith('colour · '): await member.remove_roles(r,reason='Changed colour')
        chosen=discord.utils.get(g.roles,name='colour · '+self.values[0])
        if chosen: await member.add_roles(chosen,reason='Selected starter colour')
        con.execute('INSERT OR REPLACE INTO colours VALUES (?,?,?)',(g.id,member.id,self.values[0]));con.commit()
        await interaction.response.send_message('colour set to '+self.values[0]+'.',ephemeral=True)
class ColourView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None);self.add_item(ColourSelect())
def member_xp(g,u):
    row=con.execute('SELECT xp FROM xp WHERE guild=? AND user=?',(g,u)).fetchone()
    return row[0] if row else 0
def member_level(g,u): return level(member_xp(g,u))
async def level_panel(member,lvl):
    ch=discord.utils.get(member.guild.text_channels,name='welcome')
    if not ch:return
    em=discord.Embed(title='level up  /  '+str(lvl),description=f'{member.mention} reached **level {lvl}**.',colour=0xE5E5E5)
    em.set_thumbnail(url=member.display_avatar.url)
    await ch.send(embed=em,allowed_mentions=discord.AllowedMentions(users=[member]),silent=True)
async def join_panel(member,test=False):
    ch=discord.utils.get(member.guild.text_channels,name='welcome')
    if not ch:return
    rules=discord.utils.get(member.guild.text_channels,name='rules')
    destination=rules.mention if rules else '#rules'
    em=discord.Embed(title='join notifier  /  test' if test else 'welcome to offhours',description=f'{member.mention}  ·  member #{member.guild.member_count}\n\nstart in {destination}',colour=0xE5E5E5)
    em.set_thumbnail(url=member.display_avatar.url)
    if WELCOME_BANNER_URL.startswith('https://'):em.set_image(url=WELCOME_BANNER_URL)
    await ch.send(embed=em,allowed_mentions=discord.AllowedMentions(users=[member]))
@bot.event
async def on_ready():
    if not getattr(bot,'_initialized',False):
        bot.add_view(VerifyView()); bot.add_view(ColourView()); bot.add_view(RegionView()); bot._initialized=True
        if TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET: twitch_check.start()
        try: await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        except discord.HTTPException: log.exception('Slash command sync failed')
        guild=bot.get_guild(GUILD_ID)
        if guild:
            # Startup must not post test welcomes, audits or owner pings.
    log.info('Ready as %s',bot.user)

@bot.event
async def on_member_join(member):
    if member.bot: return
    role=discord.utils.get(member.guild.roles,name='pending')
    if role:
        try: await member.add_roles(role,reason='New member awaiting verification')
        except discord.Forbidden: log.warning('Cannot assign unverified role')
    await join_panel(member)  # Maki welcome messages should be disabled to prevent duplicates.

@bot.event
async def on_message(message):
    if message.guild and not message.author.bot and message.guild.id==GUILD_ID and isinstance(message.author,discord.Member):
        member=message.author; g=message.guild; now=time.time(); lvl=member_level(g.id,member.id)
        if message.content.startswith('?'):
            await bot.process_commands(message);return
        if discord.utils.get(member.roles,name='regular') and not member.guild_permissions.manage_messages:
            if message.channel.name=='general' and lvl<20 and (link_re.search(message.content) or message.attachments):
                try: await message.delete(); await member.send('links and GIF attachments in #general unlock at level 20.')
                except discord.HTTPException: pass
                return
            # Basic flood control; Discord slowmode is separately configured.
            key=(g.id,member.id,message.channel.id)
            prior=last_messages.get(key)
            if prior and now-prior[0]<2.5 and prior[1]==message.content:
                try: await message.delete()
                except discord.HTTPException: pass
                return
            last_messages[key]=(now,message.content)
        if message.channel.name=='post-your-setup' and message.attachments:
            for emoji in ('🔥','❤️','💀','❌'):
                try: await message.add_reaction(emoji)
                except discord.HTTPException: pass
        if discord.utils.get(member.roles,name='regular') and len(message.content.strip())>=8:
            row=con.execute('SELECT xp,last FROM xp WHERE guild=? AND user=?',(g.id,member.id)).fetchone()
            prev_xp,prev_last=row if row else (0,0)
            if now-prev_last>=45 and lvl<150:
                gained=random.randint(12,18);new_xp=prev_xp+gained;new_lvl=level(new_xp)
                con.execute('INSERT OR REPLACE INTO xp VALUES (?,?,?,?)',(g.id,member.id,new_xp,now));con.commit()
                if new_lvl>lvl:
                    for threshold,name in LEVELS:
                        if lvl<threshold<=new_lvl:
                            role=discord.utils.get(g.roles,name=name)
                            if role:
                                try: await member.add_roles(role,reason=f'Level {threshold} reward')
                                except discord.Forbidden: pass
                    if lvl<5<=new_lvl:
                        for role in member.roles:
                            if role.name.startswith('colour · '):
                                try: await member.remove_roles(role,reason='Level 5 replaces starter colour')
                                except discord.Forbidden: pass
                    try: await level_panel(member,new_lvl)
                    except discord.HTTPException: pass
    await bot.process_commands(message)

@bot.event
async def on_raw_reaction_add(payload):
    if payload.guild_id!=GUILD_ID or payload.user_id==bot.user.id:return
    guild=bot.get_guild(payload.guild_id)
    if not guild:return
    member=payload.member or guild.get_member(payload.user_id)
    if not member or member.bot or member.guild_permissions.manage_messages:return
    channel=guild.get_channel(payload.channel_id)
    if not channel or channel.name not in ('general','media','clips'):return
    if member_level(guild.id,member.id)>=10:return
    try:
        msg=await channel.fetch_message(payload.message_id)
        await msg.remove_reaction(payload.emoji,member)
    except (discord.HTTPException,discord.Forbidden):pass

async def ensure_role(guild,name,color):
    r=discord.utils.get(guild.roles,name=name)
    if not r: r=await guild.create_role(name=name,colour=discord.Colour(color),hoist=(name in ('owner','admin','moderator')),reason='offhours setup')
    return r


async def update_server_icon(guild):
    """Generate a solid-background dark offhours icon locally and apply it."""
    from io import BytesIO
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageFilter
    except ImportError:
        log.warning('Install Pillow to enable automatic server icon updates.')
        return False
    size=512
    im=Image.new('RGB',(size,size),(13,13,18))
    d=ImageDraw.Draw(im)
    # Restrained charcoal/lavender emblem with solid background.
    for i in range(240,0,-1):
        shade=int(15+10*(1-i/240))
        d.ellipse((256-i,256-i,256+i,256+i),outline=(shade,shade,shade+5),width=2)
    d.rounded_rectangle((64,64,448,448),radius=98,outline=(105,93,133),width=8)
    d.arc((120,110,390,390),42,314,fill=(168,149,198),width=17)
    d.arc((146,124,402,378),48,304,fill=(31,29,40),width=23)
    d.ellipse((355,133,369,147),fill=(207,196,220))
    d.line((362,121,362,159),fill=(207,196,220),width=3)
    d.line((343,140,381,140),fill=(207,196,220),width=3)
    fonts=['/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf','C:/Windows/Fonts/arialbd.ttf']
    font=next((ImageFont.truetype(p,78) for p in fonts if os.path.exists(p)),ImageFont.load_default())
    text='offhours'
    box=d.textbbox((0,0),text,font=font)
    w=box[2]-box[0]
    # Use a short readable monogram instead of microscopic full lettering.
    mono='off'
    font=next((ImageFont.truetype(p,116) for p in fonts if os.path.exists(p)),ImageFont.load_default())
    b=d.textbbox((0,0),mono,font=font)
    d.text(((size-(b[2]-b[0]))/2,216),mono,font=font,fill=(225,219,232),stroke_width=1)
    out=BytesIO(); im.save(out,format='PNG',optimize=True)
    try:
        await guild.edit(icon=out.getvalue(),reason='offhours owner-requested icon refresh')
        return True
    except (discord.Forbidden,discord.HTTPException):
        log.exception('Could not update server icon; check Manage Server permission')
        return False

@bot.command(name='setup')
@commands.guild_only()
async def setup(ctx):
    if not owner_only(ctx): return await ctx.reply('Only the configured owner can run this.')
    guild=ctx.guild
    await ctx.reply('Setting up offhours. This can take a minute…')
    # Preserve all existing channel IDs and category names; only a few information channel names get subtle symbols.
    # Migrate only channels created by earlier offhours versions, preserving IDs and history.
    renames = {
        '✦・welcome':'welcome', '✓・verify':'verify', '◈・announcements':'announcements',
        '⌘・my-setup':'my-setup', '◇・my-gear':'my-gear',
        '◉・live-notifications':'live-notifications', '▣・post-your-setup':'post-your-setup',
    }
    for old_name, new_name in {}.items():
        old_channel = discord.utils.get(guild.text_channels, name=old_name)
        if old_channel:
            existing = discord.utils.get(guild.text_channels, name=new_name)
            if not existing:
                await old_channel.edit(name=new_name, reason='offhours minimal channel names')
    # Only delete the old commands channel in the community category.
    old_commands = discord.utils.get(guild.text_channels, name='commands')
    if False and old_commands and old_commands.category and 'COMMUNITY' in old_commands.category.name:
        await old_commands.delete(reason='offhours simplified layout')
    roles={}
    for name,color in ROLE_SPECS: roles[name]=await ensure_role(guild,name,color)
    for region in ('europe','north america','south america','asia','africa','oceania'):
        await ensure_role(guild,'region · '+region,0x8898A8)
    # Role ordering is constrained by Discord's role hierarchy. Adjust owner/bot ordering manually after setup.
    await guild.me.add_roles(roles['bots'],reason='offhours bot grouping')
    owner=guild.get_member(OWNER_ID)
    if owner and roles['owner'] not in owner.roles:
        try: await owner.add_roles(roles['owner'],reason='offhours owner')
        except discord.Forbidden: await ctx.send('Move my bot role above owner temporarily to assign it, or assign owner manually.')
    everyone=guild.default_role
    member=roles['regular']; unverified=roles['pending']
    staff={roles['owner'],roles['admin'],roles['moderator']}
    # STRICT EXISTING-CHANNEL MODE: never create, rename, delete, move or duplicate channels/categories.
    # Resolve the exact existing category and only channels already inside that category.
    # Do not search globally by channel name: e.g. #general exists as both text and voice.
    existing_categories = {c.name: c for c in guild.categories}
    skipped = []
    for cat_name,names in CATEGORIES:
        cat = existing_categories.get(cat_name)
        if cat is None:
            skipped.append(f'category {cat_name}')
            continue
        is_staff='STAFF' in cat_name
        if is_staff:
            await cat.set_permissions(everyone,view_channel=False)
            for r in staff: await cat.set_permissions(r,view_channel=True,send_messages=True,read_message_history=True)
            await cat.set_permissions(guild.me,view_channel=True,send_messages=True)
        elif 'INFORMATION' in cat_name:
            await cat.set_permissions(everyone,view_channel=True,send_messages=False,read_message_history=True)
            await cat.set_permissions(member,view_channel=True,send_messages=False)
        else:
            await cat.set_permissions(everyone,view_channel=False)
            await cat.set_permissions(member,view_channel=True,send_messages=True,read_message_history=True)
            for r in staff: await cat.set_permissions(r,view_channel=True,send_messages=True)
            await cat.set_permissions(guild.me,view_channel=True,send_messages=True)
        for name in names:
            voice='VOICE' in cat_name
            ch=discord.utils.get(cat.voice_channels if voice else cat.text_channels,name=name)
            if ch is None:
                skipped.append(f'{cat_name} / {name}')
                continue
            if not voice:
                if name in ('my-setup','my-gear','announcements','live-notifications'):
                    await ch.set_permissions(member,view_channel=True,send_messages=False,add_reactions=True)
                    for r in (roles['admin'],roles['moderator']):
                        await ch.set_permissions(r,view_channel=True,send_messages=False)
                    await ch.set_permissions(roles['owner'],view_channel=True,send_messages=True)
                    await ch.set_permissions(guild.me,view_channel=True,send_messages=True)
                if name in ('welcome','rules','verify'):
                    await ch.set_permissions(everyone,view_channel=True,send_messages=False)
                    if name in ('rules','verify'):
                        await ch.set_permissions(member,view_channel=False)
                    else:
                        await ch.set_permissions(member,view_channel=True,send_messages=False)
                if name=='post-your-setup': await ch.edit(slowmode_delay=3600)
                if name=='general': await ch.edit(slowmode_delay=1)
                if name=='media': await ch.edit(slowmode_delay=15)
                if name=='clips': await ch.edit(slowmode_delay=10)
    info_cat=existing_categories.get('01  INFORMATION')
    community_cat=existing_categories.get('03  COMMUNITY')
    welcome=discord.utils.get(info_cat.text_channels,name='welcome') if info_cat else None
    rules=discord.utils.get(info_cat.text_channels,name='rules') if info_cat else None
    verify=discord.utils.get(info_cat.text_channels,name='verify') if info_cat else None
    async def publish_panel(channel, key, *, embed, view=None):
        if not channel:
            return
        # Delete previous bot-authored panels before posting a fresh one.
        # This avoids Discord's visible '(edited)' indicator.
        old_ids = set()
        old_id = setting(guild.id, key)
        if old_id and old_id.isdigit():
            old_ids.add(int(old_id))
        async for msg in channel.history(limit=50):
            if msg.author.id == bot.user.id and (msg.id in old_ids or msg.embeds):
                try:
                    await msg.delete()
                except (discord.Forbidden, discord.HTTPException):
                    pass
        posted = await channel.send(embed=embed, view=view)
        set_setting(guild.id, key, posted.id)

    # Light neutral embed accent and simple horizontal separators; no edited messages.
    accent=discord.Colour(0xE6E6E6)
    line='━━━━━━━━━━━━━━━━━━━━'
    if welcome:
        async for old in welcome.history(limit=40):
            if old.author.id==bot.user.id and old.embeds and old.embeds[0].title and 'welcome to offhours' in old.embeds[0].title.lower():
                try: await old.delete()
                except discord.HTTPException: pass
    if rules:
        rules_embed=discord.Embed(title='rules',description=(
            '**01  respect people**\nno harassment, racism or bullying.\n\n'
            '**02  keep it clean**\nno scams, spam, NSFW or sharing private information.\n\n'
            '**03  follow Discord guidelines**\nserious violations may result in a ban.\n\n'
            f'continue to {verify.mention if verify else "#verify"}'),colour=accent)
        await publish_panel(rules,'rules_post',embed=rules_embed)

    if verify:
        verify_embed=discord.Embed(title='verification',description='press **Verify** to unlock offhours.',colour=accent)
        await publish_panel(verify,'verify_post',embed=verify_embed,view=VerifyView())
    general=discord.utils.get(community_cat.text_channels,name='general') if community_cat else None
    if general:
        old=setting(guild.id,'colour_post')
        if old:
            try: await (await general.fetch_message(int(old))).delete()
            except discord.HTTPException: pass
        colour_msg=await general.send(embed=discord.Embed(title='choose your colour',description='pick a starter colour below. level 5 replaces it with your earned role.',colour=accent),view=ColourView())
        set_setting(guild.id,'colour_post',colour_msg.id)
    for role_name,colour in ROLE_SPECS:
        role=roles[role_name]
        if role.colour.value!=colour:
            try: await role.edit(colour=discord.Colour(colour),reason='offhours updated role palette')
            except discord.Forbidden: log.warning('Cannot recolour role %s',role_name)
    summary='Setup finished: edited existing channels only. No channels or categories created, renamed, moved or deleted.'
    if skipped: summary += '\\nMissing channels/categories (left untouched): ' + ', '.join(skipped[:12])
    await ctx.send(summary)

@bot.command(name='say')
@commands.guild_only()
async def say(ctx, channel: discord.TextChannel, *, message: str):
    """Owner-only message relay, e.g. ?say #announcements Going live tonight."""
    if not owner_only(ctx):
        return
    if len(message)>1900:
        return await ctx.reply('Message is too long (1900 characters max).')
    await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
    try:
        await ctx.message.add_reaction('✅')
    except discord.HTTPException:
        pass
    await log_action(ctx.guild, f'owner relay: {ctx.author.mention} -> {channel.mention}')

@bot.command(name='testwelcome')
@commands.guild_only()
async def testwelcome(ctx):
    if not owner_only(ctx):return
    await join_panel(ctx.author,test=True)
    await ctx.message.add_reaction('✅')

@bot.command(name='purge')
@staff_only()
@commands.guild_only()
async def purge(ctx,amount:int):
    if not 1<=amount<=100: return await ctx.reply('Use `?purge 1` to `?purge 100`.')
    cutoff=discord.utils.utcnow()-datetime.timedelta(days=7)
    deleted=await ctx.channel.purge(limit=amount+1,after=cutoff)
    await log_action(ctx.guild,f'purge: {ctx.author.mention} removed {len(deleted)-1} messages in {ctx.channel.mention}')

@bot.command(name='warn')
@staff_only()
async def warn(ctx,member:discord.Member,*,reason:str='No reason provided'):
    con.execute('INSERT INTO warnings (guild,user,moderator,reason,timestamp) VALUES (?,?,?,?,?)',(ctx.guild.id,member.id,ctx.author.id,reason,int(time.time())));con.commit()
    await ctx.reply(f'Warned {member.mention}: {discord.utils.escape_mentions(reason)}')
    await log_action(ctx.guild,f'warning: {member.mention} by {ctx.author.mention}: {discord.utils.escape_mentions(reason)}')

@bot.command(name='warnings')
@staff_only()
async def warnings(ctx,member:discord.Member):
    rows=con.execute('SELECT id,reason FROM warnings WHERE guild=? AND user=? ORDER BY id DESC LIMIT 10',(ctx.guild.id,member.id)).fetchall()
    await ctx.reply('\n'.join(f'#{i}: {discord.utils.escape_mentions(reason)[:150]}' for i,reason in rows) if rows else 'No warnings.')

@bot.command(name='timeout')
@staff_only()
async def timeout(ctx,member:discord.Member,minutes:int,*,reason:str='No reason provided'):
    if not 1<=minutes<=40320: return await ctx.reply('Choose 1–40320 minutes.')
    await member.timeout(discord.utils.utcnow()+__import__('datetime').timedelta(minutes=minutes),reason=reason)
    await ctx.reply(f'Timed out {member.mention} for {minutes} minutes.')
    await log_action(ctx.guild,f'timeout: {member.mention}, {minutes} min, by {ctx.author.mention}: {discord.utils.escape_mentions(reason)}')

@bot.command(name='kick')
@staff_only()
async def kick(ctx,member:discord.Member,*,reason:str='No reason provided'):
    await member.kick(reason=reason);await ctx.reply(f'Kicked {member}.')
    await log_action(ctx.guild,f'kick: {member}, by {ctx.author.mention}: {discord.utils.escape_mentions(reason)}')

@bot.command(name='ban')
@staff_only()
async def ban(ctx,member:discord.Member,*,reason:str='No reason provided'):
    await member.ban(reason=reason);await ctx.reply(f'Banned {member}.')
    await log_action(ctx.guild,f'ban: {member}, by {ctx.author.mention}: {discord.utils.escape_mentions(reason)}')

@bot.command(name='lock')
@staff_only()
async def lock(ctx):
    await ctx.channel.set_permissions(discord.utils.get(ctx.guild.roles,name='regular'),send_messages=False)
    await ctx.reply('Channel locked.');await log_action(ctx.guild,f'locked: {ctx.channel.mention} by {ctx.author.mention}')

@bot.command(name='unlock')
@staff_only()
async def unlock(ctx):
    await ctx.channel.set_permissions(discord.utils.get(ctx.guild.roles,name='regular'),send_messages=True)
    await ctx.reply('Channel unlocked.');await log_action(ctx.guild,f'unlocked: {ctx.channel.mention} by {ctx.author.mention}')

@bot.command(name='rank')
async def rank(ctx,member:discord.Member=None):

    member=member or ctx.author
    row=con.execute('SELECT xp FROM xp WHERE guild=? AND user=?',(ctx.guild.id,member.id)).fetchone()
    xp=row[0] if row else 0
    await ctx.reply(f'{member.display_name} — level **{level(xp)}** · **{xp} XP**')

@bot.command(name='leaderboard')
async def leaderboard(ctx):

    rows=con.execute('SELECT user,xp FROM xp WHERE guild=? ORDER BY xp DESC LIMIT 10',(ctx.guild.id,)).fetchall()
    await ctx.reply('**offhours leaderboard**\n'+'\n'.join(f'{i}. <@{u}> — level {level(x)} ({x} XP)' for i,(u,x) in enumerate(rows,1)) if rows else 'No XP yet.')

@bot.command(name='help')
async def help_cmd(ctx):
    await ctx.reply('**offhours** — `?rank`, `?leaderboard`, `?testwelcome` (owner)\n**staff** — `?purge 10`, `?warn @user reason`, `?warnings @user`, `?timeout @user 10 reason`, `?kick @user reason`, `?ban @user reason`, `?lock`, `?unlock`\n**owner** — `?setup`, `?say #channel text`')

@bot.event
async def on_command_error(ctx,error):
    if isinstance(error,commands.CommandNotFound): return
    if isinstance(error,(commands.CheckFailure,commands.MissingPermissions)):
        return await ctx.reply('You do not have permission to use that command.')
    if isinstance(error,(commands.BadArgument,commands.MissingRequiredArgument)):
        return await ctx.reply('Invalid arguments. Use `?help` for examples.')
    if isinstance(error,commands.CommandInvokeError) and isinstance(error.original,discord.Forbidden):
        return await ctx.reply('Discord denied that action. Check my permissions and role position.')
    if isinstance(error,discord.Forbidden):
        return await ctx.reply('Discord denied that action. Check my permissions and role position.')
    log.exception('Command error',exc_info=error)
    await ctx.reply('Something went wrong. Check the bot console.')

_twitch_token=None
_twitch_expiry=0
_twitch_was_live=False
@tasks.loop(minutes=3)
async def twitch_check():
    global _twitch_token,_twitch_expiry,_twitch_was_live
    guild=bot.get_guild(GUILD_ID)
    if not guild: return
    channel=discord.utils.get(guild.text_channels,name='live-notifications')
    if not channel: return
    try:
        async with aiohttp.ClientSession() as session:
            if not _twitch_token or time.time()>_twitch_expiry:
                async with session.post('https://id.twitch.tv/oauth2/token',params={'client_id':TWITCH_CLIENT_ID,'client_secret':TWITCH_CLIENT_SECRET,'grant_type':'client_credentials'}) as response:
                    data=await response.json()
                    if response.status!=200: raise RuntimeError(f'Twitch token error: {response.status}')
                    _twitch_token=data['access_token'];_twitch_expiry=time.time()+data['expires_in']-60
            async with session.get('https://api.twitch.tv/helix/streams',params={'user_login':TWITCH_LOGIN},headers={'Client-ID':TWITCH_CLIENT_ID,'Authorization':f'Bearer {_twitch_token}'}) as response:
                data=await response.json()
                if response.status!=200: raise RuntimeError(f'Twitch API error: {response.status}')
                live=bool(data.get('data'))
                if live and not _twitch_was_live:
                    await channel.send(f'**hika is live.**\nhttps://twitch.tv/{TWITCH_LOGIN}')
                _twitch_was_live=live
    except Exception: log.exception('Twitch check failed')


# Slash commands are registered per guild so updates appear promptly.
from discord import app_commands

@bot.tree.command(name='rank',description='View your XP, level and next milestone',guild=discord.Object(id=GUILD_ID))
@app_commands.describe(member='Optional member to inspect')
async def slash_rank(interaction:discord.Interaction,member:discord.Member=None):
    target=member or interaction.user
    xp=member_xp(interaction.guild_id,target.id); lvl=level(xp)
    next_xp=xp_for_level(min(150,lvl+1))
    em=discord.Embed(title=f'rank / {target.display_name}',description=f'**level {lvl}** · {xp:,} XP'+(f'\\n{max(0,next_xp-xp):,} XP to next level' if lvl<150 else '\\nmaximum level reached'),colour=0xE5E5E5)
    em.set_thumbnail(url=target.display_avatar.url)
    await interaction.response.send_message(embed=em)

@bot.tree.command(name='leaderboard',description='Top members by experience',guild=discord.Object(id=GUILD_ID))
async def slash_leaderboard(interaction:discord.Interaction):
    rows=con.execute('SELECT user,xp FROM xp WHERE guild=? ORDER BY xp DESC LIMIT 10',(interaction.guild_id,)).fetchall()
    msg='\\n'.join(f'**{i}.** <@{u}> · level {level(x)} · {x:,} XP' for i,(u,x) in enumerate(rows,1)) or 'No XP earned yet.'
    await interaction.response.send_message(embed=discord.Embed(title='offhours / leaderboard',description=msg,colour=0xE5E5E5))

@bot.tree.command(name='server-audit',description='Check roles, channels, and bot permissions safely',guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
async def slash_audit(interaction:discord.Interaction):
    if interaction.user.id!=OWNER_ID and not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message('Staff only.',ephemeral=True)
    guild=interaction.guild; me=guild.me; perms=me.guild_permissions
    required={'Manage Roles':perms.manage_roles,'Manage Channels':perms.manage_channels,'Manage Messages':perms.manage_messages,'Read Message History':perms.read_message_history,'Add Reactions':perms.add_reactions,'Embed Links':perms.embed_links,'Moderate Members':perms.moderate_members}
    lines=[('✅' if ok else '❌')+' '+name for name,ok in required.items()]
    for name in ('welcome','rules','verify','general','media','post-your-setup','mod-logs'):
        ch=discord.utils.get(guild.text_channels,name=name)
        lines.append(('✅' if ch else '❌')+' #'+name)
    member=discord.utils.get(guild.roles,name='regular')
    lines.append(('✅' if member and member.position<me.top_role.position else '❌')+' role hierarchy')
    lines.append('ℹ️ External member tests: not performed')
    lines.append('ℹ️ Discord Community setup: manual')
    em=discord.Embed(title='offhours / server audit',description='\\n'.join(lines),colour=0xE5E5E5)
    await interaction.response.send_message(embed=em,ephemeral=True)
    logch=discord.utils.get(guild.text_channels,name='mod-logs')
    if logch:
        await logch.send(f'{interaction.user.mention} completed a safe server audit. Review the private results.',allowed_mentions=discord.AllowedMentions(users=[interaction.user]),silent=True)

@bot.tree.command(name='test-welcome',description='Preview the member join notification',guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
async def slash_test_welcome(interaction:discord.Interaction):
    if interaction.user.id!=OWNER_ID:return await interaction.response.send_message('Owner only.',ephemeral=True)
    await join_panel(interaction.user,test=True)
    await interaction.response.send_message('Join preview posted in #welcome.',ephemeral=True)

@bot.tree.command(name='colour',description='Show the starter role colour selector',guild=discord.Object(id=GUILD_ID))
async def slash_colour(interaction:discord.Interaction):
    await interaction.response.send_message('Choose a starter colour below (available before level 5).',view=ColourView(),ephemeral=True)

@bot.tree.command(name='announce',description='Post an announcement as offhours',guild=discord.Object(id=GUILD_ID))
@app_commands.describe(channel='Destination channel',message='Message to post')
@app_commands.default_permissions(manage_guild=True)
async def slash_announce(interaction:discord.Interaction,channel:discord.TextChannel,message:str):
    if interaction.user.id!=OWNER_ID:return await interaction.response.send_message('Owner only.',ephemeral=True)
    if len(message)>1900:return await interaction.response.send_message('Message too long.',ephemeral=True)
    await channel.send(message,allowed_mentions=discord.AllowedMentions.none())
    await interaction.response.send_message(f'Posted in {channel.mention}.',ephemeral=True)

@bot.command(name='mute')
@staff_only()
async def mute(ctx,member:discord.Member,minutes:int=10,*,reason:str='No reason provided'):
    if not 1<=minutes<=40320:return await ctx.reply('Choose 1–40320 minutes.')
    await member.timeout(discord.utils.utcnow()+__import__('datetime').timedelta(minutes=minutes),reason=reason)
    await ctx.reply(f'Muted {member.mention} for {minutes} minutes.')

@bot.command(name='unmute')
@staff_only()
async def unmute(ctx,member:discord.Member):
    await member.timeout(None,reason=f'Unmuted by {ctx.author}')
    await ctx.reply(f'Unmuted {member.mention}.')



async def startup_audit(guild):
    """Read-only startup diagnostics. Never delete messages, change roles or self-sabotage permissions."""
    channel=discord.utils.get(guild.text_channels,name='mod-logs') or discord.utils.get(guild.text_channels,name='staff-chat')
    if not channel: return
    me=guild.me
    checks={
        'manage roles':me.guild_permissions.manage_roles,
        'manage channels':me.guild_permissions.manage_channels,
        'manage messages':me.guild_permissions.manage_messages,
        'embed links':me.guild_permissions.embed_links,
        'add reactions':me.guild_permissions.add_reactions,
        'moderate members':me.guild_permissions.moderate_members,
        'read message history':me.guild_permissions.read_message_history,
    }
    for name in ('welcome','rules','verify','general','media','post-your-setup'):
        ch=discord.utils.get(guild.text_channels,name=name)
        checks['#'+name]=bool(ch)
        if ch:
            p=ch.permissions_for(me)
            checks['#'+name+' embeds']=p.embed_links
            checks['#'+name+' reactions']=p.add_reactions
    rank=discord.utils.get(guild.roles,name='regular')
    checks['member role manageable']=bool(rank and me.top_role>rank)
    checks['community enabled']='COMMUNITY' in [str(f).upper() for f in guild.features]
    result='\\n'.join(('✅' if ok else '❌')+' '+name for name,ok in checks.items())
    result+='\\n\\n*Read-only checks only; no fake member spam, real ban, or destructive permission tests.*'
    em=discord.Embed(title='offhours / startup audit',description=result[:4000],colour=0xE5E5E5)
    owner=guild.get_member(OWNER_ID)
    mention=owner.mention if owner else ''
    await channel.send(content=mention,embed=em,allowed_mentions=discord.AllowedMentions(users=[owner] if owner else []),silent=True)

@bot.tree.command(name='setup-check',description='Inspect Community readiness and channel permissions',guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
async def setup_check(interaction:discord.Interaction):
    if interaction.user.id!=OWNER_ID:
        return await interaction.response.send_message('Owner only.',ephemeral=True)
    enabled='COMMUNITY' in interaction.guild.features
    rules=discord.utils.get(interaction.guild.text_channels,name='rules')
    announcements=discord.utils.get(interaction.guild.text_channels,name='announcements')
    em=discord.Embed(title='offhours / community setup',description=(
        f'Community enabled: **{"yes" if enabled else "no"}**\\n'
        f'Rules channel: {rules.mention if rules else "missing"}\\n'
        f'Announcements channel: {announcements.mention if announcements else "missing"}\\n\\n'
        'Discord requires the owner to enable Community in Server Settings. '
        'After that, choose your rules channel and announcement channel in the Community settings.'
    ),colour=0xE5E5E5)
    await interaction.response.send_message(embed=em,ephemeral=True)


# Optional audio dependencies: pip install yt-dlp PyNaCl
# ffmpeg must be installed and available on PATH. Spotify URLs are metadata-only;
# direct Spotify audio extraction is not supported.
import shutil
from collections import deque
try:
    import yt_dlp
except ImportError:
    yt_dlp = None

MUSIC_STATES = {}
class MusicState:
    def __init__(self):
        self.queue=deque()
        self.history=deque(maxlen=30)
        self.current=None
        self.loop=False
        self.lock=asyncio.Lock()

def music_state(guild_id):
    return MUSIC_STATES.setdefault(guild_id, MusicState())

def music_allowed(interaction):
    m=interaction.user
    return isinstance(m,discord.Member) and (m.id==OWNER_ID or member_level(interaction.guild_id,m.id)>=50)

def music_voice(interaction):
    member=interaction.user
    return member.voice.channel if isinstance(member,discord.Member) and member.voice and member.voice.channel.name in ('general','afk') else None

def extract_tracks(url):
    if yt_dlp is None: raise RuntimeError('yt-dlp is not installed.')
    opts={'quiet':True,'no_warnings':True,'extract_flat':'in_playlist','noplaylist':False,'playlistend':50,'skip_download':True,'default_search':'error'}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info=ydl.extract_info(url,download=False)
    if not info: raise RuntimeError('No playable tracks were found.')
    entries=info.get('entries') or [info]
    tracks=[]
    for entry in entries:
        if not entry:continue
        link=entry.get('webpage_url') or entry.get('url')
        if link and not str(link).startswith('http') and entry.get('id'):
            link='https://www.youtube.com/watch?v='+entry['id']
        if link and str(link).startswith('http'):
            tracks.append({'title':entry.get('title') or 'Unknown track','url':link})
    return tracks[:50]

def resolve_audio(url):
    with yt_dlp.YoutubeDL({'quiet':True,'no_warnings':True,'format':'bestaudio/best','noplaylist':True}) as ydl:
        info=ydl.extract_info(url,download=False)
    if not info or not info.get('url'):raise RuntimeError('Audio source could not be resolved.')
    return info['url'], info.get('title','Unknown track')

async def play_next(guild):
    state=music_state(guild.id)
    voice=guild.voice_client
    if not voice or not voice.is_connected():return
    async with state.lock:
        if state.loop and state.current:
            track=state.current
        elif state.queue:
            track=state.queue.popleft()
            state.current=track
            state.history.append(track)
        else:
            state.current=None
            return
    try:
        audio,title=await asyncio.to_thread(resolve_audio,track['url'])
        state.current['title']=title
        source=discord.FFmpegPCMAudio(audio,before_options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',options='-vn')
        def finished(error):
            if error:log.error('Audio playback error: %s',error)
            asyncio.run_coroutine_threadsafe(play_next(guild),bot.loop)
        voice.play(source,after=finished)
    except Exception:
        log.exception('Failed to play music track')
        state.loop=False
        await play_next(guild)

class MusicControls(discord.ui.View):
    def __init__(self,guild_id):
        super().__init__(timeout=3600)
        self.guild_id=guild_id
    async def gate(self,interaction):
        if not music_allowed(interaction) or not music_voice(interaction):
            await interaction.response.send_message('Level 50+ and general/afk voice required.',ephemeral=True)
            return False
        return True
    @discord.ui.button(label='Pause / Resume',style=discord.ButtonStyle.secondary)
    async def pause(self,interaction:discord.Interaction,button:discord.ui.Button):
        if not await self.gate(interaction):return
        vc=interaction.guild.voice_client
        if not vc:return await interaction.response.send_message('Nothing playing.',ephemeral=True)
        if vc.is_paused():vc.resume()
        elif vc.is_playing():vc.pause()
        await interaction.response.send_message('Playback toggled.',ephemeral=True)
    @discord.ui.button(label='Skip',style=discord.ButtonStyle.secondary)
    async def skip(self,interaction:discord.Interaction,button:discord.ui.Button):
        if not await self.gate(interaction):return
        vc=interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            music_state(interaction.guild_id).loop=False
            vc.stop()
        await interaction.response.send_message('Skipped.',ephemeral=True)
    @discord.ui.button(label='Loop',style=discord.ButtonStyle.secondary)
    async def loop(self,interaction:discord.Interaction,button:discord.ui.Button):
        if not await self.gate(interaction):return
        state=music_state(interaction.guild_id)
        state.loop=not state.loop
        await interaction.response.send_message('Loop '+('on' if state.loop else 'off'),ephemeral=True)
    @discord.ui.button(label='Previous',style=discord.ButtonStyle.secondary)
    async def previous(self,interaction:discord.Interaction,button:discord.ui.Button):
        if not await self.gate(interaction):return
        state=music_state(interaction.guild_id)
        if len(state.history)<2:return await interaction.response.send_message('No previous track.',ephemeral=True)
        state.history.pop()
        track=state.history.pop()
        state.queue.appendleft(track)
        state.loop=False
        vc=interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):vc.stop()
        elif vc:await play_next(interaction.guild)
        await interaction.response.send_message('Previous track queued.',ephemeral=True)
    @discord.ui.button(label='Stop',style=discord.ButtonStyle.danger)
    async def stop(self,interaction:discord.Interaction,button:discord.ui.Button):
        if not await self.gate(interaction):return
        state=music_state(interaction.guild_id)
        state.queue.clear();state.loop=False;state.current=None
        if interaction.guild.voice_client:await interaction.guild.voice_client.disconnect(force=True)
        await interaction.response.send_message('Player stopped.',ephemeral=True)

@bot.tree.command(name='play',description='Play a song or playlist in general/afk voice (level 50+)',guild=discord.Object(id=GUILD_ID))
@app_commands.describe(link='YouTube or SoundCloud song/playlist URL')
async def slash_music(interaction:discord.Interaction,link:str):
    if not music_allowed(interaction):return await interaction.response.send_message('Music unlocks at level 50.',ephemeral=True)
    channel=music_voice(interaction)
    if not channel:return await interaction.response.send_message('Join general or afk voice first.',ephemeral=True)
    if not re.match(r'^https://(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com|soundcloud\.com)/',link,re.I):
        return await interaction.response.send_message('Use a YouTube or SoundCloud link. Spotify streams cannot be extracted directly.',ephemeral=True)
    if not yt_dlp or not shutil.which('ffmpeg') or not __import__('importlib').util.find_spec('nacl'):
        return await interaction.response.send_message('Install yt-dlp, PyNaCl and FFmpeg on the host to enable audio.',ephemeral=True)
    await interaction.response.defer(ephemeral=True,thinking=True)
    try:
        tracks=await asyncio.to_thread(extract_tracks,link)
        if not tracks:raise RuntimeError('No tracks found.')
        vc=interaction.guild.voice_client
        if vc and vc.channel!=channel:await vc.move_to(channel)
        elif not vc:vc=await channel.connect()
        state=music_state(interaction.guild_id)
        state.queue.extend(tracks)
        if not vc.is_playing() and not vc.is_paused():await play_next(interaction.guild)
        em=discord.Embed(title='offhours / music',description=f'Queued **{len(tracks)}** track(s).\nUse the controls below.',colour=0xE5E5E5)
        await channel.send(embed=em,view=MusicControls(interaction.guild_id))
        await interaction.followup.send('Queued '+str(len(tracks))+' track(s).',ephemeral=True)
    except Exception as exc:
        log.exception('Music request failed')
        await interaction.followup.send('Music failed: '+str(exc)[:300],ephemeral=True)

@bot.tree.command(name='safety-check',description='Run non-destructive moderation and permission diagnostics',guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(administrator=True)
async def test_safety(interaction:discord.Interaction):
    if interaction.user.id!=OWNER_ID:return await interaction.response.send_message('Owner only.',ephemeral=True)
    await interaction.response.defer(ephemeral=True,thinking=True)
    guild=interaction.guild
    me=guild.me
    checks=[]
    def check(label,passed):
        checks.append((label,bool(passed)))
    perms=me.guild_permissions
    for key in ('manage_messages','manage_roles','manage_channels','moderate_members','ban_members','kick_members','embed_links','add_reactions','read_message_history','connect','speak'):
        check('bot permission: '+key,getattr(perms,key,False))
    regular=discord.utils.get(guild.roles,name='regular')
    pending=discord.utils.get(guild.roles,name='pending')
    check('regular role assignable',bool(regular and me.top_role>regular))
    check('pending role assignable',bool(pending and me.top_role>pending))
    for name in ('welcome','rules','verify','general','media','post-your-setup','mod-logs'):
        ch=discord.utils.get(guild.text_channels,name=name)
        check('channel #'+name,bool(ch))
        if ch:
            p=ch.permissions_for(me)
            check('send in #'+name,p.send_messages)
            check('embed in #'+name,p.embed_links)
            check('react in #'+name,p.add_reactions)
    check('link detector',bool(link_re.search('https://example.com')))
    check('link non-match',not bool(link_re.search('ordinary message')))
    check('XP progression',xp_for_level(6)>xp_for_level(5)>xp_for_level(4))
    check('level cap',level(xp_for_level(150)+50000)==150)
    check('yt-dlp installed',bool(yt_dlp))
    check('FFmpeg executable found',bool(shutil.which('ffmpeg')))
    check('PyNaCl voice library installed',bool(discord.opus.is_loaded() or __import__('importlib').util.find_spec('nacl')))
    failed=[label for label,passed in checks if not passed]
    report = [
        'OFFHOURS / SAFETY REPORT',
        'Generated: '+discord.utils.utcnow().isoformat(),
        'Server: '+guild.name+' ('+str(guild.id)+')',
        'Result: '+str(len(checks)-len(failed))+'/'+str(len(checks))+' checks passed',
        '',
        *[('PASS' if ok else 'FAIL')+'  '+label for label,ok in checks],
        '',
        'READ-ONLY: These checks inspect bot permissions, channel access,',
        'role hierarchy, dependencies and basic logic. They do NOT simulate',
        'real user joins, spam, bans, punishment or real music playback.',
        '',
        'Community enabled: '+str('COMMUNITY' in guild.features),
        'Native onboarding configured: NOT VERIFIED by this command.',
        'Music: install yt-dlp, PyNaCl and FFmpeg on the machine hosting the bot.',
        'Spotify tracks cannot be streamed directly using a Spotify URL.',
        '',
        'If a check fails, fix the reported permission or dependency and rerun.'
    ]
    payload='\\n'.join(report).encode('utf-8')
    await interaction.followup.send(
        content=f'Safety diagnostics: {len(checks)-len(failed)}/{len(checks)} passed. Download the attached report.',
        file=discord.File(io.BytesIO(payload),filename='readme.txt'),
        ephemeral=True
    )


class RegionSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder='choose your region',min_values=1,max_values=1,
            options=[discord.SelectOption(label=x.title(),value=x) for x in ('europe','north america','south america','asia','africa','oceania')],
            custom_id='offhours:region:select')
    async def callback(self,interaction:discord.Interaction):
        if not isinstance(interaction.user,discord.Member):
            return await interaction.response.send_message('Members only.',ephemeral=True)
        member=interaction.user
        if not discord.utils.get(member.roles,name='regular'):
            return await interaction.response.send_message('Verify first.',ephemeral=True)
        new=discord.utils.get(interaction.guild.roles,name='region · '+self.values[0])
        if not new:
            return await interaction.response.send_message('Run ?setup to create region roles.',ephemeral=True)
        old=[role for role in member.roles if role.name.startswith('region · ')]
        try:
            if old: await member.remove_roles(*old,reason='Region selection')
            await member.add_roles(new,reason='Region selection')
            await interaction.response.send_message('Region updated.',ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message('Bot role must be above region roles.',ephemeral=True)

class RegionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(RegionSelect())

@bot.tree.command(name='region',description='Choose your region role',guild=discord.Object(id=GUILD_ID))
async def slash_region(interaction:discord.Interaction):
    await interaction.response.send_message('Choose your region.',view=RegionView(),ephemeral=True)

@bot.tree.command(name='ban',description='Ban a member by user ID or username',guild=discord.Object(id=GUILD_ID))
@app_commands.default_permissions(ban_members=True)
async def slash_ban(interaction:discord.Interaction,user:str,reason:str='No reason provided'):
    if not interaction.user.guild_permissions.ban_members:
        return await interaction.response.send_message('Ban Members permission required.',ephemeral=True)
    target=discord.utils.get(interaction.guild.members,name=user) or discord.utils.get(interaction.guild.members,display_name=user)
    if target is None:
        user_id=user.strip().strip('<@!>')
        if not user_id.isdecimal():
            return await interaction.response.send_message('Use a member mention or numeric user ID.',ephemeral=True)
        target=await bot.fetch_user(int(user_id))
    if target.id==OWNER_ID or target.id==interaction.user.id:
        return await interaction.response.send_message('Cannot ban that user.',ephemeral=True)
    try:
        await interaction.guild.ban(target,reason=reason[:400],delete_message_seconds=0)
        await interaction.response.send_message('Banned '+str(target),ephemeral=True)
        await log_action(interaction.guild,'ban: '+str(target.id)+' by '+str(interaction.user.id))
    except discord.HTTPException:
        await interaction.response.send_message('Ban failed. Check role hierarchy and permissions.',ephemeral=True)


@bot.tree.command(name='message',description='Send a public formatted message to a selected channel',guild=discord.Object(id=GUILD_ID))
@app_commands.describe(channel='Channel to post in',message='Discord Markdown text',title='Optional embed title',embed='Send as an embed instead of plain text')
@app_commands.default_permissions(manage_guild=True)
async def slash_message(interaction:discord.Interaction,channel:discord.TextChannel,message:str,title:str='',embed:bool=False):
    if interaction.user.id!=OWNER_ID and not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message('Staff only.',ephemeral=True)
    if len(message)>1900 or len(title)>200:
        return await interaction.response.send_message('Message or title too long.',ephemeral=True)
    try:
        if embed:
            await channel.send(embed=discord.Embed(title=title or None,description=message,colour=0xE6E6E6),allowed_mentions=discord.AllowedMentions.none())
        else:
            await channel.send(message,allowed_mentions=discord.AllowedMentions.none())
        await interaction.response.send_message('Published in '+channel.mention,ephemeral=True)
    except discord.HTTPException:
        await interaction.response.send_message('Could not publish. Check bot channel permissions.',ephemeral=True)


if __name__=='__main__':
    if not TOKEN or TOKEN=='PASTE_YOUR_PRIVATE_BOT_TOKEN_HERE' or not GUILD_ID or not OWNER_ID:
        raise SystemExit('Edit .env first: DISCORD_TOKEN, GUILD_ID and OWNER_ID are required.')
    bot.run(TOKEN)