"""Bounded metadata extraction. Refresh expiring stream URLs at playback time."""
import asyncio
from dataclasses import dataclass
import os
from urllib.parse import urlsplit

import aiohttp
import yt_dlp

from .safe_audio import validate_url

MAX_PLAYLIST = 50
PLATFORMS = {'youtube.com', 'www.youtube.com', 'music.youtube.com', 'youtu.be',
             'soundcloud.com', 'www.soundcloud.com', 'm.soundcloud.com', 'on.soundcloud.com'}


@dataclass
class Track:
    title: str
    url: str
    requester: int
    artist: str = ''
    duration: int = 0
    thumbnail: str = ''
    matched: bool = False
    direct: bool = False
    search_query: str = ''
    metadata_url: str = ''


@dataclass
class AudioStream:
    url: str
    headers: dict


class QuietLogger:
    # yt-dlp errors can include signed URLs, IPs and provider response bodies.
    def debug(self, message):
        pass

    warning = debug
    error = debug


def options(**extra):
    return {
        'quiet': True, 'no_warnings': True, 'logger': QuietLogger(),
        'socket_timeout': 15, 'retries': 1, 'extractor_retries': 1,
        'cachedir': False, 'skip_download': True, 'playlistend': MAX_PLAYLIST,
        'allowed_extractors': ['youtube.*', 'soundcloud.*'],
        'js_runtimes': {'deno': {'path': (os.getenv('DENO_PATH') or 'deno')}},
        'remote_components': set(), **extra,
    }


class Resolver:
    def __init__(self):
        self.slots = asyncio.Semaphore(2)

    async def blocking(self, function, *args):
        # The worker retains its slot until it exits, even if the caller is cancelled.
        state={'started':False}
        async def run():
            async with self.slots:
                state['started']=True
                return await asyncio.to_thread(function, *args)
        task = asyncio.create_task(run())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Keep the semaphore until the thread exits and consume late failures.
            if not state['started']:task.cancel()
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            raise

    async def tracks(self, query, requester):
        query = query.strip()
        if not query or len(query) > 2000:
            raise ValueError('Provide a song name or supported URL (up to 2000 characters).')
        if '://' in query:
            parsed = validate_url(query)
            host = parsed.hostname.rstrip('.').lower()
            if host in ('open.spotify.com', 'www.open.spotify.com'):
                return await self.spotify(query, requester)
            if host not in PLATFORMS:
                return [Track(parsed.path.rsplit('/', 1)[-1] or host, query, requester, direct=True)]
            target = query
        else:
            if query.startswith(('file:', 'data:', 'ftp:', '//', 'ytsearch:')):
                raise ValueError('Unsupported query type.')
            target = 'ytsearch1:' + query
        return await self.blocking(self._tracks, target, requester)

    def _tracks(self, query, requester):
        try:
            with yt_dlp.YoutubeDL(options(extract_flat='in_playlist', noplaylist=False)) as ydl:
                info = ydl.extract_info(query, download=False)
            if not info:
                raise ValueError('No playable tracks found.')
            result = []
            entries = info.get('entries') if 'entries' in info else [info]
            for item in entries or []:
                if not item:
                    continue
                url = str(item.get('webpage_url') or item.get('url') or '')
                if not url.startswith('http') and item.get('id'):
                    url = 'https://www.youtube.com/watch?v=' + item['id']
                if (urlsplit(url).hostname or '').rstrip('.').lower() not in PLATFORMS:
                    continue
                result.append(Track(str(item.get('title') or 'Unknown track'), url, requester,
                                    str(item.get('artist') or item.get('uploader') or ''),
                                    int(item.get('duration') or 0), item.get('thumbnail') or ''))
                if len(result) == MAX_PLAYLIST:
                    break
            if not result:
                raise ValueError('No accessible tracks found in this request.')
            return result
        except yt_dlp.utils.DownloadError:
            raise ValueError('Source could not be resolved. It may be unavailable, restricted, or blocked by the provider.') from None

    async def stream(self, track):
        if track.direct:
            validate_url(track.url)
            return AudioStream(track.url,{})
        return await self.blocking(self._stream, track)

    def _stream(self, track):
        try:
            if track.search_query:
                # Match one Spotify item only when it reaches the front of the queue.
                # A 50-item playlist must not perform 50 searches before /play replies.
                match=self._tracks('ytsearch1:'+track.search_query,track.requester)[0]
                track.url=match.url
                track.search_query=''
            with yt_dlp.YoutubeDL(options(noplaylist=True,
                    format='bestaudio[ext=webm][protocol=https]/bestaudio[protocol=https]/bestaudio[protocol=http]/best[protocol=https]/best[protocol=http]')) as ydl:
                info = ydl.extract_info(track.url, download=False)
            if not info or not info.get('url') or info.get('is_live'):
                raise ValueError('No supported progressive audio stream; live streams are not supported.')
            validate_url(info['url'])
            track.title = str(info.get('title') or track.title)
            track.artist = str(info.get('artist') or info.get('uploader') or track.artist)
            track.duration = int(info.get('duration') or track.duration)
            track.thumbnail = info.get('thumbnail') or track.thumbnail
            return AudioStream(info['url'],info.get('http_headers') or {})
        except yt_dlp.utils.DownloadError:
            raise ValueError('Playback source is unavailable or access is restricted.') from None

    async def spotify(self, url, requester):
        client_id = os.getenv('SPOTIFY_CLIENT_ID')
        secret = os.getenv('SPOTIFY_CLIENT_SECRET')
        if not client_id or not secret:
            raise ValueError('Spotify metadata is not configured on the host. Use a song name, YouTube or SoundCloud URL.')
        parts = urlsplit(url).path.strip('/').split('/')
        if parts and parts[0].startswith('intl-'):
            parts.pop(0)
        if len(parts) != 2 or parts[0] not in ('track', 'playlist') or not parts[1].isalnum():
            raise ValueError('Use a Spotify track or playlist URL.')
        kind, identifier = parts
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post('https://accounts.spotify.com/api/token',
                    auth=aiohttp.BasicAuth(client_id, secret), data={'grant_type': 'client_credentials'}) as response:
                if response.status != 200:
                    raise ValueError('Spotify metadata authentication failed on the host.')
                token = (await response.json())['access_token']
            path = f'tracks/{identifier}' if kind == 'track' else f'playlists/{identifier}/items?limit=50'
            async with session.get('https://api.spotify.com/v1/' + path,
                    headers={'Authorization': 'Bearer ' + token}) as response:
                if response.status != 200:
                    raise ValueError(f'Spotify metadata is unavailable (HTTP {response.status}). Use a song search instead.')
                data = await response.json()
        metadata = [data] if kind == 'track' else [i.get('item') or i.get('track') for i in data.get('items', [])]
        matches = []
        for item in metadata[:MAX_PLAYLIST]:
            if not item or item.get('is_local') or not item.get('name') or item.get('type','track')!='track':
                continue
            artists = ', '.join(a['name'] for a in item.get('artists', []))
            metadata_url=item.get('external_urls',{}).get('spotify') or url
            matches.append(Track(str(item['name']),metadata_url,requester,artist=artists,
                duration=int(item.get('duration_ms') or 0)//1000,matched=True,
                search_query=f'{artists} {item["name"]} audio',metadata_url=metadata_url))
        if not matches:
            raise ValueError('No playable matches found for this Spotify metadata.')
        return matches
