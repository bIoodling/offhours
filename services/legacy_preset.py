"""Migrate only the explicitly identified old server; never mutate Discord objects."""
import json
import os
from pathlib import Path
import re
import time
import unicodedata


def normal(value):
    text = unicodedata.normalize('NFKC', value).casefold()
    return re.sub(r'[^a-z0-9]+', '', text)


def migrate_legacy(guild, con, get, put):
    if get(guild.id, 'migration:public_bot', '0') == '1':
        return
    preset = json.loads((Path(__file__).parent.parent / 'presets' / 'offhours.json').read_text(encoding='utf-8'))
    existing = dict(con.execute('SELECT key,value FROM settings WHERE guild=?', (guild.id,)))
    put(guild.id, 'preset:legacy_snapshot', json.dumps(existing))
    for key, names in preset['channels'].items():
        if get(guild.id, 'channel:' + key):
            continue
        matches = [c for c in guild.text_channels if normal(c.name) in {normal(n) for n in names}]
        if len(matches) == 1:
            put(guild.id, 'channel:' + key, matches[0].id)
    for name in preset['roles']:
        matches = [r for r in guild.roles if normal(r.name) == normal(name)]
        if len(matches) == 1 and not get(guild.id, 'role:' + name):
            put(guild.id, 'role:' + name, matches[0].id)
    rewards = {}
    for threshold in range(5, 151, 5):
        matches = [r for r in guild.roles if normal(r.name) == normal(f'level {threshold}')]
        if len(matches) == 1:
            rewards[str(threshold)] = matches[0].id
    if rewards and not get(guild.id, 'levels:rewards'):
        put(guild.id, 'levels:rewards', json.dumps(rewards))
    for name in preset['colours']:
        matches = [r for r in guild.roles if normal(r.name) == normal('colour ' + name)]
        if len(matches) == 1:
            put(guild.id, 'selfrole:colour:' + name, matches[0].id)
    for name in preset['regions']:
        matches = [r for r in guild.roles if normal(r.name) == normal('region ' + name)]
        if len(matches) == 1:
            put(guild.id, 'selfrole:region:' + name, matches[0].id)
    allowed = [c.id for c in guild.voice_channels if normal(c.name) in ('general', 'afk')]
    if allowed and not get(guild.id, 'music:channels'):
        put(guild.id, 'music:channels', ','.join(map(str, allowed)))
    for module in ('levels', 'welcome', 'verification', 'selfroles'):
        if not get(guild.id, 'module:' + module):
            put(guild.id, 'module:' + module, '1')
    if not get(guild.id, 'social:twitch_login'):
        put(guild.id, 'social:twitch_login', os.getenv('TWITCH_LOGIN') or preset['twitch_login'])
    for kind, slot, title, description in [
        ('welcome','welcome','welcome to offhours','{user.mention} · member #{server.member_count}'),
        ('level','levels','level up / {level}','{user.mention} reached level {level}.'),
        ('verify','verify','verification','Press Verify to unlock this server.'),
    ]:
        if not get(guild.id,'panel:'+kind):
            destination = get(guild.id,'channel:'+slot,'0')
            regular = get(guild.id,'role:regular','0')
            put(guild.id,'panel:'+kind,json.dumps({'enabled':True,'channel_id':int(destination or 0),
                'title':title,'description':description,'colour':'E5E5E5','show_avatar':True,
                'banner':os.getenv('WELCOME_BANNER_URL','') if kind=='welcome' else '',
                'role_ids':[int(regular)] if kind=='verify' and regular.isdecimal() and int(regular) else []}))
    for key,value in [('levels:link_level','20'),('levels:reaction_level','10')]:
        if not get(guild.id,key):put(guild.id,key,value)
    put(guild.id, 'migration:public_bot', '1')
