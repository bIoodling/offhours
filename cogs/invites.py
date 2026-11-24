"""Best-effort invite attribution; ambiguity is recorded rather than invented."""
import asyncio
from collections import defaultdict
import contextlib
import io
import json
import time

import discord
from discord import app_commands
from services.configuration import administrator, enabled, private
from cogs.selfroles import safe_reward_role


class Invites:
    def __init__(self,bot,con,get,put):
        self.bot,self.con,self.get,self.put=bot,con,get,put
        self.cache={};self.locks=defaultdict(asyncio.Lock)
        con.executescript('''
        CREATE TABLE IF NOT EXISTS invite_joins(guild INTEGER,user INTEGER,inviter INTEGER,code TEXT,joined REAL,left_at REAL,PRIMARY KEY(guild,user));
        CREATE TABLE IF NOT EXISTS invite_bonus(guild INTEGER,user INTEGER,amount INTEGER DEFAULT 0,PRIMARY KEY(guild,user));
        CREATE INDEX IF NOT EXISTS invite_joins_inviter ON invite_joins(guild,inviter);
        CREATE TABLE IF NOT EXISTS invite_events(id INTEGER PRIMARY KEY AUTOINCREMENT,guild INTEGER NOT NULL,user INTEGER NOT NULL,
          inviter INTEGER NOT NULL,code TEXT NOT NULL,joined REAL NOT NULL,left_at REAL,UNIQUE(guild,user,joined));
        CREATE INDEX IF NOT EXISTS invite_events_inviter ON invite_events(guild,inviter,left_at);
        INSERT OR IGNORE INTO invite_events(guild,user,inviter,code,joined,left_at)
          SELECT guild,user,inviter,COALESCE(code,''),joined,left_at FROM invite_joins WHERE joined IS NOT NULL;
        ''');con.commit()

    async def sync(self,guild):
        invites=await guild.invites();self.cache[guild.id]={i.code:(i.uses,i.inviter.id if i.inviter else 0) for i in invites}
        return invites

    def count(self,guild,user):
        joined,left=self.con.execute('SELECT COUNT(*),COALESCE(SUM(left_at IS NOT NULL),0) FROM invite_events WHERE guild=? AND inviter=?',(guild,user)).fetchone()
        bonus=self.con.execute('SELECT amount FROM invite_bonus WHERE guild=? AND user=?',(guild,user)).fetchone()
        return joined,left,bonus[0] if bonus else 0

    async def reward(self, member):
        if not member or member.bot:
            return
        joined, left, bonus = self.count(member.guild.id, member.id)
        total = max(0, joined - left + bonus)
        for threshold, role_id in json.loads(self.get(member.guild.id, 'invites:rewards', '{}')).items():
            role = member.guild.get_role(int(role_id))
            if total >= int(threshold) and safe_reward_role(role, member.guild.me) and role not in member.roles:
                with contextlib.suppress(discord.HTTPException):
                    await member.add_roles(role, reason='Configured active-invite milestone reward')

    async def joined(self,member):
        if member.bot or not enabled(self.get,member.guild.id,'invites'):return {}
        async with self.locks[member.guild.id]:
            previous=self.cache.get(member.guild.id)
            inviter=0;code=''
            try:
                current=await self.sync(member.guild)
                increased=[i for i in current if previous and i.code in previous and (i.uses or 0)-(previous[i.code][0] or 0)==1]
                total=sum(max(0,(i.uses or 0)-(previous.get(i.code,(0,0))[0] or 0)) for i in current) if previous else 0
                disappeared = set(previous or {}) - {invite.code for invite in current}
                if total==1 and len(increased)==1 and not disappeared:
                    invite=increased[0];inviter=invite.inviter.id if invite.inviter else 0;code=invite.code
            except discord.HTTPException:pass
            joined_at = time.time()
            # A fresh join proves any previously active membership ended. Zero denotes
            # an inferred departure with an unavailable exact leave time; no timestamp is invented.
            self.con.execute('UPDATE invite_events SET left_at=0 WHERE guild=? AND user=? AND left_at IS NULL',
                             (member.guild.id, member.id))
            self.con.execute('INSERT INTO invite_joins(guild,user,inviter,code,joined,left_at) VALUES(?,?,?,?,?,NULL) ON CONFLICT(guild,user) DO UPDATE SET inviter=excluded.inviter,code=excluded.code,joined=excluded.joined,left_at=NULL',
                             (member.guild.id,member.id,inviter,code,joined_at))
            self.con.execute('INSERT INTO invite_events(guild,user,inviter,code,joined) VALUES(?,?,?,?,?)',
                             (member.guild.id, member.id, inviter, code, joined_at))
            self.con.commit()
            inviter_member=member.guild.get_member(inviter) if inviter else None
            if inviter and not inviter_member:
                with contextlib.suppress(discord.HTTPException):
                    inviter_member=await member.guild.fetch_member(inviter)
            joins,leaves,bonus=self.count(member.guild.id,inviter) if inviter else (0,0,0)
            if inviter_member:
                await self.reward(inviter_member)
            variables={'inviter.name':inviter_member.display_name if inviter_member else 'Unknown / ambiguous',
                'inviter.mention':inviter_member.mention if inviter_member else 'Unknown','inviter.invites':joins-leaves+bonus,'invite.code':code or 'Unknown'}
            panels=getattr(self.bot,'panels_service',None)
            if panels:
                with contextlib.suppress(discord.HTTPException):
                    await panels.emit('invite',member,**variables)
            return variables

    async def left(self,member):
        if not enabled(self.get,member.guild.id,'invites'):return
        async with self.locks[member.guild.id]:
            now=time.time()
            self.con.execute('UPDATE invite_joins SET left_at=? WHERE guild=? AND user=? AND left_at IS NULL',(now,member.guild.id,member.id))
            self.con.execute('UPDATE invite_events SET left_at=? WHERE guild=? AND user=? AND left_at IS NULL',(now,member.guild.id,member.id))
            self.con.commit()


