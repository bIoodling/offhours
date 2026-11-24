"""Official Twitch/YouTube polling and an explicit supported TikTok provider adapter."""
import asyncio
import contextlib
import datetime
import hashlib
import json
import logging
import os
import re
import time
from urllib.parse import urlsplit

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from services.configuration import administrator, enabled, channel_for, role_for, private

log=logging.getLogger('offhours.social')


class Social:
    def __init__(self,bot,con,get,put):
        self.bot,self.con,self.get,self.put=bot,con,get,put
        self.token='';self.token_expiry=0;self.next_scan=0;self.lock=asyncio.Lock()
        self.errors={};self.cache={};self.pending={};self.live_cache={};self.playlists={};self.retry_after={}
        con.executescript('''
        CREATE TABLE IF NOT EXISTS social_seen(guild INTEGER,platform TEXT,event TEXT,kind TEXT,created REAL,PRIMARY KEY(guild,platform,event,kind));
        CREATE TABLE IF NOT EXISTS social_outbox(id INTEGER PRIMARY KEY AUTOINCREMENT,guild INTEGER,platform TEXT,account TEXT,event TEXT,kind TEXT,url TEXT,title TEXT,due REAL,created REAL,UNIQUE(guild,platform,event,kind));
        CREATE TABLE IF NOT EXISTS social_history(id INTEGER PRIMARY KEY AUTOINCREMENT,guild INTEGER,platform TEXT,event TEXT,kind TEXT,channel INTEGER,message INTEGER,created REAL);
        CREATE TABLE IF NOT EXISTS social_quota(day TEXT PRIMARY KEY,units INTEGER DEFAULT 0);
        ''');con.commit()

    def accounts(self,guild,platform):
        try:data=json.loads(self.get(guild,'social:'+platform,'[]'))
        except ValueError:data=[]
        legacy=self.get(guild,'social:'+{'twitch':'twitch_login','youtube':'youtube_channel','tiktok':'tiktok_username'}[platform],'')
        if legacy and legacy not in data:data.append(legacy)
        return [x for x in data if isinstance(x,str)][:5]

    async def request(self,session,url,*,params=None,headers=None,method='get',data=None):
        host=urlsplit(url).hostname
        if time.time()<self.retry_after.get(host,0):
            raise ValueError('Provider rate-limit cooldown is active.')
        async with session.request(method,url,params=params,headers=headers,data=data,allow_redirects=False) as response:
            if response.status==429:
                try:seconds=max(1,min(3600,float(response.headers.get('Retry-After','180'))))
                except ValueError:seconds=180
                self.retry_after[host]=time.time()+seconds
                raise ValueError('Provider rate limit reached; retry is delayed automatically.')
            if response.status==401 and host=='api.twitch.tv':
                self.token_expiry=0
            if response.status!=200:
                raise ValueError(f'Provider returned HTTP {response.status}.')
            return await response.json(content_type=None)

    async def twitch(self,session,account):
        client=os.getenv('TWITCH_CLIENT_ID','');secret=os.getenv('TWITCH_CLIENT_SECRET','')
        if not client or not secret:raise ValueError('Twitch host credentials are not configured.')
        if time.time()>=self.token_expiry:
            data=await self.request(session,'https://id.twitch.tv/oauth2/token',method='post',
                data={'client_id':client,'client_secret':secret,'grant_type':'client_credentials'})
            self.token=data['access_token'];self.token_expiry=time.time()+int(data['expires_in'])-60
        data=await self.request(session,'https://api.twitch.tv/helix/streams',params={'user_login':account},
            headers={'Client-ID':client,'Authorization':'Bearer '+self.token})
        return [{'id':x['id'],'type':'live','url':'https://www.twitch.tv/'+x['user_login'],'title':x.get('title','Live now')}
                for x in data.get('data',[])]

    async def youtube_get(self,session,resource,params,cost=1):
        key=os.getenv('YOUTUBE_API_KEY','')
        if not key:raise ValueError('YouTube host API key is not configured.')
        day=datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        used=self.con.execute('SELECT units FROM social_quota WHERE day=?',(day,)).fetchone()
        if (used[0] if used else 0)+cost>int(os.getenv('YOUTUBE_DAILY_QUOTA_BUDGET','9000')):
            raise ValueError('Configured daily YouTube quota budget reached.')
        self.con.execute('INSERT INTO social_quota(day,units) VALUES(?,?) ON CONFLICT(day) DO UPDATE SET units=units+excluded.units',(day,cost));self.con.commit()
        return await self.request(session,'https://www.googleapis.com/youtube/v3/'+resource,params={**params,'key':key})

    async def youtube(self,session,account):
        if account not in self.playlists:
            data=await self.youtube_get(session,'channels',{'id':account,'part':'snippet,contentDetails'})
            if not data.get('items'):raise ValueError('YouTube channel ID could not be verified.')
            self.playlists[account]=data['items'][0]['contentDetails']['relatedPlaylists']['uploads']
        data=await self.youtube_get(session,'playlistItems',{'playlistId':self.playlists[account],'part':'snippet,contentDetails','maxResults':5})
        result=[]
        for item in data.get('items',[]):
            snippet=item.get('snippet',{});identifier=item.get('contentDetails',{}).get('videoId')
            if identifier:
                result.append({'id':identifier,'type':'video','url':'https://www.youtube.com/watch?v='+identifier,'title':snippet.get('title','New video')})
        # Official live search costs 100 quota units: cache for thirty minutes globally.
        timestamp,live=self.live_cache.get(account,(0,[]))
        if time.time()-timestamp>=1800:
            try:
                data=await self.youtube_get(session,'search',{'channelId':account,'part':'snippet','eventType':'live','type':'video','maxResults':5},100)
                live=[{'id':x['id']['videoId'],'type':'live','url':'https://www.youtube.com/watch?v='+x['id']['videoId'],
                       'title':x.get('snippet',{}).get('title','Live now')} for x in data.get('items',[]) if x.get('id',{}).get('videoId')]
                self.live_cache[account]=(time.time(),live)
            except ValueError as exc:
                self.live_cache[account]=(time.time()-1500,live)
                log.warning('YouTube live search deferred: %s',type(exc).__name__)
        active={x['id'] for x in live}
        return [x for x in result if x['id'] not in active]+live

    async def tiktok(self,session,account):
        endpoint=os.getenv('TIKTOK_PROVIDER_URL','');token=os.getenv('TIKTOK_PROVIDER_TOKEN','')
        parsed=urlsplit(endpoint)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('No supported TikTok monitoring provider is configured. Generic TikTok LIVE polling is unavailable through the public Display API.')
        payload=await self.request(session,endpoint,params={'username':account},headers={'Authorization':'Bearer '+token} if token else None)
        events=[]
        for value in payload.get('events',[])[:20]:
            link=value.get('url','');host=urlsplit(link).hostname or ''
            if (value.get('type') not in ('live','video') or not value.get('id') or
                    urlsplit(link).scheme!='https' or not (host=='tiktok.com' or host.endswith('.tiktok.com'))):continue
            if value.get('type')=='live' and value.get('live') is not True:continue
            events.append({'id':str(value['id'])[:100],'type':value['type'],'url':link[:1000],'title':str(value.get('title',''))[:200]})
        return events

    async def scan(self):
        async with self.lock:
            self.cache={}
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
                for guild in self.bot.guilds:
                    if not enabled(self.get,guild.id,'social'):continue
                    for platform in ('twitch','youtube','tiktok'):
                        for account in self.accounts(guild.id,platform):
                            key=(platform,account)
                            try:
                                if key not in self.cache:
                                    try:self.cache[key]=await getattr(self,platform)(session,account)
                                    except Exception as error:self.cache[key]=error
                                if isinstance(self.cache[key],Exception):raise self.cache[key]
                                events=self.cache[key]
                                seeded=self.get(guild.id,f'social:seeded:{platform}:{account}','0')=='1'
                                for event in events:
                                    seen=self.con.execute('SELECT 1 FROM social_seen WHERE guild=? AND platform=? AND event=? AND kind=?',
                                        (guild.id,platform,event['id'],event['type'])).fetchone()
                                    if seen:continue
                                    if not seeded:
                                        self.con.execute('INSERT OR IGNORE INTO social_seen VALUES(?,?,?,?,?)',(guild.id,platform,event['id'],event['type'],time.time()))
                                    else:
                                        self.con.execute('INSERT OR IGNORE INTO social_outbox(guild,platform,account,event,kind,url,title,due,created) VALUES(?,?,?,?,?,?,?,?,?)',
                                            (guild.id,platform,account,event['id'],event['type'],event['url'],event['title'],time.time()+45,time.time()))
                                self.con.commit();self.put(guild.id,f'social:seeded:{platform}:{account}','1')
                                self.errors.pop((guild.id,platform,account),None)
                            except Exception as exc:
                                self.errors[(guild.id,platform,account)]=str(exc)[:160] if isinstance(exc,ValueError) else type(exc).__name__
                                log.warning('Social scan failed guild=%s platform=%s type=%s',guild.id,platform,type(exc).__name__)

    async def flush(self):
        rows=self.con.execute('SELECT guild,kind FROM social_outbox WHERE due<=? GROUP BY guild,kind LIMIT 30',(time.time(),)).fetchall()
        for guild_id,kind in rows:
            guild=self.bot.get_guild(guild_id)
            if not guild or not enabled(self.get,guild_id,'social'):continue
            events=self.con.execute('SELECT id,platform,event,url,title FROM social_outbox WHERE guild=? AND kind=? AND due<=? ORDER BY created LIMIT 10',(guild_id,kind,time.time())).fetchall()
            slot='live' if kind=='live' else 'video'
            channel=channel_for(guild,self.get,slot)
            try:
                panel=json.loads(self.get(guild_id,'panel:'+slot,'{}'))
            except ValueError:panel={}
            if not channel and panel.get('channel_id'):channel=guild.get_channel(int(panel['channel_id']))
            if not isinstance(channel,discord.TextChannel):continue
            role=role_for(guild,self.get,slot)
            body=self.get(guild_id,'social:'+slot+'_message','Live now:' if kind=='live' else 'New video:')
            text=(role.mention+'\n' if role else '')+body+'\n'+'\n'.join(dict.fromkeys(x[3] for x in events))
            nonce=int.from_bytes(hashlib.sha256((':'.join(str(x[0]) for x in events)).encode()).digest()[:8],'big') & ((1<<63)-1)
            try:
                message=await channel.send(text[:1900],nonce=nonce,allowed_mentions=discord.AllowedMentions(everyone=False,users=False,roles=[role] if role else False))
            except discord.HTTPException:
                log.warning('Social notification send failed guild=%s',guild_id)
                continue
            for identifier,platform,event,url,title in events:
                self.con.execute('INSERT OR IGNORE INTO social_seen VALUES(?,?,?,?,?)',(guild_id,platform,event,kind,time.time()))
                self.con.execute('INSERT INTO social_history(guild,platform,event,kind,channel,message,created) VALUES(?,?,?,?,?,?,?)',(guild_id,platform,event,kind,channel.id,message.id,time.time()))
                self.con.execute('DELETE FROM social_outbox WHERE id=? AND guild=?',(identifier,guild_id))
            self.con.commit()

    @tasks.loop(seconds=15)
    async def loop(self):
        try:
            if time.time()>=self.next_scan:
                self.next_scan=time.time()+180
                await self.scan()
            await self.flush()
        except Exception as exc:log.warning('Social scheduler failed: %s',type(exc).__name__)

    @loop.before_loop
    async def ready(self):await self.bot.wait_until_ready()

    async def close(self):self.loop.cancel()


