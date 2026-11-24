"""Host-only executable discovery and an actual encode/decode smoke check."""
import os
import shutil
import subprocess


def ffmpeg_path():
    configured = os.getenv('FFMPEG_PATH')
    if configured:
        resolved = shutil.which(configured)
        if not resolved:
            raise RuntimeError('FFMPEG_PATH does not name an executable on the host.')
        return resolved
    found = shutil.which('ffmpeg')
    if found:
        return found
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def smoke_check():
    """Encode and decode a generated tone in memory, without joining Discord."""
    executable = ffmpeg_path()
    encoded = subprocess.run([
        executable, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
        '-i', 'sine=frequency=440:duration=0.2', '-ac', '2', '-ar', '48000',
        '-c:a', 'libopus', '-f', 'ogg', 'pipe:1',
    ], capture_output=True, timeout=20, check=True).stdout
    decoded = subprocess.run([
        executable, '-hide_banner', '-loglevel', 'error', '-i', 'pipe:0',
        '-f', 's16le', '-ac', '2', '-ar', '48000', 'pipe:1',
    ], input=encoded, capture_output=True, timeout=20, check=True).stdout
    if len(decoded) < 19200 or not any(decoded):
        raise RuntimeError('FFmpeg did not produce non-silent decoded audio.')
    import nacl.secret  # noqa: F401
    import davey  # noqa: F401
    import yt_dlp  # noqa: F401
    from importlib.metadata import version
    runtime = shutil.which(os.getenv('DENO_PATH') or 'deno')
    if not runtime:
        raise RuntimeError('Deno is missing from the host environment PATH.')
    subprocess.run([runtime, '--version'], capture_output=True, timeout=10, check=True)
    return {
        'ffmpeg': executable, 'encoded_bytes': len(encoded),
        'decoded_pcm_bytes': len(decoded), 'discord.py': version('discord.py'),
        'yt-dlp': version('yt-dlp'), 'davey': version('davey'),
        'audible_discord_playback': 'UNVERIFIED — requires a listener in Discord',
    }


if __name__ == '__main__':
    import json
    print(json.dumps(smoke_check(), indent=2))
