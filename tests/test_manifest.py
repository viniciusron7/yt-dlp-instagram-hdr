import html
import importlib.util
import tempfile
import unittest
from pathlib import Path

from yt_dlp.utils import ExtractorError, PostProcessingError


PLUGIN_PATH = Path(__file__).parents[1] / 'yt_dlp_plugins/extractor/instagram_hdr.py'
PLUGIN_SPEC = importlib.util.spec_from_file_location('instagram_hdr_under_test', PLUGIN_PATH)
PLUGIN_MODULE = importlib.util.module_from_spec(PLUGIN_SPEC)
PLUGIN_SPEC.loader.exec_module(PLUGIN_MODULE)
InstagramHDRIE = PLUGIN_MODULE.InstagramHDRIE
InstagramHDRVerifyPP = PLUGIN_MODULE.InstagramHDRVerifyPP
_parse_manifest = PLUGIN_MODULE._parse_manifest


def manifest(codec, tag, video_url='https://cdn.example/video.mp4?a=1&b=2'):
    return f'''<MPD><Period>
    <AdaptationSet contentType="video">
      <Representation id="video" bandwidth="4000000" codecs="{codec}"
        mimeType="video/mp4" FBEncodingTag="{tag}" FBContentLength="1234"
        width="1080" height="1920" frameRate="60/1">
        <BaseURL>{video_url}</BaseURL>
      </Representation>
    </AdaptationSet>
    <AdaptationSet contentType="audio">
      <Representation id="audio" bandwidth="128000" codecs="mp4a.40.42"
        mimeType="audio/mp4" audioSamplingRate="48000">
        <BaseURL>https://cdn.example/audio.mp4?a=1&amp;b=2</BaseURL>
      </Representation>
    </AdaptationSet>
    </Period></MPD>'''


def ladder_manifest():
    return '''<MPD><Period>
    <AdaptationSet contentType="video">
      <Representation id="low" bandwidth="2000000"
        codecs="vp09.02.30.10.01.09.18.09.00" mimeType="video/mp4"
        FBEncodingTag="dash-vp9-hdr_q70" width="720" height="1280" frameRate="30/1">
        <BaseURL>https://cdn.example/video-low.mp4?a=1&amp;b=2</BaseURL>
      </Representation>
      <Representation id="high" bandwidth="5000000"
        codecs="vp09.02.40.10.01.09.18.09.00" mimeType="video/mp4"
        FBEncodingTag="dash-vp9-hdr-hfr_q90" width="1080" height="1920" frameRate="60/1">
        <BaseURL>https://cdn.example/video-high.mp4?a=1&amp;b=2</BaseURL>
      </Representation>
    </AdaptationSet>
    <AdaptationSet contentType="audio">
      <Representation id="audio-low" bandwidth="64000" codecs="mp4a.40.2"
        mimeType="audio/mp4" audioSamplingRate="44100">
        <BaseURL>https://cdn.example/audio-low.mp4</BaseURL>
      </Representation>
      <Representation id="audio-high" bandwidth="128000" codecs="mp4a.40.2"
        mimeType="audio/mp4" audioSamplingRate="48000">
        <BaseURL>https://cdn.example/audio-high.mp4</BaseURL>
      </Representation>
    </AdaptationSet>
    </Period></MPD>'''


