import tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import app

class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.media = self.root / 'fala.mp4'
        self.media.write_bytes(b'original')
    def tearDown(self): self.tmp.cleanup()
    def run_local(self, answer, **opts):
        with patch.object(app, 'whisper_srt', return_value=answer), patch.object(app, 'trash_media') as trash:
            app.batch_job('test', [str(self.media)], {'mode':'text', 'srt':True, **opts})
            return app.get_job('test'), trash
    def test_delete_only_after_outputs(self):
        result, trash = self.run_local(('1\n00:00:00,000 --> 00:00:01,000\nOla\n', 'Ola'), remove_local=True)
        self.assertEqual(result['items'][0]['status'], 'done')
        self.assertTrue(Path(result['items'][0]['txt_path']).read_text().strip())
        trash.assert_called_once_with(self.media)
    def test_empty_text_keeps_original(self):
        result, trash = self.run_local(('some srt', ''), remove_local=True)
        trash.assert_not_called()
        self.assertTrue(self.media.exists())
        self.assertEqual(result['items'][0]['status'], 'error')
    def test_default_preserves_local(self):
        result, trash = self.run_local(('some srt', 'Ola'))
        trash.assert_not_called()
        self.assertEqual(result['items'][0]['status'], 'done')
    def test_failure_continues_and_no_stale_success(self):
        old = self.media.with_suffix('.txt'); old.write_text('old')
        with patch.object(app, 'whisper_srt', side_effect=[RuntimeError('failed'), ('srt','ok')]), patch.object(app,'trash_media') as trash:
            second = self.root / 'second.mp4'; second.write_bytes(b'2')
            app.batch_job('test', [str(self.media),str(second)], {'mode':'text','remove_local':True})
            result = app.get_job('test')
            self.assertEqual([r['status'] for r in result['items']], ['error','done'])
            trash.assert_called_once_with(second)
            self.assertEqual(old.read_text(), 'old')

class OutputSafetyTests(unittest.TestCase):
    def test_dotted_name_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            media=Path(d)/'meeting.2026.mp4'
            old=media.with_suffix('.txt'); old.write_text('original text')
            txt,_=app.batch_sidecars(media,'new text','',False)
            self.assertEqual(old.read_text(),'original text')
            self.assertNotEqual(str(old),txt)
    def test_write_failure_does_not_remove(self):
        with tempfile.TemporaryDirectory() as d:
            media=Path(d)/'video.mp4';media.write_bytes(b'original')
            with patch.object(app,'whisper_srt',return_value=('srt','text')), patch.object(app,'batch_sidecars',side_effect=OSError('disk full')), patch.object(app,'trash_media') as trash:
                app.batch_job('full',[str(media)],{'mode':'text','remove_local':True})
                trash.assert_not_called()
                self.assertTrue(media.exists())
                self.assertEqual(app.get_job('full')['items'][0]['status'],'error')

if __name__ == '__main__': unittest.main()
