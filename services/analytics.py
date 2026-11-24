"""Opt-in aggregate analytics. No message content is stored."""
import datetime as dt
import time
from collections import defaultdict

import discord
from discord.ext import tasks


class AnalyticsService:
    def __init__(self,bot,connection,get_setting,put_setting):
        self.bot,self.db,self.get,self.put=bot,connection,get_setting,put_setting
        connection.executescript('''
        CREATE TABLE IF NOT EXISTS activity_metrics (
            guild INTEGER NOT NULL,day TEXT NOT NULL,hour INTEGER NOT NULL,
            metric TEXT NOT NULL,kind TEXT NOT NULL,target TEXT NOT NULL,
            value INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(guild,day,hour,metric,kind,target));
        CREATE INDEX IF NOT EXISTS activity_metrics_lookup ON activity_metrics(guild,metric,day);
        ''');connection.commit()
        self.voice_sessions={}

    def enabled(self,guild):return self.get(guild,'module:analytics','0')=='1'

    def excluded(self,member,channel=None):
        if member.bot:return True
        roles=set(self.get(member.guild.id,'analytics:ignored_roles','').split(','))
        channels=set(self.get(member.guild.id,'analytics:ignored_channels','').split(','))
        return any(str(r.id) in roles for r in member.roles) or (channel and str(channel.id) in channels)

    def add(self,guild,metric,amount=1,member=None,channel=None,target=None,commit=True):
        if not self.enabled(guild):return
        now=discord.utils.utcnow()
        destinations=[('guild','0')]
        if member:destinations.append(('member',str(member)))
        if channel:destinations.append(('channel',str(channel)))
        if target:destinations.append(('command',str(target)))
        for kind,identifier in destinations:
            self.db.execute('''INSERT INTO activity_metrics(guild,day,hour,metric,kind,target,value)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(guild,day,hour,metric,kind,target)
                DO UPDATE SET value=value+excluded.value''',(guild,now.date().isoformat(),now.hour,metric,kind,identifier,int(amount)))
        if commit:self.db.commit()

    def query(self,guild,metric,days=7,kind='guild',target='0'):
        cutoff=(discord.utils.utcnow().date()-dt.timedelta(days=days-1)).isoformat()
        return self.db.execute('SELECT COALESCE(SUM(value),0) FROM activity_metrics WHERE guild=? AND metric=? AND day>=? AND kind=? AND target=?',
            (guild,metric,cutoff,kind,str(target))).fetchone()[0]

    def ranking(self,guild,metric,days,kind):
        cutoff=(discord.utils.utcnow().date()-dt.timedelta(days=days-1)).isoformat()
        return self.db.execute('SELECT target,SUM(value) FROM activity_metrics WHERE guild=? AND metric=? AND day>=? AND kind=? GROUP BY target ORDER BY SUM(value) DESC LIMIT 15',
            (guild,metric,cutoff,kind)).fetchall()

    async def on_message(self,message):
        if message.guild and isinstance(message.author,discord.Member) and not self.excluded(message.author,message.channel):
            self.add(message.guild.id,'messages',member=message.author.id,channel=message.channel.id)

    async def on_member_join(self,member):
        if not member.bot:self.add(member.guild.id,'joins')

    async def on_member_remove(self,member):
        if not member.bot:self.add(member.guild.id,'leaves')
        self.end_voice(member.guild.id,member.id)

    async def on_app_command_completion(self,interaction,command):
        if interaction.guild and isinstance(interaction.user,discord.Member) and not self.excluded(interaction.user,interaction.channel):
            self.add(interaction.guild_id,'commands',member=interaction.user.id,target='/'+command.qualified_name)

    async def on_command_completion(self,ctx):
        if ctx.guild and not self.excluded(ctx.author,ctx.channel):
            self.add(ctx.guild.id,'commands',member=ctx.author.id,target='?'+ctx.command.qualified_name)

    def eligible_voice(self,member,state):
        if not state or not state.channel or not self.enabled(member.guild.id) or self.excluded(member,state.channel):return False
        if state.channel.id==member.guild.afk_channel_id or state.self_deaf or state.deaf:return False
        active=[m for m in state.channel.members if not m.bot and m.voice and not m.voice.self_deaf and not m.voice.deaf]
        return len(active)>=2

    def end_voice(self,guild,user):
        session=self.voice_sessions.pop((guild,user),None)
        if session:
            channel,started=session
            elapsed=max(0,int(time.monotonic()-started))
            if elapsed:self.add(guild,'voice_seconds',elapsed,member=user,channel=channel)

    async def on_voice_state_update(self,member,before,after):
        self.end_voice(member.guild.id,member.id)
        # Reevaluate both channels: a second person arriving enables both sessions,
        # and the last companion leaving ends the remaining person's accrual.
        channels={c.id:c for c in (before.channel,after.channel) if c}
        for channel in channels.values():
            for other in channel.members:
                self.end_voice(other.guild.id,other.id)
                if self.eligible_voice(other,other.voice):
                    self.voice_sessions[(other.guild.id,other.id)]=(channel.id,time.monotonic())

    @tasks.loop(seconds=60)
    async def flush(self):
        now=time.monotonic()
        for (guild_id,user),(channel,started) in list(self.voice_sessions.items()):
            guild=self.bot.get_guild(guild_id);member=guild.get_member(user) if guild else None
            if not member or not self.eligible_voice(member,member.voice):
                self.end_voice(guild_id,user)
                continue
            elapsed=max(0,int(now-started))
            if elapsed:
                self.add(guild_id,'voice_seconds',elapsed,member=user,channel=channel,commit=False)
                self.voice_sessions[(guild_id,user)]=(channel,now)
        self.db.commit()
        for guild in self.bot.guilds:
            try:days=max(7,min(365,int(self.get(guild.id,'analytics:retention_days','90'))))
            except ValueError:days=90
            cutoff=(discord.utils.utcnow().date()-dt.timedelta(days=days)).isoformat()
            self.db.execute('DELETE FROM activity_metrics WHERE guild=? AND day<?',(guild.id,cutoff))
        self.db.commit()

    @flush.before_loop
    async def before_flush(self):await self.bot.wait_until_ready()

    async def ready(self):
        if not self.flush.is_running():
            for guild in self.bot.guilds:
                if not self.enabled(guild.id):continue
                for channel in guild.voice_channels:
                    for member in channel.members:
                        if self.eligible_voice(member,member.voice):
                            self.voice_sessions[(guild.id,member.id)]=(channel.id,time.monotonic())
            self.flush.start()

    async def close(self):
        self.flush.cancel()
        for guild,user in list(self.voice_sessions):self.end_voice(guild,user)
