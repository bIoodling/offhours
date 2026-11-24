"""User-invoked, read-only configuration report. No simulated Discord actions."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys

import discord
from discord import app_commands


def install(bot,connection,get_setting,put_setting):
    bot.tree.remove_command('safety-check')

    @bot.tree.command(name='safety-check',description='Privately inspect actual server configuration without changing Discord')
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def safety(i:discord.Interaction):
        if not i.guild or not i.user.guild_permissions.manage_guild:
            return await i.response.send_message('Manage Server permission is required.',ephemeral=True)
        await i.response.defer(ephemeral=True,thinking=True)
        guild=i.guild
        checks=[]
        def check(label,value,detail=''):
            checks.append(('PASS' if value else 'FAIL',label,detail))
        def info(label,detail=''):
            checks.append(('INFO',label,detail))
        def warning(label,detail=''):
            checks.append(('WARN',label,detail))
        check('Bot is connected to Discord gateway',bot.is_ready())
        check('Bot has a member object in this server',guild.me is not None)
        permissions=guild.me.guild_permissions if guild.me else discord.Permissions.none()
        for name in ('view_channel','send_messages','embed_links','attach_files','read_message_history','add_reactions',
                     'manage_messages','manage_channels','manage_roles','moderate_members','kick_members','ban_members',
                     'manage_guild','view_audit_log','connect','speak'):
            check('Bot server permission: '+name.replace('_',' '),getattr(permissions,name,False))
        if permissions.administrator:
            warning('Bot has Administrator permission','The permission is broader than individual features require.')
        else:
            info('Bot does not have Administrator permission','Individual required permissions are checked separately.')
        check('Python 3.12 host runtime',sys.version_info[:2]==(3,12),f'Runtime: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')
        for module,label in [('discord','Discord client'),('nacl','Voice transport encryption'),('davey','Discord DAVE encryption'),
                             ('yt_dlp','Track resolver'),('yt_dlp_ejs','YouTube JavaScript solver'),('aiohttp','Asynchronous HTTP'),('imageio_ffmpeg','Bundled FFmpeg fallback')]:
            try:available=importlib.util.find_spec(module) is not None
            except (ImportError,ValueError,ModuleNotFoundError):available=False
            check(label+' package present',available,'Presence does not prove live functionality.')
        try:
            from services.audio_runtime import ffmpeg_path
            executable=ffmpeg_path()
            check('Configured or bundled FFmpeg executable exists',Path(executable).is_file())
        except Exception:
            check('Configured or bundled FFmpeg executable exists',False,'Run the repository host installation/startup script.')
        check('Deno JavaScript runtime is discoverable',bool(shutil.which((os.getenv('DENO_PATH') or 'deno'))))
        info('Audible voice playback','NOT VERIFIED. This report never joins voice, resolves a track, sends a tone or claims that anyone heard audio.')
        voice=guild.voice_client
        info('Current voice connection','Connected' if voice and voice.is_connected() else 'Not connected')
        if voice:
            info('Successful local audio packet sends',str(getattr(voice,'audio_packets',0))+'; local sends do not prove remote audibility.')
        integrations=[('AI provider','OPENAI_API_KEY'),('Twitch client ID','TWITCH_CLIENT_ID'),('Twitch client secret','TWITCH_CLIENT_SECRET'),
                      ('YouTube API','YOUTUBE_API_KEY'),('Spotify client ID','SPOTIFY_CLIENT_ID'),('Spotify client secret','SPOTIFY_CLIENT_SECRET')]
        for label,key in integrations:
            info(label+' host credential','Configured' if bool(os.getenv(key,'')) else 'Not configured; integration remains unavailable until configured.')
        # Read the current connection only; do not create records or run a write test.
        try:
            check('Database read query succeeds',connection.execute('SELECT 1').fetchone()==(1,))
            journal=connection.execute('PRAGMA journal_mode').fetchone()[0]
            info('Database journal mode',journal)
            table_names={r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ('settings','xp','warnings','colours','mod_cases','mod_notes','mod_jobs','security_incidents','activity_metrics','onboarding_selections'):
                if table not in table_names:
                    warning('Database table: '+table,'Table is not installed in this version.')
                    continue
                columns={r[1] for r in connection.execute('PRAGMA table_info('+table+')')}
                check('Server ID column exists in '+table,'guild' in columns or 'guild_id' in columns)
                if 'guild' in columns:
                    count=connection.execute('SELECT COUNT(*) FROM '+table+' WHERE guild=?',(guild.id,)).fetchone()[0]
                    info('This server’s '+table+' record count',str(count))
            info('Database isolation limit','Schema and this server’s read queries are inspected. This report cannot prove every code path or multi-server permission behavior.')
        except Exception as exc:
            check('Database configuration inspection',False,type(exc).__name__)
            table_names=set()
        settings=dict(connection.execute('SELECT key,value FROM settings WHERE guild=?',(guild.id,)))
        for key,value in sorted(settings.items()):
            if key.startswith('module:'):
                check('Feature flag valid: '+key[7:],value in ('0','1'))
                info('Feature '+key[7:],'Enabled' if value=='1' else 'Disabled')
        role_prefixes=('role:','security:quarantine_role')
        for key,value in sorted(settings.items()):
            if not key.startswith('channel:') or value in ('','0'):continue
            channel=guild.get_channel(int(value)) if str(value).isdecimal() else None
            check('Configured destination exists: '+key[8:],channel is not None)
            if not channel:continue
            p=channel.permissions_for(guild.me)
            for name in ('view_channel','send_messages','embed_links','attach_files','read_message_history'):
                check(f'{key[8:]} destination permission: '+name.replace('_',' '),getattr(p,name,False))
            if key in ('channel:logs','channel:reports'):
                check('Private staff destination: '+key[8:],not channel.permissions_for(guild.default_role).view_channel)
            if isinstance(channel,discord.VoiceChannel):
                check(key[8:]+' voice connect permission',p.connect)
                check(key[8:]+' voice speak permission',p.speak)
        for key,value in sorted(settings.items()):
            if not key.startswith(role_prefixes) or value in ('','0'):continue
            role=guild.get_role(int(value)) if str(value).isdecimal() else None
            check('Configured role exists: '+key,role is not None)
            if not role:continue
            check('Configured role is not @everyone: '+key,not role.is_default())
            check('Configured role is not integration-managed: '+key,not role.managed)
            check('Configured role is below bot: '+key,role<guild.me.top_role)
            if key in ('role:regular','role:pending','role:autorole','security:quarantine_role'):
                dangerous=any((role.permissions.administrator,role.permissions.manage_guild,role.permissions.manage_roles,
                    role.permissions.manage_channels,role.permissions.ban_members,role.permissions.kick_members))
                check('Automatically assigned role has no administrative powers: '+key,not dangerous)
        for key,value in sorted(settings.items()):
            if key.startswith('command:'):
                try:
                    policy=json.loads(value)
                    check('Command policy is structured: '+key[8:],isinstance(policy,dict))
                    if not isinstance(policy,dict):continue
                    for role_id in policy.get('allowed_roles',[])+policy.get('blocked_roles',[]):
                        check('Command policy role belongs to this server: '+key[8:],guild.get_role(int(role_id)) is not None)
                    for channel_id in policy.get('allowed_channels',[])+policy.get('blocked_channels',[]):
                        check('Command policy channel belongs to this server: '+key[8:],guild.get_channel(int(channel_id)) is not None)
                except (ValueError,TypeError,KeyError):
                    check('Command policy is structured: '+key[8:],False)
        try:
            for table in ('tickets','ticket_records'):
                if table not in table_names:continue
                columns={r[1] for r in connection.execute('PRAGMA table_info('+table+')')}
                guild_col='guild' if 'guild' in columns else 'guild_id' if 'guild_id' in columns else None
                channel_col='channel' if 'channel' in columns else 'channel_id' if 'channel_id' in columns else None
                if not guild_col or not channel_col:continue
                rows=connection.execute('SELECT '+channel_col+' FROM '+table+' WHERE '+guild_col+'=? LIMIT 100',(guild.id,)).fetchall()
                for number,(identifier,) in enumerate(rows,1):
                    channel=guild.get_channel(int(identifier)) if str(identifier).isdecimal() else None
                    if channel:
                        check('Ticket channel '+str(number)+' denies @everyone visibility',not channel.permissions_for(guild.default_role).view_channel)
                info('Ticket privacy inspection limit','Only @everyone access on up to 100 existing ticket channels is inspected. Staff/member-specific overwrites require live review.')
        except Exception as exc:
            warning('Ticket permission snapshot inspection',type(exc).__name__)
        info('Native onboarding','Use /onboarding status to fetch actual Discord state. A configured draft is not evidence that native onboarding is active.')
        info('Social provider connectivity','NOT VERIFIED. No provider calls or live notifications were sent by this report.')
        info('Member permission behavior','NOT VERIFIED. No impersonation, joins, bans, role assignment or channel mutations were performed.')
        totals={kind:sum(row[0]==kind for row in checks) for kind in ('PASS','FAIL','WARN','INFO')}
        lines=['OFFHOURS / READ-ONLY SERVER CONFIGURATION REPORT',
            'Generated: '+discord.utils.utcnow().isoformat(),'Server ID: '+str(guild.id),
            f'Configuration checks: {totals["PASS"]} passed, {totals["FAIL"]} failed, {totals["WARN"]} warnings.',
            'Informational entries are not counted as passed tests.','',
            *[f'{kind}  {label}'+('\n      '+detail if detail else '') for kind,label,detail in checks],
            '', 'This report is a configuration snapshot, not a release-certification or external/live test report.']
        payload='\n'.join(lines).encode('utf-8')
        await i.followup.send(f'Read-only report: {totals["PASS"]} checks passed, {totals["FAIL"]} failed, {totals["WARN"]} warnings. Live behavior remains unverified.',
            file=discord.File(io.BytesIO(payload),filename='offhours-safety-report.txt'),ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

    return safety
