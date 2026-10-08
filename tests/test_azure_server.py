import importlib.util
import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import threading
from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

spec = importlib.util.spec_from_file_location('azure_server', Path(__file__).resolve().parents[1] / 'app/azure_server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class AzureTests(unittest.TestCase):
    def test_request_escapes_text_and_reuses_audio_without_network(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'OUTPUT', Path(directory)), patch.object(server, 'urlopen') as call:
            response = MagicMock()
            response.read.return_value = b'fake-mp3'
            call.return_value.__enter__.return_value = response
            audio, cached = server.synthesize('你好 <voice> & 世界', 'dummy-key', 'eastus')
            self.assertFalse(cached)
            request = call.call_args.args[0]
            self.assertIn('你好 &lt;voice&gt; &amp; 世界', request.data.decode())
            self.assertIn('zh-CN-XiaochenNeural', request.data.decode())
            self.assertEqual(request.full_url, 'https://eastus.tts.speech.microsoft.com/cognitiveservices/v1')
            self.assertEqual(request.get_header('Ocp-apim-subscription-key'), 'dummy-key')
            self.assertEqual(server.synthesize('你好 <voice> & 世界', 'another-key', 'eastus'), (audio, True))
            self.assertEqual(call.call_count, 1)
            self.assertNotIn(b'dummy-key', next(Path(directory).iterdir()).read_bytes())

    def test_network_requests_can_overlap(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'OUTPUT', Path(directory)), patch.object(server, 'urlopen') as call:
            rendezvous = threading.Barrier(2)
            def response(*args, **kwargs):
                rendezvous.wait(timeout=2)
                result = MagicMock()
                result.__enter__.return_value.read.return_value = b'fake-mp3'
                return result
            call.side_effect = response
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(server.synthesize, text, 'dummy-key', 'eastus')
                           for text in ['第一段', '第二段']]
                for future in futures:
                    self.assertEqual(future.result(), (b'fake-mp3', False))
            self.assertEqual(call.call_count, 2)

    def test_settings_validate_concurrency_and_hide_saved_key(self):
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        port = httpd.server_port
        with patch.object(server, 'PORT', port), patch.object(server.azure_credentials, 'exists', return_value=True), patch.object(server.azure_credentials, 'load', return_value={'key': 'dummy-key-123456789', 'region': 'eastus'}), patch.object(server.azure_credentials, 'save') as save:
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                url = f'http://127.0.0.1:{port}/api/settings'
                with urlopen(url) as response:
                    settings = json.load(response)
                self.assertEqual(settings['concurrency'], 4)
                self.assertNotIn('key', settings)
                request = Request(url, data=json.dumps({'key': '', 'region': 'eastus', 'concurrency': 2}).encode(), headers={'Content-Type': 'application/json'})
                with urlopen(request) as response:
                    self.assertEqual(response.status, 200)
                self.assertEqual(save.call_args.args[0]['concurrency'], 2)
                self.assertEqual(save.call_args.args[0]['key'], 'dummy-key-123456789')
                for value in [0, 9, True, 1.5, '4']:
                    request = Request(url, data=json.dumps({'concurrency': value}).encode())
                    with self.assertRaises(HTTPError) as failure:
                        urlopen(request)
                    self.assertEqual(failure.exception.code, 400)
                save.assert_called_once()
            finally:
                httpd.shutdown()
                thread.join()
                httpd.server_close()

    def test_empty_response_is_not_cached(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'OUTPUT', Path(directory)), patch.object(server, 'urlopen') as call:
            call.return_value.__enter__.return_value.read.return_value = b''
            with self.assertRaises(ValueError):
                server.synthesize('你好', 'dummy-key', 'eastus')
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
