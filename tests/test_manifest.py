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
InstagramHDRStoryIE = PLUGIN_MODULE.InstagramHDRStoryIE
InstagramHDRVerifyPP = PLUGIN_MODULE.InstagramHDRVerifyPP
_parse_manifest = PLUGIN_MODULE._parse_manifest
_merge_native_audio_formats = PLUGIN_MODULE._merge_native_audio_formats


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

    def test_reels_posts_and_stories_are_captured(self):
        self.assertTrue(InstagramHDRIE.suitable('https://www.instagram.com/reel/ABC_123/'))
        self.assertTrue(InstagramHDRIE.suitable('https://www.instagram.com/user/reels/ABC-123/'))
        self.assertTrue(InstagramHDRIE.suitable('https://www.instagram.com/p/ABC_123/'))
        self.assertTrue(InstagramHDRIE.suitable('https://www.instagram.com/user/p/ABC_123/'))
        self.assertFalse(InstagramHDRIE.suitable('https://www.instagram.com/tv/ABC_123/'))
        self.assertTrue(InstagramHDRStoryIE.suitable(
            'https://www.instagram.com/stories/example/'))
        self.assertTrue(InstagramHDRStoryIE.suitable(
            'https://www.instagram.com/stories/example/1234567890/'))
        self.assertTrue(InstagramHDRStoryIE.suitable(
            'https://www.instagram.com/stories/highlights/1234567890/'))
        self.assertFalse(InstagramHDRStoryIE.suitable(
            'https://www.instagram.com/reel/ABC_123/'))

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

    def test_story_missing_cookies_delegate_to_native_extractor(self):
        extractor = InstagramHDRStoryIE()
        extractor._fallback_to_native = True
        result = extractor._real_extract('https://www.instagram.com/stories/example/')
        self.assertEqual(result['_type'], 'url')
        self.assertEqual(result['ie_key'], 'InstagramStory')
        self.assertEqual(result['id'], 'example')

    def test_story_fetches_media_info_and_returns_hdr_formats(self):
        extractor = InstagramHDRStoryIE()
        extractor.__dict__['_can_impersonate'] = False
        extractor._get_cookies = lambda _url: {}
        requests = []
        extractor._download_json = lambda url, *_args, **_kwargs: (
            requests.append(url) or {'items': [{
                'pk': '123456789',
                'video_duration': 12.5,
                'video_dash_manifest': ladder_manifest(),
            }]}
        )
        extractor.to_screen = lambda _message: None

        result = extractor._extract_product_media({
            'pk': '123456789',
            'media_type': 2,
            'video_versions': [{'url': 'https://cdn.example/fallback.mp4'}],
        })

        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0].endswith('/media/123456789/info/'))
        self.assertTrue(result['__instagram_hdr'])
        self.assertEqual(
            [item['height'] for item in result['formats'] if item.get('vcodec') != 'none'],
            [1280, 1920],
        )

    def test_story_without_hdr_keeps_sdr_formats(self):
        extractor = InstagramHDRStoryIE()
        extractor.__dict__['_can_impersonate'] = False
        extractor._get_cookies = lambda _url: {}
        extractor.to_screen = lambda _message: None
        extractor._download_json = lambda *_args, **_kwargs: {'items': [{
            'pk': '123456789',
            'media_type': 2,
            'video_versions': [{
                'id': 'sdr',
                'url': 'https://cdn.example/sdr.mp4',
                'width': 720,
                'height': 1280,
            }],
        }]}

        result = extractor._extract_product_media({
            'pk': '123456789',
            'media_type': 2,
        })

        self.assertNotIn('__instagram_hdr', result)
        self.assertEqual(result['formats'][0]['format_id'], 'sdr')
        self.assertEqual(result['formats'][0]['height'], 1280)

    def test_post_without_hdr_keeps_native_formats(self):
        extractor = InstagramHDRIE()
        messages = []
        extractor.to_screen = messages.append

        result = extractor._extract_product_media({
            'pk': '123456789',
            'media_type': 2,
            'video_versions': [{
                'id': 'sdr',
                'url': 'https://cdn.example/sdr.mp4',
                'width': 720,
                'height': 1280,
            }],
        })

        self.assertNotIn('__instagram_hdr', result)
        self.assertEqual(result['formats'][0]['format_id'], 'sdr')
        self.assertEqual(result['formats'][0]['height'], 1280)
        self.assertIn('built-in Instagram extractor', messages[0])

    def test_image_story_does_not_request_media_info(self):
        extractor = InstagramHDRStoryIE()
        extractor._download_json = lambda *_args, **_kwargs: self.fail(
            'Image stories must not request video info')

        result = extractor._extract_product_media({
            'pk': '123456789',
            'media_type': 1,
        })

        self.assertEqual(result['formats'], [])

    def test_media_result_contains_only_sorted_hdr_video_ladder(self):
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
        self.assertEqual([item['height'] for item in videos], [1280, 1920])
        self.assertEqual([item['fps'] for item in videos], [30, 60])
        self.assertFalse(any(item.get('vcodec') == 'none' for item in result['formats']))
        self.assertEqual(videos[-1]['dynamic_range'], 'HLG')
        self.assertEqual(result['duration'], 12.5)
        self.assertTrue(result['__instagram_hdr'])
        self.assertIn('1080x1920', messages[0])

    def test_native_audio_replaces_manifest_audio_and_rejects_xhe_aac(self):
        hdr = {
            'id': 'ABC123',
            '__instagram_hdr': True,
            'formats': [{
                'format_id': 'hdr-video',
                'vcodec': 'vp9',
                'acodec': 'none',
            }, {
                'format_id': 'plugin-xhe-audio',
                'vcodec': 'none',
                'acodec': 'mp4a.40.42',
            }],
        }
        native = {
            'id': 'ABC123',
            'formats': [{
                'format_id': 'native-video',
                'vcodec': 'vp9',
                'acodec': 'none',
            }, {
                'format_id': 'native-he-aac',
                'vcodec': 'none',
                'acodec': 'mp4a.40.5',
            }, {
                'format_id': 'native-xhe-aac',
                'vcodec': 'none',
                'acodec': 'mp4a.40.42',
            }],
        }

        self.assertEqual(_merge_native_audio_formats(hdr, native), 1)
        self.assertEqual(
            [item['format_id'] for item in hdr['formats']],
            ['hdr-video', 'native-he-aac'],
        )

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

    def test_hdr_verifier_skips_audio_only_downloads(self):
        verifier = InstagramHDRVerifyPP()
        verifier.get_metadata_object = lambda *_args, **_kwargs: self.fail(
            'Audio-only downloads must not be HDR-verified')

        deleted, info = verifier.run({
            '__instagram_hdr': True,
            'vcodec': 'none',
        })
        self.assertEqual(deleted, [])
        self.assertEqual(info['vcodec'], 'none')


if __name__ == '__main__':
    unittest.main()
