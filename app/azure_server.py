"""Minimal local Azure Xiaochen workbench; standard library only."""
import hashlib
import json
import re
import threading
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import azure_credentials
import narration
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'output' / 'azure-xiaochen'
VOICE = 'zh-CN-XiaochenNeural'
LOCK = threading.Lock()
PORT = 18762


def synthesize(text, key, region):
    identity = json.dumps([VOICE, region, text, 'audio-24khz-48kbitrate-mono-mp3'], ensure_ascii=False)
    name = hashlib.sha256(identity.encode()).hexdigest() + '.mp3'
    target = OUTPUT / name
    with LOCK:
        if target.is_file():
            return target.read_bytes(), True
    ssml = f'<speak version="1.0" xml:lang="zh-CN"><voice name="{VOICE}">{escape(text)}</voice></speak>'
    request = Request(f'https://{region}.tts.speech.microsoft.com/cognitiveservices/v1',
                      data=ssml.encode(), headers={
                          'Ocp-Apim-Subscription-Key': key,
                          'Content-Type': 'application/ssml+xml',
                          'X-Microsoft-OutputFormat': 'audio-24khz-48kbitrate-mono-mp3',
                          'User-Agent': 'stories1001-xiaochen'})
    with urlopen(request, timeout=90) as response:
        audio = response.read()
    if not audio:
        raise ValueError('微软返回了空音频，请稍后再试。')
    with LOCK:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        target.write_bytes(audio)
    return audio, False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Do not log requests or credentials.

    def send(self, status, body, content_type='application/json; charset=utf-8', headers=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def error(self, status, message):
        self.send(status, json.dumps({'error': message}, ensure_ascii=False).encode())

    def allowed(self):
        host = self.headers.get('Host', '')
        return host in {f'127.0.0.1:{PORT}', f'localhost:{PORT}'} and self.headers.get('Origin', f'http://{host}') == f'http://{host}'

    def do_GET(self):
        if not self.allowed():
            return self.error(403, '请从本机入口打开页面。')
        match = re.fullmatch(r'/api/narrations/([a-f0-9]{64})(/audio)?', self.path)
        if match:
            job_id, audio = match.groups()
            target = narration.audio_path(job_id)
            job = narration.status(job_id)
            if job is None and target.is_file():
                job = {'state': 'done', 'message': '已读取本机成品缓存。', 'with_music': target.name == '朗读与背景音乐.mp3'}
            if job is None:
                return self.error(404, '任务不存在，请重新点击生成。')
            if not audio:
                return self.send(200, json.dumps(job, ensure_ascii=False).encode())
            if job['state'] != 'done' or not target.is_file():
                return self.error(409, '音频尚未生成完成。')
            self.send_response(200)
            self.send_header('Content-Type', 'audio/mpeg')
            self.send_header('Content-Length', str(target.stat().st_size))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try:
                with target.open('rb') as stream:
                    while block := stream.read(65536):
                        self.wfile.write(block)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if self.path == '/api/history':
            return self.send(200, json.dumps(narration.history(), ensure_ascii=False).encode())
        if self.path == '/api/settings':
            try:
                saved = azure_credentials.exists()
                settings = azure_credentials.load() if saved else {'region': 'eastus'}
                return self.send(200, json.dumps({'configured': saved, 'region': settings['region'], 'concurrency': settings.get('concurrency', narration.MAX_CONCURRENT)}).encode())
            except OSError as exc:
                return self.error(503, str(exc))
        if self.path == '/api/status':
            return self.send(200, b'{"app":"stories1001-azure-xiaochen"}')
        if self.path in ('/', '/index.html'):
            return self.send(200, (ROOT / 'app/web/azure.html').read_bytes(), 'text/html; charset=utf-8')
        self.error(404, '页面不存在。')

    def do_POST(self):
        if not self.allowed():
            return self.error(403, '仅接受本机页面请求。')
        if self.path not in ('/api/generate', '/api/settings', '/api/narrations', '/api/reveal'):
            return self.error(404, '接口不存在。')
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= (700000 if self.path == '/api/narrations' else 24000):
                raise ValueError('请求过长或为空。')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError('请求格式无效。')
            if self.path == '/api/reveal':
                narration.reveal(data.get('id'))
                return self.send(200, b'{"ok":true}')
            if self.path == '/api/narrations':
                job_id = narration.start(data.get('text', ''), azure_credentials.load, synthesize, data.get('with_music', True))
                return self.send(202, json.dumps({'id': job_id}).encode())
            if self.path == '/api/settings':
                current = azure_credentials.load()
                concurrency = data.get('concurrency', current.get('concurrency', narration.MAX_CONCURRENT))
                if type(concurrency) is not int or not 1 <= concurrency <= 8:
                    raise ValueError('并发数量应为 1 至 8 的整数。')
                key, region = data.get('key', ''), data.get('region', 'eastus')
                if not isinstance(key, str) or not isinstance(region, str):
                    raise ValueError('设置格式无效。')
                key, region = key.strip(), region.strip().lower()
                if not re.fullmatch(r'[a-z0-9]{2,40}', region):
                    raise ValueError('区域应为 eastus 这样的英文代码。')
                if not key:
                    key = current['key']
                if not re.fullmatch(r'[A-Za-z0-9_-]{16,256}', key):
                    raise ValueError('请填写完整的 Azure Speech KEY 1 或 KEY 2。')
                azure_credentials.save({'key': key, 'region': region, 'concurrency': concurrency})
                return self.send(200, b'{"ok":true}')
            text = data.get('text', '')
            if not isinstance(text, str):
                raise ValueError('输入格式无效。')
            text = text.strip()
            if not 1 <= len(text) <= 3000 or any(ord(c) < 32 and c not in '\n\r\t' for c in text):
                raise ValueError('请输入 1 至 3000 字的文字。')
            settings = azure_credentials.load()
            key, region = settings['key'], settings['region']
            if not key:
                raise ValueError('请先点击设置，保存 Speech 密钥。')
            audio, cached = synthesize(text, key, region)
            self.send(200, audio, 'audio/mpeg', {'X-Audio-Cached': str(cached).lower()})
        except (ValueError, UnicodeError) as exc:
            self.error(400, str(exc))
        except HTTPError as exc:
            messages = {401: '密钥或区域不匹配，请检查 KEY 和 Location/Region。',
                        403: '资源拒绝访问，请检查资源状态和网络访问设置。',
                        429: '免费额度用完或请求过于频繁，请稍后再试；无需升级付费层。',
                        400: '微软未接受请求，请检查区域是否支持晓辰音色。'}
            self.error(502, messages.get(exc.code, f'微软服务暂时失败（HTTP {exc.code}），请稍后再试。'))
        except OSError as exc:
            self.error(502, str(exc) if '钥匙串' in str(exc) else '连接或保存音频失败，请检查网络和本机磁盘后重试。')


if __name__ == '__main__':
    print(f'晓辰语音 http://127.0.0.1:{PORT}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
