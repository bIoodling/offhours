"""Read and configure actual collected server activity."""
import datetime as dt
import functools
import io
import json

import discord
from discord import app_commands

from services.analytics import AnalyticsService


def install(bot,connection,get_setting,put_setting):
    service=AnalyticsService(bot,connection,get_setting,put_setting)
    db=connection
    group=app_commands.Group(name='analytics',description='Opt-in server activity and statistics',guild_only=True)
    bot.tree.remove_command('analytics');bot.tree.add_command(group)

    async def reply(i,text=None,**kwargs):
        if text is not None:text=str(text)[:1900]
        kwargs.setdefault('ephemeral',True);kwargs.setdefault('allowed_mentions',discord.AllowedMentions.none())
        if i.response.is_done():await i.followup.send(text,**kwargs)
        else:await i.response.send_message(text,**kwargs)

    def admin(i):
        if not i.guild or not i.user.guild_permissions.manage_guild:raise ValueError('Manage Server permission is required.')

    def register(name,description):
        def decorator(fn):
            @functools.wraps(fn)
            async def callback(i,*args,**kwargs):
                try:
                    admin(i)
                    await fn(i,*args,**kwargs)
                except ValueError as exc:await reply(i,str(exc))
                except discord.HTTPException as exc:await reply(i,f'Discord rejected the request (HTTP {exc.status}).')
            group.add_command(app_commands.Command(name=name,description=description,callback=callback))
            return fn
        return decorator

    def collected_notice(guild):
        return '\nCollection is currently disabled.' if not service.enabled(guild) else '\nCounts cover observed events only; no historical backfill is inferred.'

    @register('overview','Read recorded message, join, leave, command and voice totals')
    async def overview(i:discord.Interaction,days:app_commands.Range[int,1,365]=7):
        lines=[f'Observed totals for the last {days} UTC calendar days:']
        for metric,label in [('messages','Messages'),('joins','Joins'),('leaves','Leaves'),('commands','Commands')]:
            lines.append(f'{label}: {service.query(i.guild_id,metric,days)}')
        lines.append(f'Active voice minutes: {service.query(i.guild_id,"voice_seconds",days)//60}')
        await reply(i,'\n'.join(lines)+collected_notice(i.guild_id))

    @register('messages','Read a member’s or channel’s collected message count')
    async def messages(i:discord.Interaction,days:app_commands.Range[int,1,365]=7,member:discord.Member=None,channel:discord.TextChannel=None):
        if member and channel:raise ValueError('Choose a member or channel, not both.')
        kind,target=('member',member.id) if member else ('channel',channel.id) if channel else ('guild',0)
        await reply(i,f'{service.query(i.guild_id,"messages",days,kind,target)} recorded messages in {days} UTC calendar days.'+collected_notice(i.guild_id))

    @register('members','Read current cached server membership and account age distribution')
    async def members(i:discord.Interaction):
        humans=[m for m in i.guild.members if not m.bot]
        week=discord.utils.utcnow()-dt.timedelta(days=7)
        fresh=sum(m.created_at>=week for m in humans)
        await reply(i,f'Discord member total: {i.guild.member_count}\nCached humans: {len(humans)}\nCached bots: {sum(m.bot for m in i.guild.members)}\nCached humans with accounts younger than seven days: {fresh}')

    @register('joins','Read observed joins per UTC calendar day')
    async def joins(i:discord.Interaction,days:app_commands.Range[int,1,90]=7):
        cutoff=(discord.utils.utcnow().date()-dt.timedelta(days=days-1)).isoformat()
        rows=db.execute("SELECT day,SUM(value) FROM activity_metrics WHERE guild=? AND metric='joins' AND kind='guild' AND day>=? GROUP BY day ORDER BY day DESC LIMIT 30",(i.guild_id,cutoff)).fetchall()
        await reply(i,'\n'.join(f'{day}: {value}' for day,value in rows) or 'No observed joins in this period.')

    @register('leaves','Read observed departures per UTC calendar day')
    async def leaves(i:discord.Interaction,days:app_commands.Range[int,1,90]=7):
        cutoff=(discord.utils.utcnow().date()-dt.timedelta(days=days-1)).isoformat()
        rows=db.execute("SELECT day,SUM(value) FROM activity_metrics WHERE guild=? AND metric='leaves' AND kind='guild' AND day>=? GROUP BY day ORDER BY day DESC LIMIT 30",(i.guild_id,cutoff)).fetchall()
        await reply(i,'\n'.join(f'{day}: {value}' for day,value in rows) or 'No observed departures in this period.')

    @register('activity','Read message activity by UTC hour of day')
    async def activity(i:discord.Interaction,days:app_commands.Range[int,1,365]=7):
        cutoff=(discord.utils.utcnow().date()-dt.timedelta(days=days-1)).isoformat()
        rows=db.execute("SELECT hour,SUM(value) FROM activity_metrics WHERE guild=? AND metric='messages' AND kind='guild' AND day>=? GROUP BY hour ORDER BY hour",(i.guild_id,cutoff)).fetchall()
        await reply(i,'\n'.join(f'{hour:02d}:00 UTC · {value} messages' for hour,value in rows) or 'No hourly message activity recorded.')

    @register('channels','Rank existing channels by observed message volume')
    async def channels(i:discord.Interaction,days:app_commands.Range[int,1,365]=7):
        rows=service.ranking(i.guild_id,'messages',days,'channel')
        await reply(i,'\n'.join(f'{n}. <#{channel}>: {value}' for n,(channel,value) in enumerate(rows,1)) or 'No channel activity recorded.')

    @register('users','Rank members by observed message volume')
    async def users(i:discord.Interaction,days:app_commands.Range[int,1,365]=7):
        rows=service.ranking(i.guild_id,'messages',days,'member')
        await reply(i,'\n'.join(f'{n}. <@{member}>: {value}' for n,(member,value) in enumerate(rows,1)) or 'No member activity recorded.')

    @register('commands','Rank successfully completed bot command callbacks')
    async def commands(i:discord.Interaction,days:app_commands.Range[int,1,365]=7):
        rows=service.ranking(i.guild_id,'commands',days,'command')
        await reply(i,'\n'.join(f'{name}: {value}' for name,value in rows) or 'No completed command callbacks recorded.')

    @register('voice','Read active voice time, excluding solo, AFK and deafened members')
    async def voice(i:discord.Interaction,days:app_commands.Range[int,1,365]=7,member:discord.Member=None):
        if member:
            total=service.query(i.guild_id,'voice_seconds',days,'member',member.id)
            await reply(i,f'{member.display_name}: {total//60} observed active voice minutes.')
        else:
            rows=service.ranking(i.guild_id,'voice_seconds',days,'member')
            await reply(i,'\n'.join(f'<@{user}>: {seconds//60} minutes' for user,seconds in rows) or 'No eligible voice time recorded.')

    @register('enable','Enable or disable metadata-only server activity collection')
    async def enable(i:discord.Interaction,enabled:bool):
        put_setting(i.guild_id,'module:analytics','1' if enabled else '0')
        await reply(i,'Activity collection '+('enabled' if enabled else 'disabled')+'. Existing aggregates were retained.')

    @register('retention','Configure aggregate retention from 7 to 365 days')
    async def retention(i:discord.Interaction,days:app_commands.Range[int,7,365]):
        put_setting(i.guild_id,'analytics:retention_days',days)
        await reply(i,'Aggregate retention saved. Older records are removed by the next scheduled cleanup.')

    @register('ignore-channel','Exclude an existing channel from future analytics collection')
    async def ignore_channel(i:discord.Interaction,channel:discord.TextChannel,ignored:bool):
        ids={x for x in get_setting(i.guild_id,'analytics:ignored_channels','').split(',') if x}
        ids.add(str(channel.id)) if ignored else ids.discard(str(channel.id));put_setting(i.guild_id,'analytics:ignored_channels',','.join(sorted(ids)))
        await reply(i,'Analytics channel exclusion saved.')

    @register('ignore-role','Exclude members with an existing role from future analytics collection')
    async def ignore_role(i:discord.Interaction,role:discord.Role,ignored:bool):
        ids={x for x in get_setting(i.guild_id,'analytics:ignored_roles','').split(',') if x}
        ids.add(str(role.id)) if ignored else ids.discard(str(role.id));put_setting(i.guild_id,'analytics:ignored_roles',','.join(sorted(ids)))
        await reply(i,'Analytics role exclusion saved.')

    @register('export','Download this server’s collected aggregate activity as JSON')
    async def export(i:discord.Interaction,days:app_commands.Range[int,1,365]=30):
        cutoff=(discord.utils.utcnow().date()-dt.timedelta(days=days-1)).isoformat()
        cursor=db.execute('SELECT day,hour,metric,kind,target,value FROM activity_metrics WHERE guild=? AND day>=? ORDER BY day DESC LIMIT 50000',(i.guild_id,cutoff))
        names=[c[0] for c in cursor.description];rows=[dict(zip(names,row)) for row in cursor.fetchall()]
        payload=json.dumps({'guild':i.guild_id,'rows':rows,'limit':50000},ensure_ascii=False,indent=2).encode()
        if len(payload)>7_000_000:raise ValueError('This export is too large. Select a shorter period.')
        await reply(i,'Private activity aggregates; message content was never collected.',file=discord.File(io.BytesIO(payload),filename='server-activity.json'))

    @register('collection','Read collection, retention, exclusions and recorded range')
    async def collection(i:discord.Interaction):
        row=db.execute('SELECT MIN(day),MAX(day),COUNT(*) FROM activity_metrics WHERE guild=?',(i.guild_id,)).fetchone()
        await reply(i,f'Enabled: {service.enabled(i.guild_id)}\nRetention: {get_setting(i.guild_id,"analytics:retention_days","90")} days\nRecorded date range: {row[0] or "none"} – {row[1] or "none"}\nAggregate rows: {row[2]}\nIgnored channels: {get_setting(i.guild_id,"analytics:ignored_channels","") or "none"}\nIgnored roles: {get_setting(i.guild_id,"analytics:ignored_roles","") or "none"}\nNo message bodies, attachments, recordings or private ticket contents are stored.')

    bot.add_listener(service.ready,'on_ready')
    for event in ('on_message','on_member_join','on_member_remove','on_app_command_completion','on_command_completion','on_voice_state_update'):
        bot.add_listener(getattr(service,event),event)
    return service
