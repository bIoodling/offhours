"""One playback worker per guild; no recursive queue advancement or stale callbacks."""
import asyncio
from collections import deque
import contextlib
import logging
import random
import threading
import time

import discord

from .audio_runtime import ffmpeg_path
from .safe_audio import AudioReader

log = logging.getLogger('offhours.music')
MAX_QUEUE = 100


class MeteredVoiceClient(discord.VoiceClient):
    """Counts successful local packet sends, never claims that a listener heard them."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.audio_packets = 0
        self.dropped_audio_packets = 0

    def send_audio_packet(self, data, *, encode=True):
        # discord.py swallows UDP send errors in the base method; counting its
        # return would incorrectly label dropped packets as successful sends.
        self.checked_add('sequence',1,65535)
        encoded=self.encoder.encode(data,self.encoder.SAMPLES_PER_FRAME) if encode else data
        packet=self._get_voice_packet(encoded)
        try:
            self._connection.send_packet(packet)
        except OSError:
            self.dropped_audio_packets+=1
        else:
            self.audio_packets+=1
        finally:
            self.checked_add('timestamp',discord.opus.Encoder.SAMPLES_PER_FRAME,4294967295)


class StreamSource(discord.AudioSource):
    def __init__(self, reader, volume, offset=0):
        self.reader = reader
        self.frames = 0
        self.offset = offset
        self.audio = None
        self.cleaned = threading.Event()
        self.cleanup_lock = threading.Lock()
        try:
            self.audio = discord.FFmpegOpusAudio(
                reader, executable=ffmpeg_path(), pipe=True, bitrate=96,
                before_options='-nostdin -protocol_whitelist pipe -threads 1 -filter_threads 1 -probesize 1048576 -analyzeduration 5000000 -max_alloc 67108864',
                options=f'-vn -ss {max(0, offset):.2f} -af volume={volume:.2f} -application audio -frame_duration 20',
            )
        except BaseException:
            reader.close()
            raise

    def read(self):
        packet = self.audio.read()
        # Ogg transport headers are metadata, not decodable Discord Opus frames.
        while packet.startswith((b'OpusHead',b'OpusTags')):
            packet=self.audio.read()
        if packet:
            self.frames += 1
        return packet

    def is_opus(self):
        return True

    @property
    def _current_error(self):
        return self.reader.error or getattr(self.audio,'_current_error',None)

    def cleanup(self):
        # AudioPlayer and the queue worker may both call cleanup after an interrupt.
        with self.cleanup_lock:
            if self.cleaned.is_set():return
            try:
                self.reader.close()
                if self.audio:self.audio.cleanup()
            finally:
                self.cleaned.set()


class Player:
    def __init__(self, guild, resolver, view_factory):
        self.guild = guild
        self.voice = None
        self.resolver = resolver
        self.view_factory = view_factory
        self.queue = deque()
        self.history = deque(maxlen=30)
        self.current = None
        self.lock = asyncio.Lock()
        self.panel_lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.done = asyncio.Event()
        self.revision = 0
        self.closed = False
        self.volume = 0.5
        self.loop = 'off'
        self.source = None
        self.message = None
        self.channel = None
        self.restart = None
        self.status = 'Idle'
        self.task = None
        self.packets_at_start = 0

    async def enqueue(self, channel, tracks):
        async with self.lock:
            if self.closed:
                raise ValueError('Player stopped; run /play again.')
            if len(self.queue) + len(tracks) > MAX_QUEUE:
                raise ValueError(f'Queue limit is {MAX_QUEUE} tracks. Request a smaller playlist.')
            voice = self.guild.voice_client
            if voice and voice.channel.id != channel.id:
                raise ValueError('The player is already being used in another voice channel.')
            if not voice:
                permissions = channel.permissions_for(self.guild.me)
                if not permissions.connect or not permissions.speak:
                    raise ValueError('The bot needs Connect and Speak permissions in this voice channel.')
                voice = await channel.connect(cls=MeteredVoiceClient, timeout=20, reconnect=True, self_deaf=True)
            if not voice.is_connected():
                raise ValueError('Discord voice connection is not ready. Try again shortly.')
            if self.guild.me.voice and (self.guild.me.voice.mute or self.guild.me.voice.suppress):
                raise ValueError('Discord has server-muted or suppressed the bot. Staff must allow it to speak.')
            self.voice = voice
            self.channel = channel
            self.queue.extend(tracks)
            self.wake.set()
            if self.task is None or self.task.done():
                self.task = asyncio.create_task(self.run(), name=f'music-{self.guild.id}')

    async def publish(self):
        if not self.channel:
            return
        async with self.panel_lock:
            embed = self.embed()
            try:
                if self.message:
                    await self.message.edit(embed=embed, view=self.view_factory(),allowed_mentions=discord.AllowedMentions.none())
                else:
                    self.message = await self.channel.send(embed=embed, view=self.view_factory(),
                        allowed_mentions=discord.AllowedMentions.none(), silent=True)
            except discord.NotFound:
                self.message = None
            except discord.HTTPException:
                log.warning('Player panel could not be updated for guild %s', self.guild.id)

    def embed(self):
        track = self.current
        title = discord.utils.escape_markdown(track.title)[:250] if track else 'Queue empty'
        embed = discord.Embed(title='offhours / music', description=title, colour=0xD4D4D4)
        embed.add_field(name='Status', value=self.status, inline=False)
        if track:
            embed.add_field(name='Artist', value=discord.utils.escape_markdown(track.artist)[:200] or 'Unknown')
            embed.add_field(name='Requester', value=f'<@{track.requester}>')
            progress = self.source.offset + self.source.frames * .02 if self.source else 0
            embed.add_field(name='Progress', value=f'{int(progress)//60}:{int(progress)%60:02d} / '
                + (f'{track.duration//60}:{track.duration%60:02d}' if track.duration else 'unknown'))
            if track.thumbnail.startswith('https://'):
                embed.set_thumbnail(url=track.thumbnail)
            if track.matched:
                embed.set_footer(text='Spotify metadata match; the recording may differ.')
                if track.metadata_url.startswith('https://open.spotify.com/'):
                    embed.add_field(name='Metadata source',value='[Spotify]('+track.metadata_url+')')
        embed.add_field(name='Queue', value=str(len(self.queue)))
        embed.add_field(name='Volume', value=f'{round(self.volume * 100)}%')
        embed.add_field(name='Loop', value=self.loop)
        return embed

    def _interrupt(self):
        self.revision += 1
        voice = self.guild.voice_client
        if self.voice is not None and voice is not self.voice:
            voice = None
        if voice and (voice.is_playing() or voice.is_paused()):
            voice.stop()
        self.done.set()
        self.wake.set()
        if self.source and not self.source.cleaned.is_set():
            asyncio.create_task(asyncio.to_thread(self.source.cleanup))

    async def resolve_or_interrupt(self,track,signal):
        resolving=asyncio.create_task(asyncio.wait_for(self.resolver.stream(track),timeout=90))
        interrupted=asyncio.create_task(signal.wait())
        try:
            finished,_=await asyncio.wait((resolving,interrupted),return_when=asyncio.FIRST_COMPLETED)
            if interrupted in finished:
                resolving.cancel()
                with contextlib.suppress(asyncio.CancelledError):await resolving
                return None
            return resolving.result()
        except asyncio.CancelledError:
            resolving.cancel()
            with contextlib.suppress(asyncio.CancelledError):await resolving
            raise
        finally:
            interrupted.cancel()

    async def open_or_interrupt(self,stream,signal):
        opening=asyncio.create_task(asyncio.to_thread(AudioReader,stream.url,stream.headers))
        interrupted=asyncio.create_task(signal.wait())
        def close_late(result):
            if result.cancelled():return
            try:late=result.result()
            except Exception:return
            asyncio.create_task(asyncio.to_thread(late.close))
        try:
            finished,_=await asyncio.wait((opening,interrupted),return_when=asyncio.FIRST_COMPLETED)
            if interrupted in finished:
                opening.add_done_callback(close_late)
                return None
            return opening.result()
        except asyncio.CancelledError:
            opening.add_done_callback(close_late)
            raise
        finally:
            interrupted.cancel()

    async def control(self, action, value=None):
        async with self.lock:
            voice = self.guild.voice_client
            if self.closed or not voice or voice is not self.voice or not voice.is_connected():
                raise ValueError('No active player. Start one with /play.')
            if action == 'queue':
                return '\n'.join(f'{n}. {discord.utils.escape_markdown(t.title)[:120]}'
                    for n, t in enumerate(list(self.queue)[:15], 1)) or 'Queue empty.'
            if action == 'nowplaying':
                return self.status + (': ' + self.current.title[:200] if self.current else '')
            if action == 'pause':
                if not voice.is_playing():
                    raise ValueError('No playing track to pause.')
                voice.pause()
                self.status = 'Paused'
            elif action == 'resume':
                if not voice.is_paused():
                    raise ValueError('The player is not paused.')
                voice.resume()
                self.status = 'Sending audio — listener confirmation required'
            elif action == 'skip':
                if not self.current:
                    raise ValueError('No current track to skip.')
                self.restart = None
                self._interrupt()
            elif action == 'previous':
                if not self.history:
                    raise ValueError('No previous track in this session.')
                if self.restart:
                    raise ValueError('A playback change is already pending. Try Previous when that track starts.')
                if self.current and len(self.queue)>=MAX_QUEUE:
                    raise ValueError('The queue is full. Remove a queued track before returning to Previous.')
                if self.current:
                    self.queue.appendleft(self.current)
                self.restart = (self.history.pop(),0)
                self._interrupt()
            elif action == 'shuffle':
                if len(self.queue) < 2:
                    raise ValueError('At least two queued tracks are required to shuffle.')
                items = list(self.queue)
                random.shuffle(items)
                self.queue = deque(items)
            elif action == 'loop':
                self.loop = value or {'off': 'track', 'track': 'queue', 'queue': 'off'}[self.loop]
            elif action == 'volume':
                self.volume = max(0, min(1, value / 100))
                # Reopen at the last encoded position to apply FFmpeg's volume filter.
                if self.current and (not self.restart or self.restart[0] is self.current) and not (self.source and self.source.cleaned.is_set()):
                    offset = self.source.offset + self.source.frames * .02 if self.source else 0
                    if self.restart and self.restart[0] is self.current:offset=self.restart[1]
                    self.restart = (self.current,offset)
                    self._interrupt()
            elif action == 'clear':
                self.queue.clear()
            elif action == 'remove':
                if value > len(self.queue):
                    raise ValueError('That queue position does not exist.')
                del self.queue[value - 1]
            elif action == 'move':
                start, end = value
                if max(start, end) > len(self.queue):
                    raise ValueError('That queue position does not exist.')
                items = list(self.queue)
                items.insert(end - 1, items.pop(start - 1))
                self.queue = deque(items)
            elif action in ('seek', 'replay'):
                if not self.current:
                    raise ValueError('No current track to seek.')
                if self.restart and self.restart[0] is not self.current:
                    raise ValueError('A different track is starting. Seek after that track starts.')
                if self.source and self.source.cleaned.is_set() and not self.restart:
                    raise ValueError('The current track has finished. Start another track before seeking.')
                position = value if action == 'seek' else 0
                if self.current.duration and position >= self.current.duration:
                    raise ValueError('Seek position must be before the end of the track.')
                self.restart = (self.current,position)
                self._interrupt()
            elif action == 'stop':
                self.closed = True
                self.queue.clear()
                self.restart = None
                self.loop = 'off'
                self._interrupt()
                await voice.disconnect(force=True)
                self.current = None
                self.status = 'Stopped'
            else:
                raise ValueError('Unknown playback action.')
        await self.publish()
        return f'{action.capitalize()} applied.'

    async def run(self):
        try:
            while not self.closed:
                async with self.lock:
                    if self.restart or self.queue:
                        if self.restart:
                            track,offset=self.restart
                            self.restart=None
                        else:
                            track,offset=self.queue.popleft(),0
                        self.current = track
                        revision = self.revision
                        self.done = asyncio.Event()
                        done = self.done
                        self.status = 'Resolving audio'
                        self.source = None
                    else:
                        self.current = None
                        self.status = 'Queue finished — disconnecting after 180 seconds idle'
                        self.wake.clear()
                        track = None
                await self.publish()
                if track is None:
                    try:
                        await asyncio.wait_for(self.wake.wait(), timeout=180)
                    except TimeoutError:
                        # A new request may arrive at the timeout boundary.
                        async with self.lock:
                            if self.queue or self.restart:
                                continue
                            self.closed = True
                            voice = self.voice
                            if voice:
                                await voice.disconnect(force=True)
                            self.status = 'Disconnected after idle timeout'
                        await self.publish()
                    continue
                source = None
                owned = False
                audio_thread = None
                stage = 'resolve'
                try:
                    stream = await self.resolve_or_interrupt(track,done)
                    if stream is None:continue
                    stage = 'http_open'
                    reader=await self.open_or_interrupt(stream,done)
                    if reader is None:continue
                    stage = 'ffmpeg_start'
                    try:
                        source = StreamSource(reader, self.volume, offset)
                    except BaseException:
                        reader.close()
                        raise
                    loop = asyncio.get_running_loop()
                    errors = []
                    def finished(error, signal=done, errors=errors, loop=loop):
                        if error:
                            errors.append(type(error).__name__)
                        with contextlib.suppress(RuntimeError):loop.call_soon_threadsafe(signal.set)
                    async with self.lock:
                        voice = self.guild.voice_client
                        if self.closed or revision != self.revision or not voice or voice is not self.voice or not voice.is_connected():
                            continue
                        self.source = source
                        self.packets_at_start = getattr(voice, 'audio_packets', 0)
                        stage = 'voice_send'
                        voice.play(source, after=finished)
                        audio_thread=getattr(voice,'_player',None)
                        owned = True
                        self.status = 'Starting audio — listener confirmation required'
                    started=time.monotonic()
                    while not done.is_set():
                        try:
                            await asyncio.wait_for(done.wait(), timeout=5)
                        except TimeoutError:
                            packets = getattr(voice, 'audio_packets', 0) - self.packets_at_start
                            if packets and not voice.is_paused():
                                self.status = f'Sending audio ({packets} packets) — listener confirmation required'
                            if not voice.is_connected():
                                raise ValueError('Voice disconnected.')
                            if voice.is_paused():started=time.monotonic()
                            if source.frames==0 and time.monotonic()-started>60:
                                raise ValueError('The audio pipeline did not produce a frame within sixty seconds.')
                            await self.publish()
                    if revision == self.revision and not self.closed:
                        if errors or source.frames == 0 or getattr(voice,'audio_packets',0)==self.packets_at_start:
                            raise ValueError('No audio frames or an audio transport error.')
                        async with self.lock:
                            if revision==self.revision and not self.closed:
                                self.history.append(track)
                                if self.loop == 'track':
                                    self.queue.appendleft(track)
                                elif self.loop == 'queue':
                                    self.queue.append(track)
                    log.info('Audio attempt guild=%s frames=%s packets=%s audible=unverified',
                        self.guild.id, source.frames,
                        getattr(voice, 'audio_packets', 0) - self.packets_at_start)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Never log exception text, signed stream URLs or credentials.
                    log.warning('Audio attempt failed guild=%s stage=%s error=%s', self.guild.id, stage, type(exc).__name__)
                    if self.closed or revision!=self.revision:
                        continue
                    self.status = 'Track failed: unavailable source, host network or audio transport. Trying next track.'
                    await self.publish()
                    if self.channel:
                        with contextlib.suppress(discord.HTTPException):
                            await self.channel.send('A queued track could not be played. Trying the next track.',
                                allowed_mentions=discord.AllowedMentions.none(), silent=True)
                finally:
                    if source:
                        if owned and voice and voice.source is source:
                            voice.stop()
                        await asyncio.to_thread(source.cleanup)
                        if audio_thread and audio_thread.is_alive():
                            await asyncio.to_thread(audio_thread.join,5)
                            if audio_thread.is_alive():
                                # Never start another sender while the previous
                                # thread could still be sending old packets.
                                self.closed=True
                                if voice:await voice.disconnect(force=True)
                                self.status='Audio worker did not stop safely; start a fresh session with /play.'
        finally:
            self.current = None

    async def close(self):
        async with self.lock:
            self.closed = True
            self.queue.clear()
            self.restart = None
            self._interrupt()
            voice = self.voice
            if voice:
                await voice.disconnect(force=True)
        if self.task and self.task is not asyncio.current_task():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
