import os, asyncio, logging, sqlite3, time, re, random, math, io, datetime
from pathlib import Path
import aiohttp
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN=os.getenv('DISCORD_TOKEN','')
LEGACY_GUILD_ID=int(os.getenv('GUILD_ID') or '0')  # optional one-time migration only

TWITCH_CLIENT_ID=os.getenv('TWITCH_CLIENT_ID','')
TWITCH_CLIENT_SECRET=os.getenv('TWITCH_CLIENT_SECRET','')
TWITCH_LOGIN=os.getenv('TWITCH_LOGIN','')
CUSTOM_XP=True  # offhours handles levels; disable Maki XP to avoid duplicates
WELCOME_BANNER_URL=os.getenv('WELCOME_BANNER_URL','')
logging.basicConfig(level=logging.INFO)
log=logging.getLogger('offhours')
from logging.handlers import RotatingFileHandler
log_folder=Path('logs');log_folder.mkdir(exist_ok=True)
rotating_log=RotatingFileHandler(log_folder/'offhours.log',maxBytes=1048576,backupCount=3,encoding='utf-8')
rotating_log.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
log.addHandler(rotating_log)
DB=Path(os.getenv('OFFHOURS_DB_PATH') or str(Path(__file__).with_name('offhours.sqlite3')))
DB.parent.mkdir(parents=True, exist_ok=True)
con=sqlite3.connect(DB)
con.execute('PRAGMA journal_mode=WAL')
con.execute('PRAGMA busy_timeout=5000')
con.execute('PRAGMA foreign_keys=ON')
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
intents.invites=True
intents.moderation=True
intents.message_content=True
from services.configuration import ServerCommandTree, administrator, enabled, channel_for, role_for, allowed_command
def command_prefix(bot, message):
    return setting(message.guild.id, 'prefix', '?') if message.guild else '?'
bot=commands.Bot(command_prefix=command_prefix, intents=intents, tree_cls=ServerCommandTree, chunk_guilds_at_startup=False, max_messages=500, help_command=None, allowed_mentions=discord.AllowedMentions.none())
bot.get_setting = setting
@bot.check
async def prefix_policy(ctx):
    if not ctx.guild:
        return False
    from types import SimpleNamespace
    invocation = SimpleNamespace(guild=ctx.guild,guild_id=ctx.guild.id,user=ctx.author,channel_id=ctx.channel.id)
    ok, _ = allowed_command(invocation,setting,ctx.command.qualified_name if ctx.command else '')
    return ok