def install(bot,con,get,put):
    service=Invites(bot,con,get,put)
    bot.add_listener(service.left,'on_member_remove')
    async def ready():
        for guild in bot.guilds:
            if enabled(get,guild.id,'invites'):
                with contextlib.suppress(discord.HTTPException):await service.sync(guild)
    bot.add_listener(ready,'on_ready')
    async def invite_changed(invite):
        if invite.guild and enabled(get,invite.guild.id,'invites'):
            with contextlib.suppress(discord.HTTPException):await service.sync(invite.guild)
    bot.add_listener(invite_changed,'on_invite_create');bot.add_listener(invite_changed,'on_invite_delete')
    group=app_commands.Group(name='invites',description='Invite tracking and existing invite management');bot.tree.add_command(group)

    @group.command(name='stats',description='View recorded joins, leaves and bonus invites')
    async def stats(i:discord.Interaction,member:discord.Member=None):
        member=member or i.user;joins,leaves,bonus=service.count(i.guild_id,member.id)
        await private(i,f'{member.display_name}: {joins} attributed joins, {leaves} leaves, {bonus} bonus. Active total: {joins-leaves+bonus}.\nAttribution begins when enabled and remains unknown when Discord data is ambiguous.')

    @group.command(name='leaderboard',description='Show recorded invite leaders in this server')
    async def leaderboard(i:discord.Interaction):
        rows=con.execute('''SELECT user,SUM(amount) AS n FROM (
          SELECT inviter AS user,COUNT(*)-SUM(left_at IS NOT NULL) AS amount FROM invite_events WHERE guild=? AND inviter>0 GROUP BY inviter
          UNION ALL SELECT user,amount FROM invite_bonus WHERE guild=?) GROUP BY user ORDER BY n DESC LIMIT 15''',(i.guild_id,i.guild_id)).fetchall()
        await private(i,'\n'.join(f'{n}. <@{u}>: {count}' for n,(u,count) in enumerate(rows,1)) or 'No attributed invites recorded.')

    @group.command(name='inviter',description='Inspect the recorded inviter for a member')
    async def inviter(i:discord.Interaction,member:discord.Member=None):
        member=member or i.user
        row=con.execute('SELECT inviter,code FROM invite_joins WHERE guild=? AND user=?',(i.guild_id,member.id)).fetchone()
        await private(i,f'Inviter: <@{row[0]}> (code {row[1]})' if row and row[0] else 'Inviter unknown: unavailable, pre-tracking or ambiguous invite data.')

    @group.command(name='list',description='List current server invites with usage')
    async def list_invites(i:discord.Interaction):
        if not i.user.guild_permissions.manage_guild:return await private(i,'Manage Server permission required.')
        rows=await service.sync(i.guild)
        await private(i,'\n'.join(f'{x.code}: {x.uses} uses, creator {x.inviter}' for x in rows[:20]) or 'No current invites.')

    @group.command(name='create',description='Create an invite to a selected existing channel')
    async def create(i:discord.Interaction,channel:discord.TextChannel,max_uses:app_commands.Range[int,0,100]=0,hours:app_commands.Range[int,0,168]=24):
        if not channel.permissions_for(i.user).create_instant_invite:return await private(i,'Create Invite permission required in that channel.')
        invite=await channel.create_invite(max_age=hours*3600,max_uses=max_uses,unique=True,reason=f'Invite requested by {i.user.id}')
        await private(i,invite.url)

    @group.command(name='delete',description='Delete a specific invite belonging to this server')
    async def delete(i:discord.Interaction,code:str):
        if not i.user.guild_permissions.manage_guild:return await private(i,'Manage Server permission required.')
        rows=await i.guild.invites();match=next((x for x in rows if x.code==code.strip().rsplit('/',1)[-1]),None)
        if not match:return await private(i,'Invite not found in this server.')
        await match.delete(reason=f'Requested by {i.user.id}');await private(i,'Invite deleted.')

    @group.command(name='bonus',description='Adjust an existing member’s recorded bonus invites')
    async def bonus(i:discord.Interaction,member:discord.Member,adjustment:app_commands.Range[int,-10000,10000]):
        if not administrator(i.user):return await private(i,'Server Administrator permission required.')
        con.execute('INSERT INTO invite_bonus(guild,user,amount) VALUES(?,?,?) ON CONFLICT(guild,user) DO UPDATE SET amount=invite_bonus.amount+excluded.amount',(i.guild_id,member.id,adjustment));con.commit()
        await service.reward(member)
        await private(i,'Bonus invite adjustment saved.')

    @group.command(name='tracking',description='Enable or disable this server’s invite attribution')
    async def tracking(i:discord.Interaction,active:bool):
        if not administrator(i.user):return await private(i,'Server Administrator permission required.')
        if active:
            await service.sync(i.guild)
        put(i.guild_id,'module:invites','1' if active else '0');await private(i,'Invite tracking configuration saved.')

    @group.command(name='sync',description='Refresh invite counters without inventing historical attribution')
    async def sync(i:discord.Interaction):
        if not i.user.guild_permissions.manage_guild:return await private(i,'Manage Server permission required.')
        rows=await service.sync(i.guild);await private(i,f'Refreshed {len(rows)} invite counters. Historical joins were not fabricated.')

    @group.command(name='export',description='Download this server’s recorded invite events')
    async def export(i:discord.Interaction):
        if not administrator(i.user):return await private(i,'Server Administrator permission required.')
        rows=[dict(user=str(u),inviter=str(v),code=c,joined=j,left=l) for u,v,c,j,l in con.execute('SELECT user,inviter,code,joined,left_at FROM invite_events WHERE guild=? ORDER BY joined LIMIT 25000',(i.guild_id,))]
        payload=json.dumps({'guild':str(i.guild_id),'events':rows,'note':'left=0 means departure inferred from a later rejoin; exact leave time unavailable.'}).encode()
        if len(payload)>min(i.guild.filesize_limit-1000,8000000):return await private(i,'Invite export exceeds the safe upload limit.')
        await private(i,file=discord.File(io.BytesIO(payload),filename='invites.json'))

    @group.command(name='rewards',description='List existing roles configured as active-invite milestone rewards')
    async def rewards(i:discord.Interaction):
        data=json.loads(get(i.guild_id,'invites:rewards','{}'))
        await private(i,'\n'.join(f'{count} active invites: <@&{role}>' for count,role in sorted(data.items(),key=lambda row:int(row[0]))) or 'No invite reward roles configured.')

    @group.command(name='reward-add',description='Configure an existing role earned at an active-invite milestone')
    async def reward_add(i:discord.Interaction,invites:app_commands.Range[int,1,10000],role:discord.Role):
        if not administrator(i.user):return await private(i,'Server Administrator permission required.')
        if not safe_reward_role(role,i.guild.me):return await private(i,'Choose an existing assignable role without moderation or administrative permissions.')
        data=json.loads(get(i.guild_id,'invites:rewards','{}'));data[str(invites)]=role.id
        put(i.guild_id,'invites:rewards',json.dumps(data));await private(i,'Invite reward saved. It is evaluated after attributed joins and bonus updates; previously earned roles are not automatically removed.')

    @group.command(name='reward-remove',description='Stop awarding one configured invite milestone role')
    async def reward_remove(i:discord.Interaction,invites:app_commands.Range[int,1,10000]):
        if not administrator(i.user):return await private(i,'Server Administrator permission required.')
        data=json.loads(get(i.guild_id,'invites:rewards','{}'));data.pop(str(invites),None)
        put(i.guild_id,'invites:rewards',json.dumps(data));await private(i,'Invite reward removed from configuration. Existing member roles are preserved.')
    return service
