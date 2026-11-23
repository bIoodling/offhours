"""Offhours utility command extension. No startup mutations or unsolicited messages."""
import asyncio
import datetime
import random
import re
import time
from collections import Counter
import discord
from discord import app_commands

def install(bot, owner_id, guild_id, connection, get_xp, get_level, get_setting, put_setting):
    scope=discord.Object(id=guild_id)
    def group(name, description):
        g=app_commands.Group(name=name, description=description, guild_ids=[guild_id])
        bot.tree.add_command(g, guild=scope)
        return g
    def staff(i):
        return i.user.id == owner_id or i.user.guild_permissions.manage_messages
    def admin(i):
        return i.user.id == owner_id or i.user.guild_permissions.manage_guild
    async def reply(i, text, public=False):
        if i.response.is_done():
            await i.followup.send(str(text)[:1900], ephemeral=not public, allowed_mentions=discord.AllowedMentions.none())
        else:
            await i.response.send_message(str(text)[:1900], ephemeral=not public, allowed_mentions=discord.AllowedMentions.none())
    def require(i, predicate):
        return i.guild is not None and i.guild.id==guild_id and predicate(i)
    def check_target(i, member):
        return i.user.id==owner_id or (member.id!=i.guild.owner_id and member.top_role<i.user.top_role)
    def fmt_duration(seconds):
        seconds=int(seconds)
        return f"{seconds//86400}d {(seconds%86400)//3600}h {(seconds%3600)//60}m"
    def channel_perms(i, channel):
        return channel.permissions_for(i.guild.me)
    def config_key(k):
        return "ext:"+k
    info=group("info","Server, user and account information")
    @info.command(name="server",description="Show server information")
    async def server(i:discord.Interaction):
        g=i.guild
        await reply(i,f"**{g.name}**\nID: {g.id}\nOwner: {g.owner}\nMembers: {g.member_count}\nRoles: {len(g.roles)}\nChannels: {len(g.channels)}\nBoosts: {g.premium_subscription_count}")
    @info.command(name="user",description="Show user information")
    async def user(i:discord.Interaction, member:discord.Member=None):
        m=member or i.user
        await reply(i,f"**{m}**\nID: {m.id}\nJoined: {discord.utils.format_dt(m.joined_at,'F') if m.joined_at else 'Unknown'}\nCreated: {discord.utils.format_dt(m.created_at,'F')}\nHighest role: {m.top_role.name}")
    @info.command(name="avatar",description="Show an avatar URL")
    async def avatar(i:discord.Interaction, member:discord.Member=None):
        await reply(i,(member or i.user).display_avatar.url)
    @info.command(name="banner",description="Show the server banner")
    async def banner(i:discord.Interaction):
        await reply(i,i.guild.banner.url if i.guild.banner else "This server has no banner.")
    @info.command(name="icon",description="Show the server icon")
    async def icon(i:discord.Interaction):
        await reply(i,i.guild.icon.url if i.guild.icon else "This server has no icon.")
    @info.command(name="roles",description="List the server roles")
    async def roles(i:discord.Interaction):
        await reply(i,", ".join(r.name for r in reversed(i.guild.roles) if not r.is_default())[:1850] or "No roles.")
    @info.command(name="channels",description="Show channel totals")
    async def channels(i:discord.Interaction):
        g=i.guild
        await reply(i,f"Text: {len(g.text_channels)} | Voice: {len(g.voice_channels)} | Categories: {len(g.categories)} | Forums: {len(g.forums)}")
    @info.command(name="permissions",description="Show a member's server permissions")
    async def permissions(i:discord.Interaction,member:discord.Member=None):
        m=member or i.user
        names=[k.replace('_',' ') for k,v in m.guild_permissions if v]
        await reply(i,f"**{m.display_name}**: "+(", ".join(names)[:1750] or "None"))
    @info.command(name="joined",description="Show when a member joined")
    async def joined(i:discord.Interaction,member:discord.Member=None):
        m=member or i.user
        await reply(i,discord.utils.format_dt(m.joined_at,'R') if m.joined_at else "Unknown")
    @info.command(name="created",description="Show account creation date")
    async def created(i:discord.Interaction,member:discord.Member=None):
        await reply(i,discord.utils.format_dt((member or i.user).created_at,'F'))
    @info.command(name="boosts",description="Show server boost information")
    async def boosts(i:discord.Interaction):
        g=i.guild
        await reply(i,f"Level {g.premium_tier} | {g.premium_subscription_count} boosts")
    @info.command(name="latency",description="Show gateway latency")
    async def latency(i:discord.Interaction):
        await reply(i,f"{bot.latency*1000:.0f} ms")
    @info.command(name="invite",description="Get an invite link if one is available")
    async def invite(i:discord.Interaction):
        if not require(i,admin): return await reply(i,"Manage Server permission required.")
        try:
            links=await i.guild.invites()
            await reply(i,links[0].url if links else "No existing invites.")
        except discord.HTTPException: await reply(i,"Cannot read invites.")
    @info.command(name="emojis",description="List custom emojis")
    async def emojis(i:discord.Interaction):
        await reply(i," ".join(str(e) for e in i.guild.emojis)[:1800] or "No custom emojis.")
    @info.command(name="stats",description="Show basic bot and server stats")
    async def stats(i:discord.Interaction):
        await reply(i,f"Servers: {len(bot.guilds)} | Members: {i.guild.member_count} | Gateway: {bot.latency*1000:.0f}ms")
    moderation=group("mod","Moderation actions and history")
    @moderation.command(name="kick",description="Kick a member")
    async def kick(i:discord.Interaction,member:discord.Member,reason:str="No reason"):
        if not require(i,staff) or not i.user.guild_permissions.kick_members or not check_target(i,member): return await reply(i,"Insufficient permissions or protected target.")
        try: await member.kick(reason=reason); await reply(i,f"Kicked {member}.")
        except discord.HTTPException as e: await reply(i,f"Kick failed: {e}")
    @moderation.command(name="ban",description="Ban a member")
    async def ban(i:discord.Interaction,member:discord.Member,reason:str="No reason"):
        if not require(i,staff) or not i.user.guild_permissions.ban_members or not check_target(i,member): return await reply(i,"Insufficient permissions or protected target.")
        try: await member.ban(reason=reason,delete_message_seconds=0); await reply(i,f"Banned {member}.")
        except discord.HTTPException as e: await reply(i,f"Ban failed: {e}")
    @moderation.command(name="unban",description="Unban a user by ID")
    async def unban(i:discord.Interaction,user_id:str):
        if not require(i,staff) or not i.user.guild_permissions.ban_members: return await reply(i,"Ban Members permission required.")
        try: await i.guild.unban(discord.Object(id=int(user_id))); await reply(i,f"Unbanned {user_id}.")
        except (ValueError,discord.HTTPException) as e: await reply(i,f"Unban failed: {e}")
    @moderation.command(name="timeout",description="Timeout a member for a number of minutes")
    async def timeout(i:discord.Interaction,member:discord.Member,minutes:app_commands.Range[int,1,40320],reason:str="No reason"):
        if not require(i,staff) or not i.user.guild_permissions.moderate_members or not check_target(i,member): return await reply(i,"Insufficient permissions or protected target.")
        try: await member.timeout(datetime.timedelta(minutes=minutes),reason=reason); await reply(i,f"Timed out {member} for {minutes} minutes.")
        except discord.HTTPException as e: await reply(i,f"Timeout failed: {e}")
    @moderation.command(name="untimeout",description="Remove a timeout")
    async def untimeout(i:discord.Interaction,member:discord.Member):
        if not require(i,staff) or not i.user.guild_permissions.moderate_members or not check_target(i,member): return await reply(i,"Insufficient permissions or protected target.")
        try: await member.timeout(None); await reply(i,f"Timeout removed from {member}.")
        except discord.HTTPException as e: await reply(i,f"Failed: {e}")
    @moderation.command(name="nickname",description="Change a member's nickname")
    async def nickname(i:discord.Interaction,member:discord.Member,name:str):
        if not require(i,staff) or not i.user.guild_permissions.manage_nicknames or not check_target(i,member): return await reply(i,"Insufficient permissions.")
        try: await member.edit(nick=name[:32]); await reply(i,"Nickname updated.")
        except discord.HTTPException as e: await reply(i,f"Failed: {e}")
    @moderation.command(name="resetnick",description="Reset a member's nickname")
    async def resetnick(i:discord.Interaction,member:discord.Member):
        if not require(i,staff) or not i.user.guild_permissions.manage_nicknames or not check_target(i,member): return await reply(i,"Insufficient permissions.")
        try: await member.edit(nick=None); await reply(i,"Nickname reset.")
        except discord.HTTPException as e: await reply(i,f"Failed: {e}")
    @moderation.command(name="warn",description="Warn a member and save the warning")
    async def warn(i:discord.Interaction,member:discord.Member,reason:str):
        if not require(i,staff) or not check_target(i,member): return await reply(i,"Staff only or protected target.")
        connection.execute("INSERT INTO warnings (guild,user,moderator,reason,timestamp) VALUES (?,?,?,?,?)",(i.guild.id,member.id,i.user.id,reason,int(time.time())))
        connection.commit()
        await reply(i,f"Warning saved for {member}.")
    @moderation.command(name="warnings",description="Review a member's warnings")
    async def warnings(i:discord.Interaction,member:discord.Member):
        if not require(i,staff): return await reply(i,"Staff only.")
        rows=connection.execute("SELECT id,reason FROM warnings WHERE guild=? AND user=? ORDER BY id DESC LIMIT 15",(i.guild.id,member.id)).fetchall()
        await reply(i,"\n".join(f"#{n}: {r[:120]}" for n,r in rows) or "No warnings.")
    @moderation.command(name="clearwarnings",description="Clear all warnings for a member")
    async def clearwarnings(i:discord.Interaction,member:discord.Member):
        if not require(i,admin): return await reply(i,"Manage Server required.")
        cur=connection.execute("DELETE FROM warnings WHERE guild=? AND user=?",(i.guild.id,member.id));connection.commit()
        await reply(i,f"Removed {cur.rowcount} warnings.")
    @moderation.command(name="purge",description="Delete recent messages (7-day safety cutoff)")
    async def purge(i:discord.Interaction,amount:app_commands.Range[int,1,100]):
        if not require(i,staff) or not i.user.guild_permissions.manage_messages: return await reply(i,"Manage Messages required.")
        await i.response.defer(ephemeral=True)
        cutoff=discord.utils.utcnow()-datetime.timedelta(days=7)
        try:
            deleted=await i.channel.purge(limit=min(amount+20,120),check=lambda m:m.created_at>=cutoff,bulk=True,reason=f"Purge by {i.user}")
            await reply(i,f"Deleted {min(len(deleted),amount)} messages newer than 7 days.")
        except discord.HTTPException as e: await reply(i,f"Purge failed: {e}")
    @moderation.command(name="slowmode",description="Set channel slowmode seconds")
    async def slowmode(i:discord.Interaction,seconds:app_commands.Range[int,0,21600]):
        if not require(i,staff) or not i.user.guild_permissions.manage_channels: return await reply(i,"Manage Channels required.")
        try: await i.channel.edit(slowmode_delay=seconds); await reply(i,f"Slowmode: {seconds}s.")
        except discord.HTTPException as e: await reply(i,f"Failed: {e}")
    @moderation.command(name="lock",description="Lock the current channel for everyone")
    async def lock(i:discord.Interaction):
        if not require(i,staff) or not i.user.guild_permissions.manage_channels: return await reply(i,"Manage Channels required.")
        try: await i.channel.set_permissions(i.guild.default_role,send_messages=False,reason=f"Locked by {i.user}"); await reply(i,"Channel locked.")
        except discord.HTTPException as e: await reply(i,f"Failed: {e}")
    @moderation.command(name="unlock",description="Restore inherited everyone send permission")
    async def unlock(i:discord.Interaction):
        if not require(i,staff) or not i.user.guild_permissions.manage_channels: return await reply(i,"Manage Channels required.")
        try: await i.channel.set_permissions(i.guild.default_role,send_messages=None,reason=f"Unlocked by {i.user}"); await reply(i,"Channel unlocked.")
        except discord.HTTPException as e: await reply(i,f"Failed: {e}")
    @moderation.command(name="history",description="View recent moderation audit entries")
    async def history(i:discord.Interaction):
        if not require(i,admin) or not i.user.guild_permissions.view_audit_log: return await reply(i,"View Audit Log required.")
        try:
            items=[f"{e.action.name}: {e.user} → {e.target}" async for e in i.guild.audit_logs(limit=10)]
            await reply(i,"\n".join(items) or "No recent entries.")
        except discord.HTTPException as e: await reply(i,f"Failed: {e}")
    roles=group("roles","Manage existing roles without creating duplicates")
    @roles.command(name="add",description="Assign an existing role to a member")
    async def add(i:discord.Interaction,member:discord.Member,role:discord.Role):
        if not require(i,admin) or not i.user.guild_permissions.manage_roles or role>=i.guild.me.top_role or role>=i.user.top_role or role.managed: return await reply(i,"Role hierarchy or permission restriction.")
        try: await member.add_roles(role,reason=f"Assigned by {i.user}");await reply(i,"Role assigned.")
        except discord.HTTPException as e: await reply(i,f"Failed: {e}")
    @roles.command(name="remove",description="Remove an existing role from a member")
    async def remove(i:discord.Interaction,member:discord.Member,role:discord.Role):
        if not require(i,admin) or not i.user.guild_permissions.manage_roles or role>=i.guild.me.top_role or role>=i.user.top_role or role.managed: return await reply(i,"Role hierarchy or permission restriction.")
        try: await member.remove_roles(role,reason=f"Removed by {i.user}");await reply(i,"Role removed.")
        except discord.HTTPException as e: await reply(i,f"Failed: {e}")
    @roles.command(name="list",description="List a member's roles")
    async def list_roles(i:discord.Interaction,member:discord.Member=None):
        m=member or i.user
        await reply(i,", ".join(r.name for r in m.roles if not r.is_default())[:1800] or "No roles.")
    @roles.command(name="members",description="Count members with a role")
    async def members(i:discord.Interaction,role:discord.Role):
        await reply(i,f"{role.name}: {len(role.members)} cached members.")
    @roles.command(name="colour",description="Change an existing role colour using a hex value")
    async def colour(i:discord.Interaction,role:discord.Role,hex_code:str):
        if not require(i,admin) or not i.user.guild_permissions.manage_roles or role>=i.guild.me.top_role or role>=i.user.top_role or role.managed: return await reply(i,"Role hierarchy or permission restriction.")
        if not re.fullmatch(r"#?[0-9a-fA-F]{6}",hex_code): return await reply(i,"Use a hex colour such as #829CB7.")
        try: await role.edit(colour=discord.Colour(int(hex_code.lstrip("#"),16)));await reply(i,"Role colour updated.")
        except discord.HTTPException as e: await reply(i,f"Failed: {e}")
    @roles.command(name="rename",description="Rename an existing role")
    async def rename(i:discord.Interaction,role:discord.Role,name:str):
        if not require(i,admin) or not i.user.guild_permissions.manage_roles or role>=i.guild.me.top_role or role>=i.user.top_role or role.managed: return await reply(i,"Role hierarchy or permission restriction.")
        try: await role.edit(name=name[:100]);await reply(i,"Role renamed.")
        except discord.HTTPException as e: await reply(i,f"Failed: {e}")
    @roles.command(name="info",description="Inspect an existing role")
    async def role_info(i:discord.Interaction,role:discord.Role):
        await reply(i,f"{role.name}\nID: {role.id}\nMembers: {len(role.members)}\nColour: {role.colour}\nMentionable: {role.mentionable}\nManaged: {role.managed}")
    channels=group("channel","Edit and inspect existing channels")
    @channels.command(name="topic",description="Set the current text channel topic")
    async def topic(i:discord.Interaction,text:str):
        if not require(i,admin) or not i.user.guild_permissions.manage_channels or not isinstance(i.channel,discord.TextChannel): return await reply(i,"Manage Channels required in a text channel.")
        try: await i.channel.edit(topic=text[:1024]);await reply(i,"Topic updated.")
        except discord.HTTPException as e: await reply(i,f"Failed: {e}")
    @channels.command(name="rename",description="Rename an existing channel")
    async def rename_channel(i:discord.Interaction,name:str):
        if not require(i,admin) or not i.user.guild_permissions.manage_channels: return await reply(i,"Manage Channels required.")
        try: await i.channel.edit(name=name[:100]);await reply(i,"Channel renamed.")
        except discord.HTTPException as e: await reply(i,f"Failed: {e}")
    @channels.command(name="nsfw",description="Enable or disable the NSFW flag")
    async def nsfw(i:discord.Interaction,enabled:bool):
        if not require(i,admin) or not i.user.guild_permissions.manage_channels or not isinstance(i.channel,discord.TextChannel): return await reply(i,"Manage Channels required.")
        try: await i.channel.edit(nsfw=enabled);await reply(i,f"NSFW flag: {enabled}.")
        except discord.HTTPException as e: await reply(i,f"Failed: {e}")
    @channels.command(name="info",description="Show channel details")
    async def channel_info(i:discord.Interaction):
        c=i.channel
        await reply(i,f"{c.name}\nID: {c.id}\nCategory: {c.category}\nType: {c.type}\nCreated: {discord.utils.format_dt(c.created_at,'F')}")
    @channels.command(name="position",description="Show the current channel position")
    async def position(i:discord.Interaction):
        await reply(i,f"Position: {i.channel.position}")
    @channels.command(name="category",description="Show the current channel category")
    async def category(i:discord.Interaction):
        await reply(i,i.channel.category.name if i.channel.category else "Uncategorised.")
    @channels.command(name="permissions",description="Show a member's channel permissions")
    async def channel_permissions(i:discord.Interaction,member:discord.Member=None):
        m=member or i.user
        await reply(i,", ".join(k.replace("_"," ") for k,v in i.channel.permissions_for(m) if v)[:1800])
    utility=group("tools","Useful everyday tools")
    @utility.command(name="choose",description="Choose randomly from comma-separated options")
    async def choose(i:discord.Interaction,options:str):
        parts=[x.strip() for x in options.split(",") if x.strip()]
        await reply(i,random.choice(parts) if parts else "Provide comma-separated options.")
    @utility.command(name="roll",description="Roll a die with custom sides")
    async def roll(i:discord.Interaction,sides:app_commands.Range[int,2,1000000]=6):
        await reply(i,f"Rolled {random.randint(1,sides)} / {sides}")
    @utility.command(name="coinflip",description="Flip a coin")
    async def coinflip(i:discord.Interaction):
        await reply(i,random.choice(["Heads","Tails"]))
    @utility.command(name="eightball",description="Ask the magic eight ball")
    async def eightball(i:discord.Interaction,question:str):
        await reply(i,random.choice(["Yes","No","Maybe","Ask again later","Very likely","Unlikely","Definitely","Cannot predict now"]))
    @utility.command(name="number",description="Pick a random number within a range")
    async def number(i:discord.Interaction,minimum:int,maximum:int):
        await reply(i,str(random.randint(minimum,maximum)) if minimum<=maximum and maximum-minimum<=1000000000 else "Invalid range.")
    @utility.command(name="reverse",description="Reverse text")
    async def reverse(i:discord.Interaction,text:str):
        await reply(i,text[:1000][::-1])
    @utility.command(name="uppercase",description="Convert text to uppercase")
    async def uppercase(i:discord.Interaction,text:str):
        await reply(i,text[:1000].upper())
    @utility.command(name="lowercase",description="Convert text to lowercase")
    async def lowercase(i:discord.Interaction,text:str):
        await reply(i,text[:1000].lower())
    @utility.command(name="count",description="Count characters and words")
    async def count(i:discord.Interaction,text:str):
        await reply(i,f"Characters: {len(text)} | Words: {len(text.split())}")
    @utility.command(name="timestamp",description="Create a Discord relative timestamp from Unix seconds")
    async def timestamp(i:discord.Interaction,unix_seconds:int):
        if not 0<=unix_seconds<=253402300799: return await reply(i,"Timestamp out of range.")
        await reply(i,f"<t:{unix_seconds}:F> (<t:{unix_seconds}:R>)")
    @utility.command(name="now",description="Show the current UTC time")
    async def now(i:discord.Interaction):
        await reply(i,discord.utils.format_dt(discord.utils.utcnow(),"F"))
    @utility.command(name="math",description="Calculate a basic arithmetic expression")
    async def math_cmd(i:discord.Interaction,first:float,operator:str,second:float):
        ops={"+":lambda a,b:a+b,"-":lambda a,b:a-b,"*":lambda a,b:a*b,"/":lambda a,b:a/b}
        if operator not in ops: return await reply(i,"Supported operators: + - * /")
        try: result=ops[operator](first,second);await reply(i,str(result))
        except (ZeroDivisionError,OverflowError): await reply(i,"Invalid arithmetic operation.")
    @utility.command(name="percentage",description="Calculate a percentage")
    async def percentage(i:discord.Interaction,value:float,percent:float):
        await reply(i,str(value*percent/100))
    @utility.command(name="difference",description="Calculate absolute difference")
    async def difference(i:discord.Interaction,first:float,second:float):
        await reply(i,str(abs(first-second)))
    @utility.command(name="repeat",description="Repeat a short piece of text")
    async def repeat(i:discord.Interaction,text:str,times:app_commands.Range[int,1,10]):
        await reply(i,(" ".join([text[:100]]*times))[:1800])
    @utility.command(name="poll",description="Create a simple public reaction poll")
    async def poll(i:discord.Interaction,question:str,options:str):
        parts=[x.strip() for x in options.split("|") if x.strip()]
        if len(parts)<2 or len(parts)>9: return await reply(i,"Provide 2-9 options separated by |.")
        if not i.channel.permissions_for(i.guild.me).add_reactions: return await reply(i,"Missing Add Reactions permission.")
        await i.response.defer(ephemeral=True)
        try:
            msg=await i.channel.send(embed=discord.Embed(title=question[:200],description="\n".join(f"{j+1}. {v[:150]}" for j,v in enumerate(parts)),colour=0xD4D4D4),allowed_mentions=discord.AllowedMentions.none())
            for j in range(len(parts)): await msg.add_reaction(f"{j+1}\N{COMBINING ENCLOSING KEYCAP}")
            await reply(i,"Poll posted.")
        except discord.HTTPException as e: await reply(i,f"Poll failed: {e}")
    @utility.command(name="say",description="Post a public message as the bot (staff)")
    async def say(i:discord.Interaction,channel:discord.TextChannel,message:str):
        if not require(i,staff): return await reply(i,"Staff only.")
        try: await channel.send(message[:1900],allowed_mentions=discord.AllowedMentions.none());await reply(i,"Message posted.")
        except discord.HTTPException as e: await reply(i,f"Failed: {e}")
    xp=group("levels","Experience and level tools")
    @xp.command(name="rank",description="View a member's XP and level")
    async def xp_rank(i:discord.Interaction,member:discord.Member=None):
        m=member or i.user
        await reply(i,f"{m.display_name}: level {get_level(i.guild.id,m.id)} | {get_xp(i.guild.id,m.id)} XP")
    @xp.command(name="top",description="Show the XP leaderboard")
    async def xp_top(i:discord.Interaction):
        rows=connection.execute("SELECT user,xp FROM xp WHERE guild=? ORDER BY xp DESC LIMIT 10",(i.guild.id,)).fetchall()
        await reply(i,"\n".join(f"{j}. <@{u}>: {v} XP" for j,(u,v) in enumerate(rows,1)) or "No XP recorded.")
    @xp.command(name="next",description="Show XP needed for the next level")
    async def xp_next(i:discord.Interaction,member:discord.Member=None):
        m=member or i.user
        lvl=get_level(i.guild.id,m.id)
        await reply(i,"Maximum level reached." if lvl>=150 else f"{m.display_name} needs {max(0,30*(lvl+1)**2-get_xp(i.guild.id,m.id))} XP for level {lvl+1}.")
    @xp.command(name="set",description="Set XP for a member (owner only)")
    async def xp_set(i:discord.Interaction,member:discord.Member,amount:app_commands.Range[int,0,100000000]):
        if i.user.id!=owner_id: return await reply(i,"Owner only.")
        connection.execute("INSERT INTO xp (guild,user,xp,last) VALUES (?,?,?,0) ON CONFLICT(guild,user) DO UPDATE SET xp=excluded.xp",(i.guild.id,member.id,amount));connection.commit()
        await reply(i,"XP updated.")
    @xp.command(name="add",description="Add XP to a member (owner only)")
    async def xp_add(i:discord.Interaction,member:discord.Member,amount:app_commands.Range[int,1,1000000]):
        if i.user.id!=owner_id: return await reply(i,"Owner only.")
        connection.execute("INSERT INTO xp (guild,user,xp,last) VALUES (?,?,?,0) ON CONFLICT(guild,user) DO UPDATE SET xp=xp+excluded.xp",(i.guild.id,member.id,amount));connection.commit()
        await reply(i,"XP added.")
    @xp.command(name="remove",description="Remove XP from a member (owner only)")
    async def xp_remove(i:discord.Interaction,member:discord.Member,amount:app_commands.Range[int,1,1000000]):
        if i.user.id!=owner_id: return await reply(i,"Owner only.")
        connection.execute("UPDATE xp SET xp=MAX(0,xp-?) WHERE guild=? AND user=?",(amount,i.guild.id,member.id));connection.commit()
        await reply(i,"XP removed.")
    @xp.command(name="reset",description="Reset a member's XP (owner only)")
    async def xp_reset(i:discord.Interaction,member:discord.Member):
        if i.user.id!=owner_id: return await reply(i,"Owner only.")
        connection.execute("DELETE FROM xp WHERE guild=? AND user=?",(i.guild.id,member.id));connection.commit()
        await reply(i,"XP reset.")
    settings=group("config","Inspect and update offhours settings")
    @settings.command(name="get",description="Read a bot setting")
    async def config_get(i:discord.Interaction,key:str):
        if not require(i,admin): return await reply(i,"Manage Server required.")
        if not re.fullmatch(r"[a-z0-9_-]{1,40}",key): return await reply(i,"Invalid key.")
        await reply(i,f"{key}: {get_setting(i.guild.id,config_key(key),'Not set')}")
    @settings.command(name="set",description="Set a bot setting")
    async def config_set(i:discord.Interaction,key:str,value:str):
        if not require(i,admin): return await reply(i,"Manage Server required.")
        if not re.fullmatch(r"[a-z0-9_-]{1,40}",key): return await reply(i,"Invalid key.")
        put_setting(i.guild.id,config_key(key),value[:500]);await reply(i,"Setting saved.")
    @settings.command(name="reset",description="Reset a bot setting")
    async def config_reset(i:discord.Interaction,key:str):
        if not require(i,admin): return await reply(i,"Manage Server required.")
        connection.execute("DELETE FROM settings WHERE guild=? AND key=?",(i.guild.id,config_key(key)));connection.commit()
        await reply(i,"Setting reset.")
    @settings.command(name="list",description="List extension settings")
    async def config_list(i:discord.Interaction):
        if not require(i,admin): return await reply(i,"Manage Server required.")
        rows=connection.execute("SELECT key FROM settings WHERE guild=? AND key LIKE 'ext:%' ORDER BY key",(i.guild.id,)).fetchall()
        await reply(i,", ".join(k[0][4:] for k in rows) or "No extension settings.")
    @settings.command(name="version",description="Show installed extension version")
    async def version(i:discord.Interaction):
        await reply(i,"offhours utility extension v1.0")
    @settings.command(name="health",description="Check bot readiness without sending public messages")
    async def health(i:discord.Interaction):
        if not require(i,admin): return await reply(i,"Manage Server required.")
        p=i.channel.permissions_for(i.guild.me)
        await reply(i,f"Connected: {bot.is_ready()} | Guild: {i.guild.name} | Send: {p.send_messages} | Embed: {p.embed_links} | React: {p.add_reactions}")
