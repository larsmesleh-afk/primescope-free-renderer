#!/usr/bin/env python3
"""PrimeScope: six Pixabay scenes + public Google Drive narration -> 60s MP4.

Runs on Ubuntu GitHub Actions, with no hosted rendering API.  Six 10-second
Arabic ASS subtitle cues are scene-level, not forced-aligned word timestamps.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / 'out'
LOCAL_TEST = os.environ.get('PRIMESCOPE_LOCAL_TEST') == '1'
TIME = int(os.environ.get('PRIMESCOPE_SCENE_SECONDS', '10')) if LOCAL_TEST else 10
W, H = (240, 426) if LOCAL_TEST else (720, 1280)
FPS = 12 if LOCAL_TEST else 24
MAX_CLIP_BYTES = 250 * 1024 * 1024


def run(cmd):
    print('Running:', ' '.join(str(x) for x in cmd[:6]), '...', flush=True)
    subprocess.run(cmd, check=True, timeout=1500)


def tag_ok(value):
    return isinstance(value, str) and bool(re.fullmatch(r'ps-[a-zA-Z0-9-]{8,90}', value))


def validate_url(url):
    p = urlparse(url)
    if p.scheme != 'https' or not p.hostname:
        raise ValueError('Only HTTPS video URLs are supported')
    domain = p.hostname.lower()
    if domain not in {'cdn.pixabay.com', 'pixabay.com'} and not domain.endswith('.pixabay.com'):
        raise ValueError('Only Pixabay clips accepted; received host ' + domain)


def fetch_video(url, destination):
    validate_url(url)
    total = 0
    with requests.get(url, timeout=(20, 90), stream=True, allow_redirects=True) as r:
        r.raise_for_status()
        validate_url(r.url)
        with open(destination, 'wb') as handle:
            for part in r.iter_content(chunk_size=1024 * 1024):
                if not part:
                    continue
                total += len(part)
                if total > MAX_CLIP_BYTES:
                    raise ValueError('Clip larger than 250 MiB')
                handle.write(part)
    if not total:
        raise ValueError('Empty clip returned by Pixabay')


def get_audio(file_id, destination):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{10,120}', file_id):
        raise ValueError('Invalid Google Drive file ID')
    import gdown
    result = gdown.download(id=file_id, output=str(destination), quiet=True)
    if not result or not destination.exists() or destination.stat().st_size < 100:
        raise ValueError('Cannot download narration: set Google Drive sharing to Anyone with link')
    # Never render Google Drive interstitial HTML as audio.
    with open(destination, 'rb') as handle:
        if handle.read(4) != b'RIFF':
            raise ValueError('Google Drive did not return a WAV file')


def ass_escape(text):
    clean = str(text).strip().replace('\\', '／').replace('{', '(').replace('}', ')')
    clean = clean.replace('\r', ' ').replace('\n', ' ').replace('\u2028', ' ')
    if not clean:
        raise ValueError('Empty Arabic scene subtitle')
    # ASS script direction hint; libass with HarfBuzz does Arabic shaping.
    return '\u202b' + clean + '\u202c'


def timestamp(seconds):
    mins, secs = divmod(seconds, 60)
    return f'0:{mins:02d}:{secs:02d}.00'


def write_ass(subtitles, filename):
    preamble = '''[Script Info]
Title: PrimeScope Media Arabic Captions
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Arabic,Noto Naskh Arabic,48,&H00FFFFFF,&H00FFFFFF,&H80000000,&H90000000,-1,0,0,0,100,100,0,0,1,3,1,2,60,60,125,-1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    with open(filename, 'w', encoding='utf-8') as handle:
        handle.write(preamble)
        for i, sub in enumerate(subtitles):
            handle.write(f'Dialogue: 0,{timestamp(i*TIME)},{timestamp((i+1)*TIME)},Arabic,,0,0,0,,{ass_escape(sub)}\n')


def render(payload):
    if not isinstance(payload, dict) or not tag_ok(payload.get('tag')):
        raise ValueError('Invalid video tag')
    clips = payload.get('clips')
    subtitles = payload.get('subtitles')
    if not isinstance(clips, list) or len(clips) != 6:
        raise ValueError('Require exactly six Pixabay video clips')
    if not isinstance(subtitles, list) or len(subtitles) != 6:
        raise ValueError('Require exactly six Arabic subtitles')
    file_id = payload.get('audio_file_id', '')
    tag = payload['tag']
    OUT.mkdir(exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix='primescope_'))
    try:
        narration = temp / 'narration.wav'
        local_test = os.environ.get('PRIMESCOPE_LOCAL_TEST') == '1'
        if local_test:
            # Only when explicitly running the local smoketest; never in GitHub Actions.
            shutil.copy2(payload['audio_local_path'], narration)
        else:
            get_audio(file_id, narration)
        segments = []
        for i, source in enumerate(clips):
            clip_file = temp / f'clip_{i}.mp4'
            if local_test:
                shutil.copy2(source, clip_file)
            else:
                fetch_video(source, clip_file)
            segment = temp / f'segment_{i}.mp4'
            run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                 '-stream_loop', '-1', '-i', str(clip_file), '-t', str(TIME),
                 '-an', '-vf', f'scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS},format=yuv420p',
                 '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '24',
                 '-pix_fmt', 'yuv420p', '-r', str(FPS), '-movflags', '+faststart', str(segment)])
            segments.append(segment)
        concat = temp / 'concat.txt'
        concat.write_text(''.join(f"file '{s.as_posix()}'\n" for s in segments), encoding='utf-8')
        ass_file = temp / 'arabic.ass'
        write_ass(subtitles, ass_file)
        output = OUT / f'{tag}.mp4'
        run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
             '-f', 'concat', '-safe', '0', '-i', str(concat), '-i', str(narration),
             '-filter_complex', '[0:v]ass=' + str(ass_file) + ':shaping=complex[v];[1:a]apad[a]',
             '-map', '[v]', '-map', '[a]', '-t', str(TIME * 6),
             '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '24',
             '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '160k',
             '-movflags', '+faststart', str(output)])
        if output.stat().st_size < 10000:
            raise ValueError('Render output unexpectedly small')
        (OUT / 'tag.txt').write_text(tag, encoding='ascii')
        print('CREATED', output, f'({output.stat().st_size} bytes)')
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == '__main__':
    data = os.environ.get('PRIMESCOPE_PAYLOAD', '')
    if not data:
        sys.exit('Missing PRIMESCOPE_PAYLOAD')
    render(json.loads(data))
