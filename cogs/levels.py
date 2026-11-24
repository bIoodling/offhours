"""Existing XP schema with per-server settings, reward roles and anti-AFK voice XP."""
import asyncio
import contextlib
import io
import json
import math
import random
import time

import discord
from discord import app_commands
from discord.ext import tasks

from services.configuration import administrator, enabled, channel_for, private
from cogs.selfroles import safe_reward_role


class Levels:
    def __init__(self, bot, con, get, put):
        self.bot, self.con, self.get, self.put = bot, con, get, put
        self.messages = {}

    def xp(self, guild, user):
        row = self.con.execute('SELECT xp FROM xp WHERE guild=? AND user=?',(guild,user)).fetchone()
        return row[0] if row else 0

    def level(self, xp):
        return min(150, math.isqrt(max(0,xp)//30))

    async def add(self, member, amount, *, message_activity=False):
        row = self.con.execute('SELECT xp,last FROM xp WHERE guild=? AND user=?', (member.guild.id,member.id)).fetchone()
        before, last_message = row if row else (0, 0)
        if self.level(before) >= 150:
            return
        after = max(0,min(100000000,before+amount))
        self.con.execute('INSERT INTO xp(guild,user,xp,last) VALUES(?,?,?,?) ON CONFLICT(guild,user) DO UPDATE SET xp=excluded.xp,last=excluded.last',
                         (member.guild.id,member.id,after,time.time() if message_activity else last_message))
        self.con.commit()
        old, new = self.level(before), self.level(after)
        if new > old:
            rewards = json.loads(self.get(member.guild.id,'levels:rewards','{}'))
            for threshold, role_id in rewards.items():
                if old < int(threshold) <= new:
                    role = member.guild.get_role(int(role_id))
                    if safe_reward_role(role, member.guild.me):
                        with contextlib.suppress(discord.HTTPException):
                            await member.add_roles(role,reason='Configured XP milestone reward')
            if old < 5 <= new:
                ids = {int(v) for k,v in self.con.execute('SELECT key,value FROM settings WHERE guild=? AND key LIKE ?',
                    (member.guild.id,'selfrole:colour:%')) if v.isdecimal()}
                remove = [r for r in member.roles if r.id in ids and r < member.guild.me.top_role]
                if remove:
                    with contextlib.suppress(discord.HTTPException):
                        await member.remove_roles(*remove,reason='Level rewards replace starter colours')
            panels = getattr(self.bot,'panels_service',None)
            if panels:
                with contextlib.suppress(discord.HTTPException):
                    await panels.emit('level',member,level=new,xp=after)

    async def message(self, message):
        if not message.guild or message.author.bot or not isinstance(message.author,discord.Member):
            return
        guild, member = message.guild, message.author
        if not enabled(self.get,guild.id,'levels') or message.content.startswith(self.get(guild.id,'prefix','?')):
            return
        ignored_channels = set(json.loads(self.get(guild.id,'levels:ignored_channels','[]')))
        ignored_roles = set(json.loads(self.get(guild.id,'levels:ignored_roles','[]')))
        if message.channel.id in ignored_channels or any(r.id in ignored_roles for r in member.roles):
            return
        minimum = int(self.get(guild.id,'levels:link_level','0'))
        general = channel_for(guild,self.get,'general')
        if general and general.id == message.channel.id and minimum and self.level(self.xp(guild.id,member.id)) < minimum and not member.guild_permissions.manage_messages:
            import re
            if re.search(r'https?://|www\.|discord\.gg/',message.content,re.I) or message.attachments:
                with contextlib.suppress(discord.HTTPException):
                    await message.delete()
                return
        now = time.time()
        row = self.con.execute('SELECT last FROM xp WHERE guild=? AND user=?',(guild.id,member.id)).fetchone()
        cooldown = max(5,int(self.get(guild.id,'levels:cooldown','45')))
        text = message.content.strip()
        key = (guild.id,member.id)
        if len(text) < 8 or now - (row[0] if row else 0) < cooldown or self.messages.get(key) == text:
            return
        self.messages[key] = text
        if len(self.messages) > 10000:
            self.messages.clear()
        multiplier = max(0,min(10,float(self.get(guild.id,'levels:multiplier','1'))))
        await self.add(member,round(random.randint(12,18)*multiplier), message_activity=True)

    @tasks.loop(minutes=1)
    async def voice(self):
        for guild in self.bot.guilds:
            if not enabled(self.get,guild.id,'levels'):
                continue
            amount = int(self.get(guild.id,'levels:voice_xp','0'))
            if amount <= 0:
                continue
            ignored_channels = set(json.loads(self.get(guild.id,'levels:ignored_channels','[]')))
            ignored_roles = set(json.loads(self.get(guild.id,'levels:ignored_roles','[]')))
            for channel in guild.voice_channels:
                if channel == guild.afk_channel or channel.id in ignored_channels:
                    continue
                active = [m for m in channel.members if not m.bot and m.voice and not any((m.voice.afk,m.voice.self_deaf,m.voice.deaf,m.voice.mute,m.voice.self_mute,m.voice.suppress))
                          and not any(r.id in ignored_roles for r in m.roles)]
                if len(active) < 2:
                    continue
                for member in active:
                    await self.add(member,min(20,amount))

    @voice.before_loop
    async def ready(self):
        await self.bot.wait_until_ready()

    async def close(self):
        self.voice.cancel()


def install(bot, con, get, put):
    service = Levels(bot,con,get,put)
    bot.add_listener(service.message,'on_message')
    async def ready():
        if not service.voice.is_running():
            service.voice.start()
    bot.add_listener(ready,'on_ready')
    group = bot.tree.get_command('levels')
    if not group:
        group = app_commands.Group(name='levels',description='XP progression and rewards')
        bot.tree.add_command(group)

    async def admin(i):
        if not administrator(i.user):
            await private(i,'Server Administrator permission is required.')
            return False
        return True

    @group.command(name='rewards',description='List this server’s configured XP milestone roles')
    async def rewards(i:discord.Interaction):
        data=json.loads(get(i.guild_id,'levels:rewards','{}'))
        await private(i,'\n'.join(f'Level {k}: <@&{v}>' for k,v in sorted(data.items(),key=lambda p:int(p[0]))) or 'No reward roles configured.')

    @group.command(name='reward-add',description='Assign an existing role to an XP milestone')
    async def reward_add(i:discord.Interaction,milestone:app_commands.Range[int,1,150],role:discord.Role):
        if not await admin(i):return
        if not safe_reward_role(role, i.guild.me):
            return await private(i,'Choose an existing role below the bot without moderation or administrative permissions.')
        data=json.loads(get(i.guild_id,'levels:rewards','{}'));data[str(milestone)]=role.id
        put(i.guild_id,'levels:rewards',json.dumps(data));await private(i,'XP reward saved.')

    @group.command(name='reward-remove',description='Remove a configured XP reward')
    async def reward_remove(i:discord.Interaction,milestone:app_commands.Range[int,1,150]):
        if not await admin(i):return
        data=json.loads(get(i.guild_id,'levels:rewards','{}'));data.pop(str(milestone),None)
        put(i.guild_id,'levels:rewards',json.dumps(data));await private(i,'Reward removed from configuration.')

    @group.command(name='multiplier',description='Set the server’s message XP multiplier')
    async def multiplier(i:discord.Interaction,value:app_commands.Range[float,0,10]):
        if await admin(i):put(i.guild_id,'levels:multiplier',value);await private(i,'XP multiplier saved.')

    @group.command(name='cooldown',description='Set seconds between message XP awards')
    async def cooldown(i:discord.Interaction,seconds:app_commands.Range[int,5,3600]):
        if await admin(i):put(i.guild_id,'levels:cooldown',seconds);await private(i,'XP cooldown saved.')

    @group.command(name='voice',description='Configure XP per active voice minute, zero disables')
    async def voice(i:discord.Interaction,xp_per_minute:app_commands.Range[int,0,20]):
        if await admin(i):put(i.guild_id,'levels:voice_xp',xp_per_minute);await private(i,'Voice XP saved. At least two active humans are required.')

    @group.command(name='ignore-channel',description='Exclude or restore a channel for XP')
    async def ignore_channel(i:discord.Interaction,channel:discord.TextChannel | discord.VoiceChannel | discord.StageChannel,ignored:bool=True):
        if not await admin(i):return
        ids=set(json.loads(get(i.guild_id,'levels:ignored_channels','[]')))
        ids.add(channel.id) if ignored else ids.discard(channel.id)
        put(i.guild_id,'levels:ignored_channels',json.dumps(sorted(ids)));await private(i,'XP channel policy saved.')

    @group.command(name='ignore-role',description='Exclude or restore a role for XP')
    async def ignore_role(i:discord.Interaction,role:discord.Role,ignored:bool=True):
        if not await admin(i):return
        ids=set(json.loads(get(i.guild_id,'levels:ignored_roles','[]')))
        ids.add(role.id) if ignored else ids.discard(role.id)
        put(i.guild_id,'levels:ignored_roles',json.dumps(sorted(ids)));await private(i,'XP role policy saved.')

    @group.command(name='enabled',description='Enable or disable this server’s leveling')
    async def active(i:discord.Interaction,value:bool):
        if await admin(i):put(i.guild_id,'module:levels','1' if value else '0');await private(i,'Leveling setting saved.')

    @group.command(name='unlocks',description='Configure reaction and general-link XP thresholds')
    async def unlocks(i:discord.Interaction,reactions:app_commands.Range[int,0,150],links:app_commands.Range[int,0,150]):
        if not await admin(i):return
        put(i.guild_id,'levels:reaction_level',reactions);put(i.guild_id,'levels:link_level',links)
        await private(i,'Progression thresholds saved for configured channels.')

    @group.command(name='export',description='Download this server’s XP data')
    async def export(i:discord.Interaction):
        if not await admin(i):return
        rows=[{'user':str(u),'xp':xp,'last':last} for u,xp,last in con.execute('SELECT user,xp,last FROM xp WHERE guild=?',(i.guild_id,))]
        payload=json.dumps({'guild':str(i.guild_id),'users':rows}).encode()
        await private(i,file=discord.File(io.BytesIO(payload),filename='xp-backup.json'))

    @group.command(name='import',description='Merge a same-server XP backup, preserving higher existing XP')
    async def import_xp(i:discord.Interaction,attachment:discord.Attachment):
        if not await admin(i):return
        if attachment.size>2000000:return await private(i,'Maximum backup size is 2 MB.')
        await i.response.defer(ephemeral=True)
        try:
            payload=json.loads(await attachment.read())
            if str(payload['guild'])!=str(i.guild_id):raise ValueError
            users=payload['users']
            if not isinstance(users,list) or len(users)>50000:raise ValueError
            rows=[]
            for row in users:
                user,amount=int(row['user']),int(row['xp'])
                if user<=0 or user>2**63-1 or not 0<=amount<=100000000:raise ValueError
                rows.append((i.guild_id,user,amount,0))
        except (ValueError,KeyError,TypeError):return await private(i,'Invalid same-server XP backup.')
        con.executemany('INSERT INTO xp(guild,user,xp,last) VALUES(?,?,?,?) ON CONFLICT(guild,user) DO UPDATE SET xp=MAX(xp.xp,excluded.xp)',rows)
        con.commit();await private(i,f'Merged {len(rows)} XP rows. Existing higher XP was preserved.')

    @group.command(name='card',description='Render a member’s rank card as an image')
    async def card(i:discord.Interaction,member:discord.Member=None):
        member=member or i.user
        from PIL import Image,ImageDraw
        image=Image.new('RGB',(700,190),(24,24,27));draw=ImageDraw.Draw(image)
        xp=service.xp(i.guild_id,member.id);level=service.level(xp)
        progress=1 if level==150 else (xp-30*level*level)/(30*(level+1)**2-30*level*level)
        draw.text((30,30),member.display_name[:50],fill=(240,240,240),font_size=28)
        draw.text((30,80),f'Level {level} / 150     {xp:,} XP',fill=(190,195,205),font_size=22)
        draw.rounded_rectangle((30,135,670,160),radius=10,fill=(52,52,58))
        draw.rounded_rectangle((30,135,30+max(20,int(640*max(0,min(1,progress)))),160),radius=10,fill=(135,160,180))
        output=io.BytesIO();image.save(output,format='PNG');output.seek(0)
        await private(i,file=discord.File(output,filename='rank.png'))

    async def reaction(payload):
        if not payload.guild_id or not enabled(get,payload.guild_id,'levels'):return
        minimum=int(get(payload.guild_id,'levels:reaction_level','0'))
        if not minimum:return
        guild=bot.get_guild(payload.guild_id)
        if not guild:return
        member=payload.member or guild.get_member(payload.user_id)
        if not member or member.bot or member.guild_permissions.manage_messages:return
        ids={int(get(guild.id,'channel:'+name,'0')) for name in ('general','media','clips')}
        if payload.channel_id not in ids or service.level(service.xp(guild.id,member.id))>=minimum:return
        channel=guild.get_channel(payload.channel_id)
        if channel:
            with contextlib.suppress(discord.HTTPException):
                message=await channel.fetch_message(payload.message_id);await message.remove_reaction(payload.emoji,member)
    bot.add_listener(reaction,'on_raw_reaction_add')
    return service