def install(bot,con,get,put):
    service=Social(bot,con,get,put)
    group=app_commands.Group(name='social',description='Configure supported live and video notifications');bot.tree.add_command(group)
    async def authorize(i):
        if administrator(i.user):return True
        await private(i,'Server Administrator permission is required.');return False

    def account_commands(platform):
        async def add(i:discord.Interaction,account:str):
            if not await authorize(i):return
            account=account.strip().lstrip('@')
            expression=r'UC[A-Za-z0-9_-]{22}' if platform=='youtube' else r'[A-Za-z0-9_.]{1,50}'
            if not re.fullmatch(expression,account):return await private(i,'Use a valid YouTube channel ID beginning UC.' if platform=='youtube' else 'Use an account username without a URL.')
            values=service.accounts(i.guild_id,platform)
            if account not in values:values.append(account)
            if len(values)>5:return await private(i,'Maximum five accounts per platform per server.')
            put(i.guild_id,'social:'+platform,json.dumps(values));await private(i,'Account saved. Enable Social in /setup and select notification channels. Existing content is baselined without startup pings.')
        async def remove(i:discord.Interaction,account:str):
            if not await authorize(i):return
            values=[x for x in service.accounts(i.guild_id,platform) if x!=account.strip().lstrip('@')]
            put(i.guild_id,'social:'+platform,json.dumps(values))
            put(i.guild_id,'social:'+{'twitch':'twitch_login','youtube':'youtube_channel','tiktok':'tiktok_username'}[platform],'')
            await private(i,'Account removed from this server’s monitoring list.')
        async def list_accounts(i:discord.Interaction):
            if await authorize(i):await private(i,'\n'.join(service.accounts(i.guild_id,platform)) or 'No accounts configured.')
        for suffix,fn in [('add',add),('remove',remove),('list',list_accounts)]:
            group.add_command(app_commands.Command(name=platform+'-'+suffix,description=suffix.title()+' '+platform+' notification account',callback=fn))
    for platform in ('twitch','youtube','tiktok'):account_commands(platform)

    def channel_command(slot):
        async def callback(i:discord.Interaction,channel:discord.TextChannel):
            if await authorize(i):put(i.guild_id,'channel:'+slot,channel.id);await private(i,'Notification destination saved.')
        group.add_command(app_commands.Command(name=slot+'-channel',description='Set the '+slot+' notification destination',callback=callback))
    def role_command(slot):
        async def callback(i:discord.Interaction,role:discord.Role,active:bool=True):
            if await authorize(i):
                if role.is_default():return await private(i,'Select a dedicated notification role instead of @everyone.')
                put(i.guild_id,'role:'+slot,role.id if active else 0);await private(i,'Notification role saved.')
        group.add_command(app_commands.Command(name=slot+'-role',description='Set the '+slot+' ping role',callback=callback))
    def message_command(slot):
        async def callback(i:discord.Interaction,message:str):
            if await authorize(i):put(i.guild_id,'social:'+slot+'_message',message[:500]);await private(i,'Notification text saved.')
        group.add_command(app_commands.Command(name=slot+'-message',description='Set the text before '+slot+' links',callback=callback))
    for slot in ('live','video'):
        channel_command(slot);role_command(slot);message_command(slot)

    @group.command(name='status',description='Read provider configuration and actual monitoring errors')
    async def status(i:discord.Interaction):
        if not await authorize(i):return
        lines=['Social module: '+('enabled' if enabled(get,i.guild_id,'social') else 'disabled'),
            'Twitch API credentials: '+('configured' if os.getenv('TWITCH_CLIENT_ID') and os.getenv('TWITCH_CLIENT_SECRET') else 'missing'),
            'YouTube API key: '+('configured' if os.getenv('YOUTUBE_API_KEY') else 'missing'),
            'TikTok: '+('provider adapter configured' if os.getenv('TIKTOK_PROVIDER_URL') else 'public generic live monitoring unsupported; provider adapter required')]
        lines.extend(f'{platform}/{account}: {error}' for (guild,platform,account),error in service.errors.items() if guild==i.guild_id)
        await private(i,'\n'.join(lines))

    @group.command(name='refresh',description='Poll configured providers now without sending test messages')
    async def refresh(i:discord.Interaction):
        if not await authorize(i):return
        now=time.time()
        if now-float(get(i.guild_id,'social:last_refresh','0'))<180:return await private(i,'Wait three minutes between manual refreshes.')
        put(i.guild_id,'social:last_refresh',now)
        await i.response.defer(ephemeral=True);await service.scan();await private(i,'Provider scan completed. Check /social status for failures; new events use the deduplication queue.')

    @group.command(name='history',description='View notifications confirmed by Discord in this server')
    async def history(i:discord.Interaction):
        if not await authorize(i):return
        rows=con.execute('SELECT platform,kind,channel,message,created FROM social_history WHERE guild=? ORDER BY id DESC LIMIT 15',(i.guild_id,)).fetchall()
        await private(i,'\n'.join(f'{p} {k}: https://discord.com/channels/{i.guild_id}/{c}/{m} — <t:{int(t)}:R>' for p,k,c,m,t in rows) or 'No confirmed notifications recorded.')

    def preview_command(slot):
        async def callback(i:discord.Interaction):
            if not await authorize(i):return
            channel=channel_for(i.guild,get,slot)
            role=role_for(i.guild,get,slot)
            text=get(i.guild_id,'social:'+slot+'_message','Live now:' if slot=='live' else 'New video:')
            await private(i,'PRIVATE PREVIEW — no event simulated and no notification sent.\nDestination: '+(channel.mention if channel else 'not configured')+'\nRole: '+(role.name if role else 'none')+'\n'+text+'\n[the real event link will appear here]')
        group.add_command(app_commands.Command(name='test-'+slot,description='Privately preview '+slot+' notification formatting',callback=callback))
    for slot in ('live','video'):preview_command(slot)

    async def ready():
        if not service.loop.is_running():service.loop.start()
    bot.add_listener(ready,'on_ready')
    return service
