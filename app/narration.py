"""Local long-form narration jobs, using cached Azure chunks and existing ffmpeg."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'output' / 'narrations'
MUSIC = ROOT / 'assets' / 'ambience' / 'background.mp3'
MAX_TEXT = 100000
JOBS = {}
LOCK = threading.Lock()
WORKER = threading.Lock()
REQUEST_LOCK = threading.Lock()
LAST_REQUEST = None
MAX_CONCURRENT = 4


def wait_request_slot():
    global LAST_REQUEST
    with REQUEST_LOCK:
        if LAST_REQUEST is not None:
            time.sleep(max(0, 3.2 - (time.monotonic() - LAST_REQUEST)))
        LAST_REQUEST = time.monotonic()


def generate_segment(index, chunk, folder, settings, synthesize, stopped):
    path = folder / f'{index:04d}.mp3'
    if path.is_file():
        return path
    for attempt in range(3):
        if stopped.is_set():
            return None
        wait_request_slot()
        if stopped.is_set():
            return None
        try:
            audio, _ = synthesize(chunk, settings['key'], settings['region'])
            temporary = path.with_suffix('.tmp')
            temporary.write_bytes(audio)
            temporary.replace(path)
            return path
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                stopped.set()
                raise
            time.sleep(10 * (attempt + 1))
        except Exception:
            stopped.set()
            raise


def split_text(text, limit=800):
    # Preserve every character; prefer complete sentences, then clauses for long sentences.
    units = re.findall(r'.*?(?:[。！？!?；;\n]+[”’」』]*|$)', text, re.S)
    chunks, current = [], ''
    for unit in filter(None, units):
        while len(unit) > limit:
            cut = max(unit.rfind(mark, 0, limit) for mark in '，,、：: ') + 1
            cut = cut if cut > limit // 2 else limit
            part, unit = unit[:cut], unit[cut:]
            if current:
                chunks.append(current)
                current = ''
            chunks.append(part)
        if len(current) + len(unit) > limit:
            chunks.append(current)
            current = ''
        current += unit
    if current:
        chunks.append(current)
    return chunks


def ffmpeg():
    tool = shutil.which('ffmpeg')
    if not tool and Path('/usr/local/bin/ffmpeg').is_file():
        tool = '/usr/local/bin/ffmpeg'
    if not tool:
        raise ValueError('本机缺少 FFmpeg，无法处理朗读音频。')
    return tool


def update(job_id, **fields):
    with LOCK:
        JOBS[job_id].update(fields)


def status(job_id):
    with LOCK:
        return dict(JOBS[job_id]) if job_id in JOBS else None


def audio_path(job_id):
    folder = OUTPUT / job_id
    voice = folder / '朗读.mp3'
    return voice if voice.is_file() else folder / '朗读与背景音乐.mp3'


def history():
    entries = []
    if not OUTPUT.is_dir():
        return entries
    for folder in OUTPUT.iterdir():
        if not re.fullmatch(r'[a-f0-9]{64}', folder.name) or folder.is_symlink():
            continue
        target = audio_path(folder.name)
        if not target.is_file() or target.is_symlink():
            continue
        try:
            with (folder / '文案.txt').open() as source:
                preview = source.read(100).strip()
        except OSError:
            preview = '晓辰朗读'
        entries.append({'id': folder.name, 'preview': preview,
                        'created': target.stat().st_mtime, 'bytes': target.stat().st_size,
                        'with_music': target.name == '朗读与背景音乐.mp3'})
    return sorted(entries, key=lambda entry: entry['created'], reverse=True)


def reveal(job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r'[a-f0-9]{64}', job_id):
        raise ValueError('生成记录编号无效。')
    folder = OUTPUT / job_id
    target = audio_path(folder.name)
    if folder.is_symlink() or target.is_symlink() or not target.is_file():
        raise ValueError('音频文件不存在。')
    subprocess.run(['/usr/bin/open', '-R', str(target)], check=True, capture_output=True)


def start(text, load_settings, synthesize, with_music=True):
    if type(with_music) is not bool:
        raise ValueError('背景音乐选项应为布尔值。')
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= MAX_TEXT:
        raise ValueError('请输入 1 至 100000 字的文案。')
    if any(ord(c) < 32 and c not in '\n\r\t' for c in text):
        raise ValueError('文案中含有无法朗读的控制字符。')
    text = text.strip()
    tool = ffmpeg()
    if with_music and not MUSIC.is_file():
        raise ValueError('本机背景音乐文件缺失。')
    settings = load_settings()
    if not settings.get('key'):
        raise ValueError('请先在设置中保存 Speech 密钥。')
    identity = json.dumps([text, settings['region'], 'xiaochen-1.5-music-35-v1', hashlib.sha256(MUSIC.read_bytes()).hexdigest()] if with_music else [text, settings['region'], 'xiaochen-1.5-voice-v1'], ensure_ascii=False)
    job_id = hashlib.sha256(identity.encode()).hexdigest()
    folder = OUTPUT / job_id
    target = folder / ('朗读与背景音乐.mp3' if with_music else '朗读.mp3')
    with LOCK:
        if job_id in JOBS and JOBS[job_id]['state'] in ('queued', 'running'):
            return job_id
        JOBS[job_id] = {'with_music': with_music, 'state': 'done' if target.is_file() else 'queued', 'completed': 0,
                        'total': len(split_text(text)), 'message': '已读取本机成品缓存。' if target.is_file() else '等待生成……'}
    if not target.is_file():
        threading.Thread(target=run, args=(job_id, text, settings, synthesize, tool, with_music), daemon=True).start()
    return job_id


def run(job_id, text, settings, synthesize, tool, with_music=True):
    try:
        with WORKER:
            folder = OUTPUT / job_id
            folder.mkdir(parents=True, exist_ok=True)
            (folder / '文案.txt').write_text(text)
            chunks = split_text(text)
            paths = [folder / f'{index:04d}.mp3' for index in range(len(chunks))]
            completed = sum(path.is_file() for path in paths)
            stopped = threading.Event()
            update(job_id, state='running', completed=completed,
                   message=f'正在并发生成朗读，已完成 {completed} / {len(chunks)} 段……')
            with ThreadPoolExecutor(max_workers=settings.get('concurrency', MAX_CONCURRENT)) as executor:
                futures = [executor.submit(generate_segment, index, chunk, folder, settings,
                                           synthesize, stopped)
                           for index, chunk in enumerate(chunks) if not paths[index].is_file()]
                try:
                    for future in as_completed(futures):
                        if future.result() is not None:
                            completed += 1
                            update(job_id, completed=completed,
                                   message=f'正在并发生成朗读，已完成 {completed} / {len(chunks)} 段……')
                except Exception:
                    stopped.set()
                    for future in futures:
                        future.cancel()
                    raise
            update(job_id, message='朗读已生成，正在加速并合成循环背景音乐……' if with_music else '朗读已生成，正在处理 1.5 倍速音频……')
            # Decode each MP3 separately before concatenation to preserve encoder delay metadata.
            # A concat demuxer of WAVs streams long articles without loading all audio into RAM.
            wavs = []
            try:
                for path in paths:
                    wav = path.with_suffix('.wav')
                    subprocess.run([tool, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(path), str(wav)], check=True, capture_output=True)
                    wavs.append(wav)
                playlist = folder / 'segments.txt'
                playlist.write_text(''.join(f"file '{wav.name}'\n" for wav in wavs))
                temporary = folder / 'mix.tmp.mp3'
                voice_filter = '[0:a]atempo=1.5,loudnorm=I=-16:TP=-1.5:LRA=7,aresample=44100,pan=stereo|c0=c0|c1=c0'
                inputs = [tool, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'concat', '-safe', '1', '-i', str(playlist)]
                if with_music:
                    inputs += ['-stream_loop', '-1', '-i', str(MUSIC)]
                    filters = voice_filter + '[v];[1:a]loudnorm=I=-35:TP=-9:LRA=7,aresample=44100[m];[v][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95:level=false[out]'
                else:
                    filters = voice_filter + ',alimiter=limit=0.95:level=false[out]'
                subprocess.run(inputs + ['-filter_complex', filters, '-map', '[out]', '-codec:a', 'libmp3lame', '-b:a', '192k', str(temporary)], check=True, capture_output=True)
                temporary.replace(folder / ('朗读与背景音乐.mp3' if with_music else '朗读.mp3'))
            finally:
                for wav in wavs:
                    wav.unlink(missing_ok=True)
                (folder / 'mix.tmp.mp3').unlink(missing_ok=True)
            update(job_id, state='done', message='完成：晓辰 1.5 倍速，低音量背景音乐循环到朗读结束。' if with_music else '完成：晓辰 1.5 倍速纯朗读，无背景音乐。')
    except HTTPError as exc:
        message = {401: '密钥或区域不匹配，请检查设置。', 403: '微软拒绝访问，请检查资源状态。', 429: '微软请求受限或免费额度不足，请稍后重试。'}.get(exc.code, '微软服务暂时失败，请重试。')
        update(job_id, state='error', message=message + ' 已完成的分段会保留，重试可继续。')
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else '连接或音频合成失败，请检查网络和本机磁盘后重试。'
        update(job_id, state='error', message=message + ' 已完成的分段会保留。')
