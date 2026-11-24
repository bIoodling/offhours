"""Real moderation commands with shared permission and persistence handlers."""
import asyncio
import contextlib
import datetime as dt
import functools
import io
import json
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from services.moderation import ModerationService, duration, staff

log = logging.getLogger('offhours.moderation')


async def reply(i, content=None, **kwargs):
    if content is not None:
        content = str(content)[:1900]
    kwargs.setdefault('ephemeral', True)
    kwargs.setdefault('allowed_mentions', discord.AllowedMentions.none())
    if i.response.is_done():
        await i.followup.send(content, **kwargs)
    else:
        await i.response.send_message(content, **kwargs)


def staff_check(i, permission='manage_messages'):
    if not i.guild or not staff(i.user, permission):
        raise ValueError('You need ' + permission.replace('_', ' ').title() + ' permission in this server.')


def install(bot, connection, get_setting, put_setting):
    service = ModerationService(bot, connection, get_setting, put_setting)
    db = connection

    async def guarded(fn, i, *args, **kwargs):
        try:
            if not i.guild:
                return await reply(i, 'Use this command in a server.')
            return await fn(i, *args, **kwargs)
        except ValueError as exc:
            await reply(i, str(exc))
        except discord.HTTPException as exc:
            await reply(i, f'Discord rejected the action (HTTP {exc.status}). Check permissions and role hierarchy.')
        except Exception as exc:
            log.error('Moderation callback failed: %s', type(exc).__name__)
            await reply(i, 'The action failed. No success has been recorded.')

    def register(name, description, fn, group=None):
        @functools.wraps(fn)
        async def callback(i, *args, **kwargs):
            return await guarded(fn, i, *args, **kwargs)
        callback = app_commands.guild_only()(callback)
        command = app_commands.Command(name=name, description=description, callback=callback)
        if group:
            group.add_command(command)
        else:
            bot.tree.remove_command(name)
            bot.tree.add_command(command)
        return command

    async def action(i, name, member=None, reason='No reason supplied', value=None):
        await i.response.defer(ephemeral=True)
        case_id = await service.execute_action(i, name, member, reason, value)
        await reply(i, f'{name.capitalize()} completed. Case #{case_id}.')

    async def ban(i: discord.Interaction, member: discord.Member, reason: str = 'No reason supplied'):
        await action(i, 'ban', member, reason)
    register('ban', 'Ban a member after checking both role hierarchies', ban)

    async def unban(i: discord.Interaction, user_id: str, reason: str = 'No reason supplied'):
        if not user_id.isdecimal():
            raise ValueError('Use a numeric Discord user ID.')
        await action(i, 'unban', discord.Object(id=int(user_id)), reason)
    register('unban', 'Unban a Discord user ID and cancel pending automatic unbans', unban)

    async def kick(i: discord.Interaction, member: discord.Member, reason: str = 'No reason supplied'):
        await action(i, 'kick', member, reason)
    register('kick', 'Remove a member from this server', kick)

    async def timeout(i: discord.Interaction, member: discord.Member, duration_text: str, reason: str = 'No reason supplied'):
        await action(i, 'timeout', member, reason, duration_text)
    register('timeout', 'Timeout a member for a duration such as 30m or 7d', timeout)

    async def untimeout(i: discord.Interaction, member: discord.Member, reason: str = 'No reason supplied'):
        await action(i, 'untimeout', member, reason)
    register('untimeout', 'Remove a native Discord member timeout', untimeout)

    async def warn(i: discord.Interaction, member: discord.Member, reason: str):
        await action(i, 'warn', member, reason)
    register('warn', 'Save a member warning and moderation case', warn)

    async def warnings(i: discord.Interaction, member: discord.Member):
        staff_check(i)
        rows = db.execute('SELECT id,reason,timestamp FROM warnings WHERE guild=? AND user=? ORDER BY id DESC LIMIT 15', (i.guild_id, member.id)).fetchall()
        await reply(i, '\n'.join(f'#{row[0]} · <t:{row[2]}:d> · {row[1][:160]}' for row in rows) or 'No warnings for this member.')
    register('warnings', 'Read this server’s saved warnings for a member', warnings)

    async def purge(i: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
        await i.response.defer(ephemeral=True)
        count = await service.purge(i, amount)
        await reply(i, f'Deleted {count} messages newer than seven days.')
    register('purge', 'Delete up to 100 recent messages with a seven-day safety cutoff', purge)

    async def clean(i: discord.Interaction, amount: app_commands.Range[int, 1, 100] = 30):
        await i.response.defer(ephemeral=True)
        count = await service.purge(i, amount, bots_only=True)
        await reply(i, f'Deleted {count} bot-authored messages newer than seven days.')
    register('clean', 'Remove recent bot messages from this channel', clean)

    async def softban(i: discord.Interaction, member: discord.Member, reason: str = 'No reason supplied'):
        await action(i, 'softban', member, reason)
    register('softban', 'Ban and unban a member to remove their last day of messages', softban)

    async def tempban(i: discord.Interaction, member: discord.Member, duration_text: str, reason: str = 'No reason supplied'):
        await action(i, 'tempban', member, reason, duration_text)
    register('tempban', 'Ban a member with a persistent scheduled automatic unban', tempban)

    async def lock(i: discord.Interaction, channel: discord.TextChannel = None, reason: str = 'No reason supplied'):
        await action(i, 'lock', channel, reason)
    register('lock', 'Lock an existing channel and save the previous overwrite', lock)

    async def unlock(i: discord.Interaction, channel: discord.TextChannel = None, reason: str = 'No reason supplied'):
        await action(i, 'unlock', channel, reason)
    register('unlock', 'Restore the exact overwrite saved by Offhours channel locking', unlock)

    async def lockdown(i: discord.Interaction, enabled: bool, reason: str = 'No reason supplied'):
        service.check(i, 'manage_channels')
        await i.response.defer(ephemeral=True)
        succeeded, failed = 0, 0
        for channel in i.guild.text_channels:
            if not enabled and not db.execute('SELECT 1 FROM channel_locks WHERE guild=? AND channel=?', (i.guild_id, channel.id)).fetchone():
                continue
            try:
                await service.execute_action(i, 'lock' if enabled else 'unlock', channel, reason)
                succeeded += 1
            except (discord.HTTPException, ValueError):
                failed += 1
        await reply(i, f'Updated {succeeded} channels; {failed} failed. Only saved Offhours locks are restored when disabling.')
    register('lockdown', 'Lock existing text channels or restore their saved permissions', lockdown)

    async def slowmode(i: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
        await action(i, 'slowmode', value=seconds)
    register('slowmode', 'Set the current text channel’s slowmode', slowmode)

    async def nick(i: discord.Interaction, member: discord.Member, nickname: str = ''):
        await action(i, 'nick', member, 'Nickname changed by moderator', nickname)
    register('nick', 'Set or clear a member nickname', nick)

    async def deafen(i: discord.Interaction, member: discord.Member, reason: str = 'No reason supplied'):
        await action(i, 'deafen', member, reason)
    register('deafen', 'Server-deafen a member connected to voice', deafen)

    async def undeafen(i: discord.Interaction, member: discord.Member, reason: str = 'No reason supplied'):
        await action(i, 'undeafen', member, reason)
    register('undeafen', 'Remove server deafening from a voice member', undeafen)

    async def mute(i: discord.Interaction, member: discord.Member, reason: str = 'No reason supplied'):
        service.check(i, 'mute_members', member)
        if not member.voice:
            raise ValueError('The member is not connected to voice.')
        await i.response.defer(ephemeral=True)
        await member.edit(mute=True, reason=f'{i.user.id}: {reason}'[:512])
        number = await service.record_case(i.guild, member.id, i.user.id, 'voice-mute', reason)
        await reply(i, f'Server voice mute applied. Case #{number}.')
    register('mute', 'Server-mute a member in voice; use /timeout for text restrictions', mute)

    async def unmute(i: discord.Interaction, member: discord.Member, reason: str = 'No reason supplied'):
        service.check(i, 'mute_members', member)
        if not member.voice:
            raise ValueError('The member is not connected to voice.')
        await i.response.defer(ephemeral=True)
        await member.edit(mute=False, reason=f'{i.user.id}: {reason}'[:512])
        number = await service.record_case(i.guild, member.id, i.user.id, 'voice-unmute', reason)
        await reply(i, f'Server voice mute removed. Case #{number}.')
    register('unmute', 'Remove a member’s server voice mute', unmute)

    # Management commands replace the old mixed /mod action group.
    bot.tree.remove_command('mod')
    for guild in bot.guilds:
        bot.tree.remove_command('mod', guild=discord.Object(id=guild.id))
    mod = app_commands.Group(name='mod', description='Server moderation records and configuration', guild_only=True)
    bot.tree.add_command(mod)

    def fetch_case(i, case_id):
        staff_check(i)
        row = db.execute('SELECT id,user,moderator,action,reason,created,expires,status FROM mod_cases WHERE guild=? AND id=?', (i.guild_id, case_id)).fetchone()
        if not row:
            raise ValueError('No such moderation case in this server.')
        return row

    async def case(i: discord.Interaction, case_id: int):
        row = fetch_case(i, case_id)
        await reply(i, f'Case #{row[0]} · {row[3]}\nUser: {row[1]} · Moderator: {row[2]}\nCreated: <t:{int(row[5])}:F>\nExpires: {"<t:"+str(int(row[6]))+":R>" if row[6] else "—"}\nStatus: {row[7]}\n{row[4][:1200]}')
    register('case', 'Read a moderation case from this server', case, mod)

    async def cases(i: discord.Interaction, member: discord.Member = None):
        staff_check(i)
        query = 'SELECT id,action,user,reason FROM mod_cases WHERE guild=?'
        args = [i.guild_id]
        if member:
            query += ' AND user=?'
            args.append(member.id)
        rows = db.execute(query + ' ORDER BY id DESC LIMIT 15', args).fetchall()
        await reply(i, '\n'.join(f'#{n} · {a} · {u}: {r[:100]}' for n, a, u, r in rows) or 'No moderation cases.')
    register('cases', 'List recent cases, optionally for one member', cases, mod)

    async def modlogs(i: discord.Interaction, channel: discord.TextChannel):
        staff_check(i, 'manage_guild')
        if not channel.permissions_for(i.guild.me).send_messages:
            raise ValueError('The bot cannot send messages in that channel.')
        if channel.permissions_for(i.guild.default_role).view_channel:
            raise ValueError('Choose an existing private staff channel to protect moderation records.')
        put_setting(i.guild_id, 'moderation:log_channel', channel.id)
        await reply(i, 'Moderation log destination saved.')
    register('modlogs', 'Select the existing channel for silent moderation logs', modlogs, mod)

    async def modstats(i: discord.Interaction, moderator: discord.Member = None):
        staff_check(i)
        query = 'SELECT action,COUNT(*) FROM mod_cases WHERE guild=?'
        args = [i.guild_id]
        if moderator:
            query += ' AND moderator=?'
            args.append(moderator.id)
        rows = db.execute(query + ' GROUP BY action ORDER BY COUNT(*) DESC', args).fetchall()
        await reply(i, '\n'.join(f'{a}: {c}' for a, c in rows) or 'No recorded actions.')
    register('modstats', 'Aggregate recorded actions by moderator and action', modstats, mod)

    async def reason(i: discord.Interaction, case_id: int, text: str):
        staff_check(i, 'manage_guild')
        fetch_case(i, case_id)
        db.execute('UPDATE mod_cases SET reason=? WHERE guild=? AND id=?', (text[:1000], i.guild_id, case_id)); db.commit()
        await reply(i, 'Case reason updated.')
    register('reason', 'Edit a saved moderation case reason', reason, mod)

    async def duration_cmd(i: discord.Interaction, case_id: int, duration_text: str):
        row = fetch_case(i, case_id)
        if row[3] not in ('timeout', 'tempban'):
            raise ValueError('Only timeout and temporary-ban durations can be changed.')
        permission = 'moderate_members' if row[3] == 'timeout' else 'ban_members'
        service.check(i, permission)
        seconds = duration(duration_text, 28*86400 if row[3] == 'timeout' else 365*86400)
        expires = time.time() + seconds
        await i.response.defer(ephemeral=True)
        if row[3] == 'timeout':
            member = i.guild.get_member(row[1]) or await i.guild.fetch_member(row[1])
            service.check(i, permission, member)
            await member.timeout(dt.timedelta(seconds=seconds), reason=f'Case #{case_id} duration edited by {i.user.id}')
        else:
            await i.guild.fetch_ban(discord.Object(id=row[1]))
            db.execute("UPDATE mod_jobs SET status='cancelled' WHERE guild=? AND target=? AND kind='unban' AND status='pending'", (i.guild_id, row[1]))
            service.schedule(i.guild_id, 'unban', row[1], {'reason': f'Case #{case_id} temporary ban expired'}, expires)
        db.execute('UPDATE mod_cases SET expires=? WHERE guild=? AND id=?', (expires, i.guild_id, case_id)); db.commit()
        await reply(i, 'Live punishment expiration and saved case updated.')
    register('duration', 'Change the live duration of a timeout or temporary ban', duration_cmd, mod)

    async def moderations(i: discord.Interaction):
        staff_check(i)
        rows = db.execute("SELECT id,kind,target,due,status FROM mod_jobs WHERE guild=? AND kind!='reminder' AND status IN ('pending','failed') ORDER BY due LIMIT 15", (i.guild_id,)).fetchall()
        await reply(i, '\n'.join(f'#{n} · {k} · {u} · <t:{int(d)}:R> · {s}' for n,k,u,d,s in rows) or 'No scheduled moderation actions.')
    register('moderations', 'List pending or failed scheduled punishments', moderations, mod)

    async def note(i: discord.Interaction, member: discord.Member, text: str):
        staff_check(i)
        cur = db.execute('INSERT INTO mod_notes(guild,user,author,body,created) VALUES(?,?,?,?,?)', (i.guild_id, member.id, i.user.id, text[:2000], time.time())); db.commit()
        await reply(i, f'Private staff note #{cur.lastrowid} saved.')
    register('note', 'Save a private moderation note for a member', note, mod)

    async def notes(i: discord.Interaction, member: discord.Member):
        staff_check(i)
        rows = db.execute('SELECT id,author,body FROM mod_notes WHERE guild=? AND user=? ORDER BY id DESC LIMIT 10', (i.guild_id, member.id)).fetchall()
        await reply(i, '\n'.join(f'#{n} · {a}: {text[:160]}' for n,a,text in rows) or 'No private staff notes.')
    register('notes', 'Read this server’s private notes for a member', notes, mod)

    async def editnote(i: discord.Interaction, note_id: int, text: str):
        staff_check(i)
        cur = db.execute('UPDATE mod_notes SET body=? WHERE guild=? AND id=? AND (author=? OR ?)', (text[:2000], i.guild_id, note_id, i.user.id, int(i.user.guild_permissions.manage_guild))); db.commit()
        await reply(i, 'Note updated.' if cur.rowcount else 'No editable note with that ID in this server.')
    register('editnote', 'Edit a note you authored, or any note with Manage Server', editnote, mod)

    async def delnote(i: discord.Interaction, note_id: int):
        staff_check(i)
        cur = db.execute('DELETE FROM mod_notes WHERE guild=? AND id=? AND (author=? OR ?)', (i.guild_id, note_id, i.user.id, int(i.user.guild_permissions.manage_guild))); db.commit()
        await reply(i, 'Note deleted.' if cur.rowcount else 'No deletable note with that ID in this server.')
    register('delnote', 'Delete an authorized private moderation note', delnote, mod)

    async def clearnotes(i: discord.Interaction, member: discord.Member):
        staff_check(i, 'manage_guild')
        cur = db.execute('DELETE FROM mod_notes WHERE guild=? AND user=?', (i.guild_id, member.id)); db.commit()
        await reply(i, f'Deleted {cur.rowcount} notes for this member.')
    register('clearnotes', 'Delete all private moderation notes for a member', clearnotes, mod)

    async def delwarn(i: discord.Interaction, warning_id: int):
        staff_check(i, 'manage_guild')
        cur = db.execute('DELETE FROM warnings WHERE guild=? AND id=?', (i.guild_id, warning_id)); db.commit()
        await reply(i, 'Warning deleted.' if cur.rowcount else 'No warning with that ID in this server.')
    register('delwarn', 'Delete one warning belonging to this server', delwarn, mod)

    async def clearwarn(i: discord.Interaction, member: discord.Member):
        staff_check(i, 'manage_guild')
        cur = db.execute('DELETE FROM warnings WHERE guild=? AND user=?', (i.guild_id, member.id)); db.commit()
        await reply(i, f'Deleted {cur.rowcount} warnings for this member.')
    register('clearwarn', 'Clear a member’s warnings in this server', clearwarn, mod)

    async def ignored(i: discord.Interaction):
        staff_check(i, 'manage_guild')
        rows = db.execute('SELECT kind,target FROM mod_ignored WHERE guild=? ORDER BY kind,target', (i.guild_id,)).fetchall()
        await reply(i, '\n'.join(f'{k}: {v}' for k,v in rows)[:1800] or 'No automoderation exclusions.')
    register('ignored', 'List server-specific automoderation exclusions', ignored, mod)

    def save_ignore(i, kind, target, enabled):
        staff_check(i, 'manage_guild')
        if enabled:
            db.execute('INSERT OR IGNORE INTO mod_ignored VALUES(?,?,?)', (i.guild_id, kind, target))
        else:
            db.execute('DELETE FROM mod_ignored WHERE guild=? AND kind=? AND target=?', (i.guild_id, kind, target))
        db.commit()

    async def ignoreuser(i: discord.Interaction, member: discord.Member, enabled: bool):
        save_ignore(i, 'user', member.id, enabled); await reply(i, 'Member automoderation exclusion saved.')
    register('ignoreuser', 'Enable or remove an automoderation member exclusion', ignoreuser, mod)

    async def ignorechannel(i: discord.Interaction, channel: discord.TextChannel, enabled: bool):
        save_ignore(i, 'channel', channel.id, enabled); await reply(i, 'Channel automoderation exclusion saved.')
    register('ignorechannel', 'Enable or remove an automoderation channel exclusion', ignorechannel, mod)

    async def ignorerole(i: discord.Interaction, role: discord.Role, enabled: bool):
        save_ignore(i, 'role', role.id, enabled); await reply(i, 'Role automoderation exclusion saved.')
    register('ignorerole', 'Enable or remove an automoderation role exclusion', ignorerole, mod)

    async def history(i: discord.Interaction):
        staff_check(i, 'view_audit_log')
        await i.response.defer(ephemeral=True)
        entries = [f'{entry.action.name} · {entry.user_id} → {getattr(entry.target,"id",entry.target)}' async for entry in i.guild.audit_logs(limit=12)]
        await reply(i, '\n'.join(entries)[:1800] or 'No audit-log entries.')
    register('history', 'Read recent Discord audit-log entries', history, mod)

    async def export(i: discord.Interaction, member: discord.Member = None):
        staff_check(i, 'manage_guild')
        query, params = 'SELECT * FROM mod_cases WHERE guild=?', [i.guild_id]
        if member:
            query += ' AND user=?'; params.append(member.id)
        cursor = db.execute(query + ' ORDER BY id DESC LIMIT 10000', params)
        records = [dict(zip([c[0] for c in cursor.description], row)) for row in cursor.fetchall()]
        payload = json.dumps({'guild': i.guild_id, 'cases': records}, ensure_ascii=False, indent=2).encode()
        await reply(i, 'Private moderation case export (up to 10,000 most recent cases).', file=discord.File(io.BytesIO(payload), filename='moderation-cases.json'))
    register('export', 'Download private server-scoped moderation records as JSON', export, mod)

    security = app_commands.Group(name='security', description='Opt-in server safety controls', guild_only=True)
    bot.tree.remove_command('security'); bot.tree.add_command(security)

    async def status(i: discord.Interaction):
        staff_check(i, 'manage_guild')
        names = ('antispam','antiraid','antinuke','antilink','antiinvite','antimention','antiattachment','antihoist','accountage')
        lines = ['Security module: ' + ('enabled' if get_setting(i.guild_id,'module:security','0')=='1' else 'disabled')]
        lines += [name + ': ' + ('enabled' if get_setting(i.guild_id,'security:'+name,'0')=='1' else 'disabled') for name in names]
        await reply(i, '\n'.join(lines))
    register('status', 'Read saved security controls for this server', status, security)

    async def enable(i: discord.Interaction):
        staff_check(i,'manage_guild'); put_setting(i.guild_id,'module:security','1')
        await reply(i,'Security module enabled. Individual filters retain their saved settings.')
    register('enable','Enable configured safety filters for this server',enable,security)

    async def disable(i: discord.Interaction):
        staff_check(i,'manage_guild'); put_setting(i.guild_id,'module:security','0')
        await reply(i,'Security module disabled. Saved filter configuration was retained.')
    register('disable','Disable all automatic security actions for this server',disable,security)

    def filter_command(name, description):
        async def callback(i: discord.Interaction, enabled: bool):
            staff_check(i,'manage_guild'); put_setting(i.guild_id,'security:'+name,'1' if enabled else '0')
            await reply(i,f'{name} configuration saved. The Security module must also be enabled for automatic actions.')
        register(name,description,callback,security)
    for name, description in [
        ('antispam','Toggle burst-spam removal and temporary timeouts'),
        ('antiraid','Toggle join-burst quarantine and temporary timeouts'),
        ('antinuke','Toggle destructive audit-event role-assignment containment'),
        ('antilink','Toggle link removal with configurable allowed domains'),
        ('antiinvite','Toggle removal of Discord invite links'),
        ('antimention','Toggle excessive member and role mention removal'),
        ('antiattachment','Toggle attachment message removal'),
        ('antihoist','Toggle leading-symbol nickname cleanup on new joins'),
    ]:
        filter_command(name,description)

    async def accountage(i: discord.Interaction, enabled: bool, minimum_hours: app_commands.Range[int,1,8760]=24):
        staff_check(i,'manage_guild'); put_setting(i.guild_id,'security:accountage','1' if enabled else '0'); put_setting(i.guild_id,'security:minimum_age_hours',minimum_hours)
        await reply(i,'Minimum account age configuration saved.')
    register('accountage','Configure account-age quarantine on new member joins',accountage,security)

    async def verification(i: discord.Interaction, level: app_commands.Range[int,0,4]):
        service.check(i,'manage_guild'); await i.response.defer(ephemeral=True)
        await i.guild.edit(verification_level=discord.VerificationLevel(level),reason=f'Offhours verification setting by {i.user.id}')
        await reply(i,f'Discord verification level set to {level}.')
    register('verification','Set Discord’s actual server verification level (0–4)',verification,security)

    async def quarantine(i: discord.Interaction, member: discord.Member, role: discord.Role = None):
        service.check(i,'manage_roles',member)
        if role:
            dangerous=any((role.permissions.administrator,role.permissions.manage_guild,role.permissions.manage_roles,
                role.permissions.manage_channels,role.permissions.ban_members,role.permissions.kick_members,role.permissions.manage_webhooks))
            if role.managed or role.is_default() or role>=i.guild.me.top_role or dangerous or (i.user.id!=i.guild.owner_id and role>=i.user.top_role):
                raise ValueError('Choose an existing non-managed non-administrator role below both role hierarchies.')
            put_setting(i.guild_id,'security:quarantine_role',role.id)
        raw = get_setting(i.guild_id,'security:quarantine_role','')
        selected = i.guild.get_role(int(raw)) if raw.isdecimal() else None
        if not selected:
            raise ValueError('Select an existing quarantine role first.')
        dangerous=any((selected.permissions.administrator,selected.permissions.manage_guild,selected.permissions.manage_roles,
            selected.permissions.manage_channels,selected.permissions.ban_members,selected.permissions.kick_members,selected.permissions.manage_webhooks))
        if selected.managed or selected.is_default() or dangerous or selected>=i.guild.me.top_role:
            raise ValueError('The configured quarantine role is unsafe or above the bot.')
        await i.response.defer(ephemeral=True)
        await member.add_roles(selected,reason=f'Quarantine by {i.user.id}')
        await service.record_case(i.guild,member.id,i.user.id,'quarantine','Configured quarantine role assigned')
        await reply(i,'Configured quarantine role assigned. Its channel restrictions are controlled by your server permissions.')
    register('quarantine','Assign an existing restricted role to a member',quarantine,security)

    async def release(i: discord.Interaction, member: discord.Member):
        service.check(i,'manage_roles',member)
        raw=get_setting(i.guild_id,'security:quarantine_role',''); role=i.guild.get_role(int(raw)) if raw.isdecimal() else None
        if not role or role not in member.roles:
            raise ValueError('The member does not have the configured quarantine role.')
        await i.response.defer(ephemeral=True); await member.remove_roles(role,reason=f'Quarantine release by {i.user.id}')
        await service.record_case(i.guild,member.id,i.user.id,'release','Configured quarantine role removed')
        await reply(i,'Quarantine role removed. Any native timeout can be removed with /untimeout.')
    register('release','Remove the configured quarantine role from a member',release,security)

    async def whitelist_add(i: discord.Interaction, member: discord.Member = None, role: discord.Role = None):
        staff_check(i,'manage_guild')
        if (member is None)==(role is None): raise ValueError('Select exactly one member or role.')
        values=set(get_setting(i.guild_id,'security:whitelist','').split(',')); values.discard(''); values.add(str((member or role).id))
        put_setting(i.guild_id,'security:whitelist',','.join(sorted(values))); await reply(i,'Security allowlist entry saved.')
    register('whitelist-add','Exclude one existing member or role from automatic security actions',whitelist_add,security)

    async def whitelist_remove(i: discord.Interaction, member: discord.Member = None, role: discord.Role = None):
        staff_check(i,'manage_guild')
        if (member is None)==(role is None): raise ValueError('Select exactly one member or role.')
        values=set(get_setting(i.guild_id,'security:whitelist','').split(',')); values.discard(''); values.discard(str((member or role).id))
        put_setting(i.guild_id,'security:whitelist',','.join(sorted(values))); await reply(i,'Security allowlist entry removed if present.')
    register('whitelist-remove','Remove one security allowlist exemption',whitelist_remove,security)

    async def whitelist_list(i: discord.Interaction):
        staff_check(i,'manage_guild'); values=get_setting(i.guild_id,'security:whitelist','')
        await reply(i,values[:1800] or 'The security allowlist is empty.')
    register('whitelist-list','Read this server’s security allowlist IDs',whitelist_list,security)

    async def thresholds(i: discord.Interaction, spam_limit: app_commands.Range[int,3,30]=8, mention_limit: app_commands.Range[int,2,50]=5,
                         raid_limit: app_commands.Range[int,3,100]=10, destructive_limit: app_commands.Range[int,3,30]=5, allowed_domains: str=''):
        staff_check(i,'manage_guild')
        for name,value in [('spam_limit',spam_limit),('mention_limit',mention_limit),('raid_limit',raid_limit),('nuke_limit',destructive_limit),('allowed_domains',allowed_domains[:1000])]:
            put_setting(i.guild_id,'security:'+name,value)
        await reply(i,'Security thresholds saved. Spam uses an 8-second window; join and destructive-action bursts use 10 seconds.')
    register('thresholds','Configure burst limits and exact allowed link domains',thresholds,security)

    async def incidents(i: discord.Interaction, member: discord.Member = None):
        staff_check(i,'manage_guild'); query='SELECT id,user,kind,detail,created FROM security_incidents WHERE guild=?'; params=[i.guild_id]
        if member: query+=' AND user=?'; params.append(member.id)
        rows=db.execute(query+' ORDER BY id DESC LIMIT 12',params).fetchall()
        await reply(i,'\n'.join(f'#{n} · {k} · {u} · <t:{int(t)}:R>\n{d[:100]}' for n,u,k,d,t in rows)[:1800] or 'No saved security incidents.')
    register('incidents','Read private server-scoped security incident records',incidents,security)

    async def wordfilter(i: discord.Interaction, enabled: bool, words: str):
        staff_check(i,'manage_guild')
        cleaned=[w.strip() for w in words.split(',') if w.strip()]
        if enabled and not cleaned: raise ValueError('Provide comma-separated blocked words or phrases.')
        if len(words)>3000 or len(cleaned)>100: raise ValueError('Use at most 100 words or phrases and 3,000 characters.')
        put_setting(i.guild_id,'security:wordfilter','1' if enabled else '0'); put_setting(i.guild_id,'security:blocked_words',','.join(cleaned))
        await reply(i,'Word filter saved. Matching is case-insensitive and respects word boundaries.')
    register('wordfilter','Configure blocked words and phrases for message removal',wordfilter,security)

    async def scamfilter(i: discord.Interaction, enabled: bool, blocked_domains: str=''):
        staff_check(i,'manage_guild')
        domains=[x.strip().lower().rstrip('.') for x in blocked_domains.split(',') if x.strip()]
        if len(domains)>100 or any('://' in d or '/' in d or ' ' in d for d in domains):
            raise ValueError('Provide up to 100 domain names without schemes, paths or spaces.')
        put_setting(i.guild_id,'security:scamfilter','1' if enabled else '0'); put_setting(i.guild_id,'security:blocked_domains',','.join(domains))
        await reply(i,'Configured domain blocking and suspicious gift-domain pattern filtering saved. No external reputation check is implied.')
    register('scamfilter','Block configured domains and suspicious gift-domain patterns',scamfilter,security)

    async def link_role(i: discord.Interaction, role: discord.Role, allowed: bool):
        staff_check(i,'manage_guild')
        ids={int(x) for x in get_setting(i.guild_id,'security:link_roles','').split(',') if x.isdecimal()}
        ids.add(role.id) if allowed else ids.discard(role.id)
        put_setting(i.guild_id,'security:link_roles',','.join(map(str,sorted(ids))))
        await reply(i,'Role-based link permission saved. Other safety filters still apply to this role.')
    register('link-role','Allow or revoke ordinary links for an existing role',link_role,security)

    async def escalation(i: discord.Interaction, warning_after: app_commands.Range[int,1,50]=3,
                         timeout_after: app_commands.Range[int,2,100]=5, timeout_minutes: app_commands.Range[int,1,40320]=10):
        staff_check(i,'manage_guild')
        if timeout_after<=warning_after: raise ValueError('The timeout threshold must be higher than the warning threshold.')
        for key,value in [('warning_after',warning_after),('timeout_after',timeout_after),('timeout_minutes',timeout_minutes)]:
            put_setting(i.guild_id,'security:'+key,value)
        await reply(i,'Escalation saved: incidents within 24 hours trigger a warning, then a native Discord timeout when permitted.')
    register('escalation','Configure warning and timeout escalation after repeated safety incidents',escalation,security)

    async def exemptions(i: discord.Interaction, moderators_exempt: bool, role: discord.Role=None, enabled: bool=True):
        staff_check(i,'manage_guild')
        put_setting(i.guild_id,'security:moderator_exempt','1' if moderators_exempt else '0')
        if role:
            ids={int(x) for x in get_setting(i.guild_id,'security:exempt_roles','').split(',') if x.isdecimal()}
            ids.add(role.id) if enabled else ids.discard(role.id)
            put_setting(i.guild_id,'security:exempt_roles',','.join(map(str,sorted(ids))))
        await reply(i,'Moderator and optional role exemptions saved for automatic message filtering.')
    register('exemptions','Configure moderator and existing-role message filter exemptions',exemptions,security)

    async def afk(i: discord.Interaction, reason: str='Away from keyboard'):
        db.execute('INSERT OR REPLACE INTO afk_members VALUES(?,?,?,?)',(i.guild_id,i.user.id,reason[:300],time.time())); db.commit()
        await reply(i,'AFK status saved for this server. Sending a message clears it.')
    register('afk','Set an AFK reason shown when members mention you',afk)

    async def remindme(i: discord.Interaction, after: str, message: str):
        seconds=duration(after)
        outstanding=db.execute("SELECT COUNT(*) FROM mod_jobs WHERE guild=? AND target=? AND kind='reminder' AND status='pending'",(i.guild_id,i.user.id)).fetchone()[0]
        if outstanding>=10: raise ValueError('You already have ten pending reminders in this server.')
        number=service.schedule(i.guild_id,'reminder',i.user.id,{'text':message[:1500]},time.time()+seconds)
        await reply(i,f'Reminder #{number} saved for <t:{int(time.time()+seconds)}:F>. Delivery requires your DMs to be open.')
    register('remindme','Save a persistent reminder delivered privately at its due time',remindme)

    async def report(i: discord.Interaction, member: discord.Member, reason: str):
        raw=get_setting(i.guild_id,'channel:reports','') or get_setting(i.guild_id,'moderation:log_channel','')
        channel=i.guild.get_channel(int(raw)) if raw.isdecimal() else None
        if not channel or not channel.permissions_for(i.guild.me).send_messages:
            raise ValueError('Administrators have not configured an accessible report destination.')
        if channel.permissions_for(i.guild.default_role).view_channel:
            raise ValueError('The configured report destination is public. Ask an administrator to select a private staff channel.')
        await i.response.defer(ephemeral=True)
        await channel.send(embed=discord.Embed(title='Member report',description=reason[:3000],colour=0xBA929D)
            .add_field(name='Member',value=f'{member} ({member.id})').add_field(name='Reporter',value=f'{i.user} ({i.user.id})'),allowed_mentions=discord.AllowedMentions.none(),silent=True)
        await reply(i,'Your report was delivered privately to the configured staff channel.')
    register('report','Send a private member report to the configured staff channel',report)

    logs=app_commands.Group(name='logs',description='Configure silent server event logging',guild_only=True)
    bot.tree.remove_command('logs');bot.tree.add_command(logs)
    async def logs_status(i:discord.Interaction):
        staff_check(i,'manage_guild')
        categories=('messages','members','roles','channels','moderation','security')
        lines=['Logging module: '+('enabled' if get_setting(i.guild_id,'module:logs','0')=='1' else 'disabled')]
        lines += [f'{name}: '+('enabled' if get_setting(i.guild_id,'logs:'+name,'1')=='1' else 'disabled') for name in categories]
        lines += ['Message logs contain IDs and event metadata; deleted/edited bodies and attachments are excluded.']
        await reply(i,'\n'.join(lines))
    register('status','Read saved event logging settings and privacy behavior',logs_status,logs)
    async def logs_enable(i:discord.Interaction,enabled:bool):
        staff_check(i,'manage_guild');put_setting(i.guild_id,'module:logs','1' if enabled else '0')
        await reply(i,'Event logging module setting saved.')
    register('enable','Enable or disable this server’s event logging',logs_enable,logs)
    async def logs_channel(i:discord.Interaction,channel:discord.TextChannel,event:str='all'):
        staff_check(i,'manage_guild')
        categories=('all','messages','members','roles','channels','moderation','security')
        if event not in categories:raise ValueError('Choose all, messages, members, roles, channels, moderation or security.')
        if not channel.permissions_for(i.guild.me).send_messages:raise ValueError('The bot cannot send messages in that channel.')
        if channel.permissions_for(i.guild.default_role).view_channel:raise ValueError('Choose a private staff channel to protect moderation and member information.')
        for category in categories[1:] if event=='all' else (event,):put_setting(i.guild_id,'logs:channel:'+category,channel.id)
        if event=='all':put_setting(i.guild_id,'moderation:log_channel',channel.id)
        await reply(i,'Private existing log destination saved.')
    register('channel','Select an existing private log channel for all or one event type',logs_channel,logs)
    def event_toggle(name):
        async def callback(i:discord.Interaction,enabled:bool):
            staff_check(i,'manage_guild');put_setting(i.guild_id,'logs:'+name,'1' if enabled else '0')
            await reply(i,name.capitalize()+' event logging setting saved.')
        register(name,'Enable or disable '+name+' event logs',callback,logs)
    for event in ('messages','members','roles','channels','moderation','security'):event_toggle(event)
    async def logs_ignore(i:discord.Interaction,channel:discord.TextChannel,ignored:bool):
        staff_check(i,'manage_guild');ids={x for x in get_setting(i.guild_id,'logs:ignored_channels','').split(',') if x}
        ids.add(str(channel.id)) if ignored else ids.discard(str(channel.id));put_setting(i.guild_id,'logs:ignored_channels',','.join(sorted(ids)))
        await reply(i,'Message log channel exclusion saved.')
    register('ignore-channel','Exclude an existing channel from message edit/delete metadata logs',logs_ignore,logs)

    reminders=app_commands.Group(name='reminder',description='Manage your own saved reminders',guild_only=True)
    bot.tree.remove_command('reminder');bot.tree.add_command(reminders)
    async def reminder_list(i:discord.Interaction):
        rows=db.execute("SELECT id,payload,due,status FROM mod_jobs WHERE guild=? AND target=? AND kind='reminder' AND status IN ('pending','failed') ORDER BY due LIMIT 15",(i.guild_id,i.user.id)).fetchall()
        await reply(i,'\n'.join(f'#{n} · <t:{int(d)}:R> · {status}: {json.loads(raw)["text"][:90]}' for n,raw,d,status in rows) or 'No pending or failed reminders in this server.')
    register('list','Read your own pending reminders for this server',reminder_list,reminders)
    async def reminder_cancel(i:discord.Interaction,reminder_id:int):
        cur=db.execute("UPDATE mod_jobs SET status='cancelled' WHERE guild=? AND target=? AND id=? AND kind='reminder' AND status IN ('pending','failed')",(i.guild_id,i.user.id,reminder_id));db.commit()
        await reply(i,'Reminder cancelled.' if cur.rowcount else 'No cancellable reminder with that ID belongs to you in this server.')
    register('cancel','Cancel one of your saved reminders in this server',reminder_cancel,reminders)

    async def ready():
        await service.ready()
    bot.add_listener(ready,'on_ready')
    bot.add_listener(service.on_message,'on_message')
    bot.add_listener(service.on_member_join,'on_member_join')
    bot.add_listener(service.on_audit_log_entry_create,'on_audit_log_entry_create')
    for event in ('on_message_delete','on_message_edit','on_member_update','on_member_remove',
                  'on_guild_channel_update','on_guild_channel_delete','on_guild_role_update','on_guild_role_delete'):
        bot.add_listener(getattr(service,event),event)

    # Preserve familiar prefix moderation commands using the same hierarchy-safe service.
    class PrefixInteraction:
        def __init__(self,ctx):
            self.guild,self.guild_id,self.user,self.channel=ctx.guild,ctx.guild.id,ctx.author,ctx.channel

    def prefix_action(name, action_name=None, timed=False):
        async def callback(ctx, member: discord.Member, *, reason: str='No reason supplied'):
            if not ctx.guild: return
            try:
                value='10m' if timed else None
                number=await service.execute_action(PrefixInteraction(ctx),action_name or name,member,reason,value)
                await ctx.reply(f'Action completed. Case #{number}.',allowed_mentions=discord.AllowedMentions.none())
            except ValueError as exc: await ctx.reply(str(exc),allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException: await ctx.reply('Discord denied the action. Check permissions and hierarchy.')
        bot.remove_command(name); bot.add_command(commands.Command(callback,name=name))
    for name in ('ban','kick','warn','deafen','undeafen'):
        prefix_action(name)
    prefix_action('unmute','untimeout')
    async def prefix_timeout(ctx, member: discord.Member, minutes: int=10, *, reason: str='No reason supplied'):
        if not ctx.guild: return
        try:
            if not 1<=minutes<=40320: raise ValueError('Choose 1–40320 minutes.')
            number=await service.execute_action(PrefixInteraction(ctx),'timeout',member,reason,f'{minutes}m')
            await ctx.reply(f'Timeout applied. Case #{number}.',allowed_mentions=discord.AllowedMentions.none())
        except ValueError as exc: await ctx.reply(str(exc),allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException: await ctx.reply('Discord denied the timeout. Check permissions and hierarchy.')
    for name in ('timeout','mute'):
        bot.remove_command(name); bot.add_command(commands.Command(prefix_timeout,name=name))

    async def prefix_purge(ctx,amount:int):
        if not ctx.guild: return
        try:
            if not 1<=amount<=100: raise ValueError('Choose between 1 and 100 messages.')
            count=await service.purge(PrefixInteraction(ctx),amount)
            await ctx.reply(f'Deleted {count} messages newer than seven days.',delete_after=10)
        except ValueError as exc: await ctx.reply(str(exc),allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException: await ctx.reply('Discord denied message deletion.')
    bot.remove_command('purge'); bot.add_command(commands.Command(prefix_purge,name='purge'))

    async def prefix_lock(ctx):
        if not ctx.guild:return
        try:
            number=await service.execute_action(PrefixInteraction(ctx),'lock',reason='Prefix channel lock')
            await ctx.reply(f'Channel locked. Case #{number}.')
        except ValueError as exc:await ctx.reply(str(exc))
        except discord.HTTPException:await ctx.reply('Discord denied the channel lock.')
    async def prefix_unlock(ctx):
        if not ctx.guild:return
        try:
            number=await service.execute_action(PrefixInteraction(ctx),'unlock',reason='Prefix channel unlock')
            await ctx.reply(f'Saved channel permissions restored. Case #{number}.')
        except ValueError as exc:await ctx.reply(str(exc))
        except discord.HTTPException:await ctx.reply('Discord denied the channel unlock.')
    for name,callback in [('lock',prefix_lock),('unlock',prefix_unlock)]:
        bot.remove_command(name);bot.add_command(commands.Command(callback,name=name))
    return service
