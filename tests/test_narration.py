import threading
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from test_azure_server import server
narration = server.narration


class NarrationTests(unittest.TestCase):
    def test_long_text_preserved_and_split_at_sentences(self):
        text = ('她说：“我是千金！”他问：真的假的？\n' * 600) + '没有标点的长句' * 300
        chunks = narration.split_text(text)
        self.assertEqual(''.join(chunks), text)
        self.assertTrue(all(0 < len(chunk) <= 800 for chunk in chunks))
        self.assertTrue(chunks[0].endswith(('！', '？', '！』', '\n')))

    def test_resume_reuses_completed_segments(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(narration, 'OUTPUT', Path(directory)), patch.object(narration.time, 'sleep'), patch.object(narration.subprocess, 'run') as command:
            text = ('甲' * 700 + '。') + ('乙' * 700 + '。')
            job_id = 'a' * 64
            narration.JOBS[job_id] = {}
            calls = []
            def synthesize(chunk, key, region):
                calls.append(chunk)
                if chunk.startswith('乙') and sum(c.startswith('乙') for c in calls) == 1:
                    raise ValueError('模拟第二段失败')
                return b'cached-audio', False
            settings = {'key': 'dummy-key', 'region': 'eastus'}
            narration.run(job_id, text, settings, synthesize, 'ffmpeg')
            self.assertEqual(narration.status(job_id)['state'], 'error')
            def output(args, **kwargs):
                Path(args[-1]).touch()
            command.side_effect = output
            narration.run(job_id, text, settings, synthesize, 'ffmpeg')
            self.assertEqual(narration.status(job_id)['state'], 'done')
            self.assertEqual(len(calls), 3)
            self.assertTrue(calls[-1].startswith('乙'))

    def test_parallel_segments_merge_in_original_order(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(narration, 'OUTPUT', Path(directory)), patch.object(narration, 'wait_request_slot'), patch.object(narration.subprocess, 'run') as command:
            text = ('甲' * 700 + '。') + ('乙' * 700 + '。')
            job_id = 'c' * 64
            narration.JOBS[job_id] = {}
            second_started = threading.Event()
            completion_order = []
            def synthesize(chunk, *args):
                if chunk.startswith('甲'):
                    self.assertTrue(second_started.wait(2), '第二段应在第一段返回前开始')
                else:
                    completion_order.append('乙')
                    second_started.set()
                    return b'second', False
                completion_order.append('甲')
                return b'first', False
            def output(args, **kwargs):
                folder = Path(directory) / job_id
                self.assertTrue((folder / '0000.mp3').is_file())
                self.assertTrue((folder / '0001.mp3').is_file())
                Path(args[-1]).touch()
            command.side_effect = output
            narration.run(job_id, text, {'key': 'dummy-key', 'region': 'eastus'}, synthesize, 'ffmpeg')
            self.assertEqual(narration.status(job_id)['state'], 'done')
            self.assertEqual(narration.status(job_id)['completed'], 2)
            self.assertEqual(completion_order, ['乙', '甲'])
            folder = Path(directory) / job_id
            self.assertEqual((folder / 'segments.txt').read_text(), "file '0000.wav'\nfile '0001.wav'\n")

    def test_request_spacing_is_shared(self):
        with patch.object(narration, 'LAST_REQUEST', None), patch.object(narration.time, 'monotonic', side_effect=[10, 11, 13.2]), patch.object(narration.time, 'sleep') as sleep:
            narration.wait_request_slot()
            narration.wait_request_slot()
            sleep.assert_called_once()
            self.assertAlmostEqual(sleep.call_args.args[0], 2.2)

    def test_history_survives_without_memory_and_reveal_is_scoped(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(narration, 'OUTPUT', Path(directory)), patch.object(narration.subprocess, 'run') as command:
            folder = Path(directory) / ('d' * 64)
            folder.mkdir()
            (folder / '文案.txt').write_text('历史文案')
            target = folder / '朗读与背景音乐.mp3'
            target.write_bytes(b'audio')
            incomplete = Path(directory) / ('e' * 64)
            incomplete.mkdir()
            entries = narration.history()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]['preview'], '历史文案')
            narration.reveal(entries[0]['id'])
            self.assertEqual(command.call_args.args[0], ['/usr/bin/open', '-R', str(target)])
            for invalid in ['../../private', 'e' * 64, None]:
                with self.assertRaises(ValueError):
                    narration.reveal(invalid)
            command.assert_called_once()

    def test_voice_only_without_music_and_separate_cache(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(narration, 'OUTPUT', Path(directory)), patch.object(narration, 'MUSIC', Path(directory) / 'missing.mp3'), patch.object(narration, 'wait_request_slot'):
            tool = narration.ffmpeg()
            source = Path(directory) / 'voice.mp3'
            subprocess.run([tool, '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3', '-y', str(source)], check=True)
            settings = {'key': 'dummy-key', 'region': 'eastus'}
            with patch.object(narration.threading, 'Thread'):
                job_id = narration.start('纯朗读测试。', lambda: settings, None, False)
                with self.assertRaises(ValueError):
                    narration.start('纯朗读测试。', lambda: settings, None, True)
                narration.MUSIC.write_bytes(source.read_bytes())
                music_id = narration.start('纯朗读测试。', lambda: settings, None, True)
                self.assertNotEqual(job_id, music_id)
                narration.MUSIC.unlink()
            narration.run(job_id, '纯朗读测试。', settings, lambda *args: (source.read_bytes(), True), tool, False)
            self.assertEqual(narration.status(job_id)['state'], 'done')
            target = narration.audio_path(job_id)
            self.assertEqual(target.name, '朗读.mp3')
            probe = subprocess.check_output(['/usr/local/bin/ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(target)])
            self.assertAlmostEqual(float(probe), 2, delta=0.15)
            self.assertFalse(narration.history()[0]['with_music'])
            with patch.object(narration.threading, 'Thread') as thread:
                self.assertEqual(narration.start('纯朗读测试。', lambda: settings, None, False), job_id)
                thread.assert_not_called()
            with patch.object(narration.subprocess, 'run') as command:
                narration.reveal(job_id)
                self.assertEqual(command.call_args.args[0][-1], str(target))

    def test_real_mix_loops_music_and_ends_with_voice(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(narration, 'OUTPUT', Path(directory)), patch.object(narration, 'MUSIC', Path(directory) / 'music.mp3'), patch.object(narration.time, 'sleep'):
            tool = narration.ffmpeg()
            source = Path(directory) / 'voice.mp3'
            subprocess.run([tool, '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3', '-y', str(source)], check=True)
            subprocess.run([tool, '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=220:duration=0.5', '-y', str(narration.MUSIC)], check=True)
            job_id = 'b' * 64
            narration.JOBS[job_id] = {}
            narration.run(job_id, '测试。' * 600, {'key': 'dummy-key', 'region': 'eastus'}, lambda *args: (source.read_bytes(), True), tool)
            self.assertEqual(narration.status(job_id)['state'], 'done')
            target = Path(directory) / job_id / '朗读与背景音乐.mp3'
            probe = subprocess.check_output(['/usr/local/bin/ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(target)])
            self.assertAlmostEqual(float(probe), 6, delta=0.15)
            subprocess.run([tool, '-v', 'error', '-i', str(target), '-f', 'null', '-'], check=True)
            self.assertFalse(list(target.parent.glob('*.wav')))


if __name__ == '__main__':
    unittest.main()