class ManifestTests(unittest.TestCase):
    def test_vp9_hlg_with_raw_ampersand(self):
        tracks = _parse_manifest(manifest(
            'vp09.02.40.10.01.09.18.09.00', 'dash-vp9-hdr'))
        video = next(track for track in tracks if track['kind'] == 'video')
        self.assertEqual(video['hdr_kind'], 'HLG')
        self.assertEqual(video['url'], 'https://cdn.example/video.mp4?a=1&b=2')

    def test_av1_hlg(self):
        tracks = _parse_manifest(manifest(
            'av01.0.08M.10.0.110.09.18.09.0', 'dash-av1-hdr'))
        video = next(track for track in tracks if track['kind'] == 'video')
        self.assertEqual(video['hdr_kind'], 'HLG')

    def test_av1_pq(self):
        tracks = _parse_manifest(manifest(
            'av01.0.08M.10.0.110.09.16.09.0', 'dash-av1-pq'))
        video = next(track for track in tracks if track['kind'] == 'video')
        self.assertEqual(video['hdr_kind'], 'HDR10')

    def test_html_escaped_and_malformed_xml_match(self):
        valid = manifest(
            'vp09.02.40.10.01.09.18.09.00',
            'dash-vp9-hdr',
            'https://cdn.example/video.mp4?a=1&amp;b=2',
        )
        valid_tracks = _parse_manifest(valid)
        malformed_tracks = _parse_manifest(html.unescape(valid))
        self.assertEqual(
            [(track['kind'], track['url']) for track in valid_tracks],
            [(track['kind'], track['url']) for track in malformed_tracks],
        )

    def test_sdr_is_not_marked_hdr(self):
        tracks = _parse_manifest(manifest(
            'vp09.00.40.08.01.01.01.01.00', 'dash-vp9-sdr'))
        video = next(track for track in tracks if track['kind'] == 'video')
        self.assertIsNone(video['hdr_kind'])

    def test_reels_and_video_posts_are_captured(self):
        self.assertTrue(InstagramHDRIE.suitable('https://www.instagram.com/reel/ABC_123/'))
        self.assertTrue(InstagramHDRIE.suitable('https://www.instagram.com/user/reels/ABC-123/'))
        self.assertTrue(InstagramHDRIE.suitable('https://www.instagram.com/p/ABC_123/'))
        self.assertTrue(InstagramHDRIE.suitable('https://www.instagram.com/user/p/ABC_123/'))
        self.assertFalse(InstagramHDRIE.suitable('https://www.instagram.com/tv/ABC_123/'))

    def test_authentication_rejections_are_detected(self):
        class HTTPStatusError(Exception):
            def __init__(self, status):
                self.status = status

        self.assertTrue(InstagramHDRIE._is_auth_rejection(
            ExtractorError('forbidden', cause=HTTPStatusError(403))))
        self.assertTrue(InstagramHDRIE._is_auth_rejection(
            ExtractorError('unauthorized', cause=HTTPStatusError(401))))
        self.assertFalse(InstagramHDRIE._is_auth_rejection(
            ExtractorError('not found', cause=HTTPStatusError(404))))

    def test_missing_cookies_delegate_to_native_extractor(self):
        extractor = InstagramHDRIE()
        extractor._fallback_to_native = True
        result = extractor._real_extract('https://www.instagram.com/p/ABC_123/')
        self.assertEqual(result['_type'], 'url')
        self.assertEqual(result['ie_key'], 'Instagram')
        self.assertEqual(result['id'], 'ABC_123')

    def test_missing_cookies_initialize_with_one_warning(self):
        extractor = InstagramHDRIE()
        warnings = []
        extractor._get_cookies = lambda _url: {}
        extractor.report_warning = warnings.append
        extractor._real_initialize()
        self.assertTrue(extractor._fallback_to_native)
        self.assertEqual(len(warnings), 1)
        self.assertIn('Falling back', warnings[0])

    def test_media_result_contains_sorted_video_and_audio_ladders(self):
        extractor = InstagramHDRIE()
        messages = []
        extractor.to_screen = messages.append
        result = extractor._extract_product_media({
            'pk': '123456789',
            'video_duration': 12.5,
            'video_dash_manifest': ladder_manifest(),
        })
        videos = [
            item for item in result['formats']
            if item['format_id'].startswith('hdr-')
        ]
        audios = [
            item for item in result['formats']
            if item['format_id'].startswith('audio-')
        ]
        self.assertEqual([item['height'] for item in videos], [1280, 1920])
        self.assertEqual([item['fps'] for item in videos], [30, 60])
        self.assertEqual([item['abr'] for item in audios], [64, 128])
        self.assertEqual(videos[-1]['dynamic_range'], 'HLG')
        self.assertEqual(result['duration'], 12.5)
        self.assertTrue(result['__instagram_hdr'])
        self.assertIn('1080x1920', messages[0])

    def test_hdr_verifier_accepts_vp9_hlg(self):
        verifier = InstagramHDRVerifyPP()
        messages = []
        verifier.to_screen = messages.append
        verifier.get_metadata_object = lambda *_args, **_kwargs: {'streams': [{
            'codec_type': 'video',
            'codec_name': 'vp9',
            'profile': 'Profile 2',
            'pix_fmt': 'yuv420p10le',
            'color_primaries': 'bt2020',
            'color_transfer': 'arib-std-b67',
            'color_space': 'bt2020nc',
        }]}
        with tempfile.NamedTemporaryFile() as output:
            deleted, info = verifier.run({
                '__instagram_hdr': True,
                'filepath': output.name,
            })
        self.assertEqual(deleted, [])
        self.assertTrue(info['__instagram_hdr'])
        self.assertIn('HDR verified', messages[0])

    def test_hdr_verifier_rejects_sdr(self):
        verifier = InstagramHDRVerifyPP()
        verifier.get_metadata_object = lambda *_args, **_kwargs: {'streams': [{
            'codec_type': 'video',
            'codec_name': 'vp9',
            'profile': 'Profile 0',
            'pix_fmt': 'yuv420p',
            'color_primaries': 'bt709',
            'color_transfer': 'bt709',
            'color_space': 'bt709',
        }]}
        with tempfile.NamedTemporaryFile() as output:
            with self.assertRaises(PostProcessingError):
                verifier.run({
                    '__instagram_hdr': True,
                    'filepath': output.name,
                })


if __name__ == '__main__':
    unittest.main()