COLOURS={'slate':0x8996A6,'silver':0xC6C8CB,'blue':0x829CB7,'lavender':0xA594C1,'sage':0x8BA99C,'rose':0xBA929D}
# XP = 30 * level^2; 750 XP to level 5 (~1-3 hours of active conversation).
def level(xp): return min(150,math.isqrt(max(0,xp)//30))
def xp_for_level(lvl): return 30*lvl*lvl
last_messages={}
link_re=re.compile(r'(https?://|www\.|discord\.gg/|tenor\.com/|giphy\.com/)',re.I)
def owner_only(ctx): return ctx.guild is not None and administrator(ctx.author)

def staff_only():
    async def predicate(ctx):
        return ctx.guild is not None and (ctx.author.guild_permissions.manage_messages)
    return commands.check(predicate)

async def log_action(guild, msg):
    channel=channel_for(guild,setting,'logs')
    if channel:
        try: await channel.send(msg,allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException: pass

class VerifyView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label='Verify',style=discord.ButtonStyle.success,custom_id='offhours:verify:v1')
    async def verify(self,interaction:discord.Interaction,button:discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user,discord.Member): return
        if not enabled(setting,interaction.guild_id,'verification'):
            return await interaction.response.send_message('Verification is disabled in this server.',ephemeral=True)
        member=role_for(interaction.guild,setting,'regular')
        unverified=role_for(interaction.guild,setting,'pending')
        if not member or member.managed or member >= interaction.guild.me.top_role or member.permissions.administrator or member.permissions.manage_roles: return await interaction.response.send_message('Verification role is unavailable or protected. Ask an administrator to configure it.',ephemeral=True)
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
        regular=role_for(g,setting,'regular')
        if not enabled(setting,g.id,'selfroles') or (regular and regular not in member.roles):
            return await interaction.response.send_message('verify first.',ephemeral=True)
        if member_level(g.id,member.id)>=5:
            return await interaction.response.send_message('level rewards replace starter colours after level 5.',ephemeral=True)
        values={int(v) for k,v in con.execute("SELECT key,value FROM settings WHERE guild=? AND key LIKE 'selfrole:colour:%'",(g.id,)) if v.isdecimal()}
        selected=setting(g.id,'selfrole:colour:'+self.values[0],'0')
        chosen=g.get_role(int(selected)) if selected.isdecimal() else None
        if not chosen or chosen.managed or chosen>=g.me.top_role or chosen.permissions.administrator or chosen.permissions.manage_roles:
            return await interaction.response.send_message('This colour role is no longer configured. Open /colour for the current menu.',ephemeral=True)
        remove=[r for r in member.roles if r.id in values and r.id!=chosen.id]
        if remove:await member.remove_roles(*remove,reason='Changed configured colour')
        await member.add_roles(chosen,reason='Selected configured starter colour')
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
    service = getattr(bot, 'panels_service', None)
    if service:
        await service.emit('level', member, level=lvl, xp=member_xp(member.guild.id,member.id))

async def join_panel(member,test=False,**variables):
    service = getattr(bot, 'panels_service', None)
    if service:
        await service.emit('welcome', member, test=test, **variables)

@bot.event
async def on_ready():
    if not getattr(bot,'_initialized',False):
        bot.add_view(VerifyView()); bot.add_view(ColourView()); bot.add_view(RegionView()); bot._initialized=True
        from services.legacy_preset import migrate_legacy
        if LEGACY_GUILD_ID:
            legacy=bot.get_guild(LEGACY_GUILD_ID)
            if legacy: migrate_legacy(legacy,con,setting,set_setting)
        try:
            await bot.tree.sync()
            if LEGACY_GUILD_ID and setting(LEGACY_GUILD_ID,'migration:global_commands','0')!='1':
                bot.tree.clear_commands(guild=discord.Object(id=LEGACY_GUILD_ID))
                await bot.tree.sync(guild=discord.Object(id=LEGACY_GUILD_ID))
                set_setting(LEGACY_GUILD_ID,'migration:global_commands','1')
        except discord.HTTPException: log.exception('Slash command sync failed')
        # No startup welcome previews or audit messages.
    log.info('Ready as %s',bot.user)

@bot.event
async def on_member_join(member):
    if member.bot: return
    invite_variables=await invites_service.joined(member) if enabled(setting,member.guild.id,'invites') else {}
    if enabled(setting,member.guild.id,'verification'):
        role=role_for(member.guild,setting,'pending')
        if role:
            try: await member.add_roles(role,reason='Configured verification on join')
            except discord.HTTPException: log.warning('Cannot assign configured pending role in %s',member.guild.id)
    autorole=role_for(member.guild,setting,'autorole')
    if enabled(setting,member.guild.id,'selfroles') and autorole:
        try: await member.add_roles(autorole,reason='Configured autorole')
        except discord.HTTPException: pass
    if enabled(setting,member.guild.id,'welcome'):
        await join_panel(member,**invite_variables)

@bot.event
async def on_message(message):
    await bot.process_commands(message)

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

# Slash commands are registered per guild so updates appear promptly.
from discord import app_commands

@bot.tree.command(name='rank',description='View your XP, level and next milestone')
@app_commands.describe(member='Optional member to inspect')
async def slash_rank(interaction:discord.Interaction,member:discord.Member=None):
    target=member or interaction.user
    xp=member_xp(interaction.guild_id,target.id); lvl=level(xp)
    next_xp=xp_for_level(min(150,lvl+1))
    em=discord.Embed(title=f'rank / {target.display_name}',description=f'**level {lvl}** · {xp:,} XP'+(f'\\n{max(0,next_xp-xp):,} XP to next level' if lvl<150 else '\\nmaximum level reached'),colour=0xE5E5E5)
    em.set_thumbnail(url=target.display_avatar.url)
    await interaction.response.send_message(embed=em)

@bot.tree.command(name='leaderboard',description='Top members by experience')
async def slash_leaderboard(interaction:discord.Interaction):
    rows=con.execute('SELECT user,xp FROM xp WHERE guild=? ORDER BY xp DESC LIMIT 10',(interaction.guild_id,)).fetchall()
    msg='\\n'.join(f'**{i}.** <@{u}> · level {level(x)} · {x:,} XP' for i,(u,x) in enumerate(rows,1)) or 'No XP earned yet.'
    await interaction.response.send_message(embed=discord.Embed(title='offhours / leaderboard',description=msg,colour=0xE5E5E5))

@bot.tree.command(name='server-audit',description='Check roles, channels, and bot permissions safely')
@app_commands.default_permissions(manage_guild=True)
async def slash_audit(interaction:discord.Interaction):
    if not administrator(interaction.user) and not interaction.user.guild_permissions.manage_guild:
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
@bot.tree.command(name='test-welcome',description='Preview the member join notification')
@app_commands.default_permissions(manage_guild=True)
async def slash_test_welcome(interaction:discord.Interaction):
    if not administrator(interaction.user):return await interaction.response.send_message('Owner only.',ephemeral=True)
    await join_panel(interaction.user,test=True)
    await interaction.response.send_message('Join preview posted in #welcome.',ephemeral=True)

@bot.tree.command(name='colour',description='Show the starter role colour selector')
async def slash_colour(interaction:discord.Interaction):
    await interaction.response.send_message('Choose a starter colour below (available before level 5).',view=ColourView(),ephemeral=True)

@bot.tree.command(name='announce',description='Post an announcement as offhours')
@app_commands.describe(channel='Destination channel',message='Message to post')
@app_commands.default_permissions(manage_guild=True)
async def slash_announce(interaction:discord.Interaction,channel:discord.TextChannel,message:str):
    if not administrator(interaction.user):return await interaction.response.send_message('Owner only.',ephemeral=True)
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



@bot.tree.command(name='setup-check',description='Inspect Community readiness and channel permissions')
@app_commands.default_permissions(manage_guild=True)
async def setup_check(interaction:discord.Interaction):
    if not administrator(interaction.user):
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


# Audio dependencies are installed and checked by the host startup process.
import shutil
import yt_dlp
from services.audio_runtime import ffmpeg_path
from cogs.music import install as install_music
music_service = install_music(bot, member_level, setting, set_setting)

@bot.tree.command(name='safety-check',description='Run non-destructive moderation and permission diagnostics')
@app_commands.default_permissions(administrator=True)
async def test_safety(interaction:discord.Interaction):
    if not administrator(interaction.user):return await interaction.response.send_message('Owner only.',ephemeral=True)
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
    check('FFmpeg executable found',bool(ffmpeg_path()))
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
        'Music: all audio dependencies are installed by python main.py on the host. Audible playback requires a listener’s confirmation.',
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
        if not enabled(setting,member.guild.id,'selfroles'):
            return await interaction.response.send_message('Self-roles are disabled.',ephemeral=True)
        selected=setting(member.guild.id,'selfrole:region:'+self.values[0],'0')
        new=member.guild.get_role(int(selected)) if selected.isdecimal() else None
        if not new or new.managed or new>=member.guild.me.top_role or new.permissions.administrator or new.permissions.manage_roles:
            return await interaction.response.send_message('This region role is no longer configured. Open /region for the current menu.',ephemeral=True)
        values={int(v) for k,v in con.execute("SELECT key,value FROM settings WHERE guild=? AND key LIKE 'selfrole:region:%'",(member.guild.id,)) if v.isdecimal()}
        old=[role for role in member.roles if role.id in values and role.id!=new.id]
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

@bot.tree.command(name='region',description='Choose your region role')
async def slash_region(interaction:discord.Interaction):
    await interaction.response.send_message('Choose your region.',view=RegionView(),ephemeral=True)

@bot.tree.command(name='ban',description='Ban a member by user ID or username')
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
    if target.id==interaction.guild.owner_id or target.id==interaction.user.id:
        return await interaction.response.send_message('Cannot ban that user.',ephemeral=True)
    try:
        await interaction.guild.ban(target,reason=reason[:400],delete_message_seconds=0)
        await interaction.response.send_message('Banned '+str(target),ephemeral=True)
        await log_action(interaction.guild,'ban: '+str(target.id)+' by '+str(interaction.user.id))
    except discord.HTTPException:
        await interaction.response.send_message('Ban failed. Check role hierarchy and permissions.',ephemeral=True)


@bot.tree.command(name='message',description='Send a public formatted message to a selected channel')
@app_commands.describe(channel='Channel to post in',message='Discord Markdown text',title='Optional embed title',embed='Send as an embed instead of plain text')
@app_commands.default_permissions(manage_guild=True)
async def slash_message(interaction:discord.Interaction,channel:discord.TextChannel,message:str,title:str='',embed:bool=False):
    if not administrator(interaction.user) and not interaction.user.guild_permissions.manage_guild:
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


from offhours_commands import install as install_offhours_commands
install_offhours_commands(bot, con, member_xp, member_level, setting, set_setting)

from services.configuration import install as install_configuration
configuration_service = install_configuration(bot, con, setting, set_setting)
from cogs import panels, ai, tickets, community, moderation, levels, selfroles, invites, social, analytics, onboarding
bot.panel_service = bot.panels_service = panels.install(bot, con, setting, set_setting)
ai_service = ai.install(bot, con, setting, set_setting)
ticket_service = tickets.install(bot, con, setting, set_setting)
community_service = community.install(bot, con, setting, set_setting, member_level)
moderation_service = moderation.install(bot, con, setting, set_setting)
levels_service = levels.install(bot, con, setting, set_setting)
selfroles_service = selfroles.install(bot, con, setting, set_setting)
invites_service = invites.install(bot, con, setting, set_setting)
social_service = social.install(bot, con, setting, set_setting)
analytics_service = analytics.install(bot, con, setting, set_setting)
onboarding_service = onboarding.install(bot, con, setting, set_setting)
from cogs import safety, embeds, automation, help as help_cog
safety.install(bot, con, setting, set_setting)
help_cog.install(bot, con, setting, set_setting)
embed_service=embeds.install(bot, con, setting, set_setting)
automation_service=automation.install(bot, con, setting, set_setting)
services = [music_service, bot.panel_service, ticket_service, community_service, moderation_service,
            levels_service, social_service, analytics_service, automation_service]

if __name__=='__main__':
    if not TOKEN or TOKEN=='PASTE_YOUR_PRIVATE_BOT_TOKEN_HERE':
        raise SystemExit("Set DISCORD_TOKEN in the host secret environment variables.")
    async def run_bot():
        import signal
        async with bot:
            async def shutdown():
                await bot.close()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))
                except NotImplementedError:
                    pass  # Windows runner handles Ctrl+C.
            try:
                await bot.start(TOKEN)
            finally:
                for service in services:
                    try: await service.close()
                    except Exception: pass
                con.commit()
                con.close()
    asyncio.run(run_bot())