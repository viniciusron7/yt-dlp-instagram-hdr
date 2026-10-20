import tempfile
import unittest
from pathlib import Path

from yt_dlp.globals import plugin_dirs
from yt_dlp.utils import PostProcessingError


plugin_dirs.value = [str(Path(__file__).resolve().parents[2])]
from yt_dlp_plugins.postprocessor.instagram_hdr import InstagramHDRHEVCPP


class HEVCOutputTests(unittest.TestCase):
    def test_non_hevc_hdr_output_never_replaces_original(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, 'video.mp4')
            source.write_bytes(b'original')
            converter = InstagramHDRHEVCPP()
            converter._available_encoders = set()
            converter.to_screen = lambda *_args: None
            converter._verify = lambda _path: 'vp9'
            converter._encode = lambda _src, dest, *_args: Path(dest).write_bytes(b'wrong codec')
            info = {
                '_instagram_hdr': True, '_instagram_hdr_hevc': True,
                'dynamic_range': 'HLG', 'filepath': str(source), 'ext': 'mp4',
            }

            with self.assertRaisesRegex(PostProcessingError, 'expected hevc, got vp9'):
                converter.run(info)

            self.assertEqual(source.read_bytes(), b'original')
            self.assertEqual(list(Path(directory).iterdir()), [source])
            self.assertEqual(info['filepath'], str(source))

    def test_non_hevc_hdr_output_tries_the_next_encoder(self):
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory, 'video.temp.mp4'))
            converter = InstagramHDRHEVCPP()
            converter._available_encoders = set()
            converter.to_screen = lambda *_args: None
            used = []

            def encode(_src, dest, encoder, *_args):
                used.append(encoder)
                Path(dest).write_text('vp9' if encoder == 'hevc_videotoolbox' else 'hevc')

            converter._encode = encode
            converter._verify = lambda path: Path(path).read_text()
            converter._convert('input.mp4', output)

            self.assertEqual(used, ['hevc_videotoolbox', 'libx265'])
            self.assertEqual(Path(output).read_text(), 'hevc')


if __name__ == '__main__':
    unittest.main()
