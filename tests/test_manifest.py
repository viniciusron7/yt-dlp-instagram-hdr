import copy
import html
import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from yt_dlp import YoutubeDL
from yt_dlp.extractor.instagram import InstagramIE
from yt_dlp.globals import plugin_dirs
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessorError
from yt_dlp.utils import ExtractorError, PostProcessingError


# Do not mix this checkout with a wheel installed in the user's plugin directory.
plugin_dirs.value = [str(Path(__file__).resolve().parents[2])]
from yt_dlp_plugins.extractor import instagram_hdr as PLUGIN_MODULE

InstagramHDRIE = PLUGIN_MODULE.InstagramHDRIE
InstagramHDRStoryIE = PLUGIN_MODULE.InstagramHDRStoryIE
InstagramHDRVerifyPP = PLUGIN_MODULE.InstagramHDRVerifyPP
InstagramHDRHEVCPP = PLUGIN_MODULE.InstagramHDRHEVCPP
_parse_manifest = PLUGIN_MODULE._parse_manifest
_merge_native_formats = PLUGIN_MODULE._merge_native_formats


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
    def test_extractor_and_postprocessor_are_loaded_from_checkout(self):
        root = Path(__file__).resolve().parents[1] / 'yt_dlp_plugins'
        self.assertEqual(Path(PLUGIN_MODULE.__file__).resolve(), root / 'extractor/instagram_hdr.py')
        self.assertEqual(Path(inspect.getfile(InstagramHDRVerifyPP)).resolve(),
                         root / 'postprocessor/instagram_hdr.py')

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

    def test_representation_cicp_overrides_adaptation_without_leaking_to_siblings(self):
        mpd = '''<MPD><Period><AdaptationSet contentType="video" codecs="vp09.02.40.10">
          <EssentialProperty schemeIdUri="urn:mpeg:mpegB:cicp:ColourPrimaries" value="9"/>
          <EssentialProperty schemeIdUri="urn:mpeg:mpegB:cicp:TransferCharacteristics" value="16"/>
          <Representation id="first">
            <EssentialProperty schemeIdUri="urn:mpeg:mpegB:cicp:TransferCharacteristics" value="18"/>
            <BaseURL>https://cdn.example/video.mp4</BaseURL>
          </Representation>
          <Representation id="second"><BaseURL>https://cdn.example/other.mp4</BaseURL></Representation>
          <Representation id="sdr">
            <EssentialProperty schemeIdUri="urn:mpeg:mpegB:cicp:ColourPrimaries" value="1"/>
            <EssentialProperty schemeIdUri="urn:mpeg:mpegB:cicp:TransferCharacteristics" value="1"/>
            <BaseURL>https://cdn.example/hdr-url-is-not-metadata.mp4</BaseURL>
          </Representation>
        </AdaptationSet></Period></MPD>'''
        self.assertEqual([track['hdr_kind'] for track in _parse_manifest(mpd)],
                         ['HLG', 'HDR10', None])

    def test_representation_cicp_detects_hdr_without_adaptation_properties(self):
        mpd = '''<MPD><Period><AdaptationSet contentType="video">
          <Representation id="video" codecs="vp09.02.40.10">
            <SupplementalProperty schemeIdUri="urn:mpeg:mpegB:cicp:ColourPrimaries" value="9"/>
            <SupplementalProperty schemeIdUri="urn:mpeg:mpegB:cicp:TransferCharacteristics" value="18"/>
            <BaseURL>https://cdn.example/video.mp4</BaseURL>
          </Representation>
        </AdaptationSet></Period></MPD>'''
        self.assertEqual(_parse_manifest(mpd)[0]['hdr_kind'], 'HLG')

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

    def test_sdr_with_unescaped_ampersands_keeps_formats_and_reaches_native_extractor(self):
        mpd = manifest('vp09.00.40.08.01.01.01.01.00', 'dash-vp9-sdr').replace(
            '<MPD>', '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT1S">')
        product = {
            'pk': '123456789',
            'video_dash_manifest': mpd,
            'video_versions': [{'id': 'progressive', 'url': 'https://cdn.example/progressive.mp4'}],
        }
        with YoutubeDL({'quiet': True}, auto_init=False) as downloader:
            extractor = InstagramHDRIE(downloader)
            ios = extractor._extract_product_media(product)
            self.assertNotIn('__instagram_hdr', ios)
            self.assertEqual(product['video_dash_manifest'], mpd)
            self.assertEqual({fmt['url'] for fmt in ios['formats']}, {
                'https://cdn.example/progressive.mp4',
                'https://cdn.example/video.mp4?a=1&b=2',
                'https://cdn.example/audio.mp4?a=1&b=2',
            })
            native = mock.Mock()
            native.extract.return_value = {
                'id': ios['id'], 'formats': [{'format_id': 'native', 'url': 'https://cdn.example/native.mp4'}],
            }
            extractor._NATIVE_IE = lambda _downloader: native
            with mock.patch.object(InstagramIE, '_real_extract', return_value=ios):
                result = extractor._real_extract('https://www.instagram.com/p/HW80V/')
            native.extract.assert_called_once()
            self.assertEqual([fmt['format_id'] for fmt in result['formats']], ['native'])

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

    def test_native_formats_are_merged_and_xhe_aac_is_rejected_by_default(self):
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

        self.assertEqual(_merge_native_formats(hdr, native), [])
        self.assertEqual(
            [item['format_id'] for item in hdr['formats']],
            ['hdr-video', 'native-video', 'native-he-aac'],
        )

    def test_native_and_plugin_xhe_aac_are_included_when_enabled(self):
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
                'format_id': 'native-xhe-audio',
                'vcodec': 'none',
                'acodec': 'mp4a.40.42',
            }],
        }

        self.assertEqual(_merge_native_formats(hdr, native, include_xhe_aac=True), [])
        self.assertEqual(
            [item['format_id'] for item in hdr['formats']],
            ['hdr-video', 'plugin-xhe-audio', 'native-xhe-audio'],
        )

    def test_duplicate_native_format_ids_are_preserved_with_suffixes(self):
        hdr = {
            'id': 'ABC123',
            '__instagram_hdr': True,
            'formats': [{
                'format_id': 'hdr-video',
                'url': 'https://cdn.example/hdr.mp4',
                'vcodec': 'vp9',
                'acodec': 'none',
            }],
        }
        native = {
            'id': 'ABC123',
            'formats': [{
                'format_id': 'native-video',
                'url': 'https://cdn.example/native-1.mp4',
            }, {
                'format_id': 'native-video',
                'url': 'https://cdn.example/native-2.mp4',
            }, {
                'format_id': 'native-video',
                'url': 'https://cdn.example/native-3.mp4',
            }],
        }

        self.assertEqual(_merge_native_formats(hdr, native), [])
        self.assertEqual(
            [item['format_id'] for item in hdr['formats']],
            ['hdr-video', 'native-video-0', 'native-video-1', 'native-video-2'],
        )

    def test_native_formats_are_paired_by_id_only(self):
        hdr = {'_type': 'playlist', 'entries': [
            {'id': 'A', 'formats': [{'format_id': 'hdr-A', 'vcodec': 'vp9', 'acodec': 'none'}]},
            {'id': 'B', 'formats': [{'format_id': 'hdr-B', 'vcodec': 'vp9', 'acodec': 'none'}]},
        ]}
        native = {'_type': 'playlist', 'entries': [
            {'id': 'B', 'formats': [{'format_id': 'aac-B', 'vcodec': 'none', 'acodec': 'mp4a.40.5'}]},
            {'id': 'C', 'formats': [{'format_id': 'aac-C', 'vcodec': 'none', 'acodec': 'mp4a.40.5'}]},
        ]}

        self.assertEqual(_merge_native_formats(hdr, native), ['A'])
        self.assertEqual(
            [[item['format_id'] for item in entry['formats']] for entry in hdr['entries']],
            [['hdr-A'], ['hdr-B', 'aac-B']],
        )

    def test_sdr_items_use_native_formats_and_keep_ios_xhe_aac_only_when_enabled(self):
        ios = {'id': 'ABC123', 'formats': [
            {'format_id': 'dash-ios-video', 'vcodec': 'vp09.00', 'acodec': 'none'},
            {'format_id': 'dash-ios-xhe', 'vcodec': 'none', 'acodec': 'mp4a.40.42'},
        ]}
        native = {'id': 'ABC123', 'formats': [
            {'format_id': 'dash-web-video', 'vcodec': 'vp09.00', 'acodec': 'none'},
            {'format_id': 'dash-web-aac', 'vcodec': 'none', 'acodec': 'mp4a.40.5'},
        ]}

        for args, expected in (
                ({}, ['dash-web-video', 'dash-web-aac']),
                ({'include_xhe_aac': ['']}, ['dash-ios-xhe', 'dash-web-video', 'dash-web-aac'])):
            extractor = InstagramHDRIE()
            extractor._downloader = SimpleNamespace(params={'extractor_args': {'instagramhdr': args}})
            extractor.to_screen = lambda _message: None
            extractor._NATIVE_IE = lambda _downloader: SimpleNamespace(
                extract=lambda _url: copy.deepcopy(native))
            with mock.patch.object(InstagramIE, '_real_extract', lambda *_args: copy.deepcopy(ios)):
                result = extractor._real_extract('https://www.instagram.com/p/ABC123/')
            self.assertEqual([item['format_id'] for item in result['formats']], expected)

    def test_include_xhe_aac_adds_manifest_audio_formats(self):
        extractor = InstagramHDRIE()
        extractor._downloader = SimpleNamespace(params={
            'extractor_args': {'instagramhdr': {'include_xhe_aac': ['']}},
        })
        extractor.to_screen = lambda _message: None

        result = extractor._extract_product_media({
            'pk': '123456789',
            'video_duration': 12.5,
            'video_dash_manifest': ladder_manifest().replace(
                'codecs="mp4a.40.2"', 'codecs="mp4a.40.42"'),
        })

        self.assertEqual(
            [item['acodec'] for item in result['formats'] if item.get('vcodec') == 'none'],
            ['mp4a.40.42', 'mp4a.40.42'],
        )

    def test_empty_native_result_keeps_ios_formats_and_filters_xhe_aac(self):
        ios = {'id': 'ABC123', 'formats': [
            {'format_id': 'hdr-video', 'vcodec': 'vp9', 'acodec': 'none'},
            {'format_id': 'ios-audio', 'vcodec': 'none', 'acodec': 'mp4a.40.42'},
        ]}
        with YoutubeDL({'quiet': True}, auto_init=False) as downloader:
            extractor = InstagramHDRIE(downloader)
            extractor._NATIVE_IE = lambda _downloader: SimpleNamespace(extract=lambda _url: None)
            with mock.patch.object(InstagramIE, '_real_extract', return_value=ios):
                result = extractor._real_extract('https://www.instagram.com/p/ABC123/')
            self.assertEqual([fmt['format_id'] for fmt in result['formats']], ['hdr-video'])

    def test_disable_uses_native_extractor_for_posts_and_stories(self):
        downloader = SimpleNamespace(params={
            'extractor_args': {'instagramhdr': {'disable': ['']}},
        })
        for extractor in (InstagramHDRIE(), InstagramHDRStoryIE()):
            messages = []
            extractor._downloader = downloader
            extractor.to_screen = messages.append
            extractor._real_initialize()
            self.assertTrue(extractor._fallback_to_native)
            self.assertIn('plugin disabled', messages[0])

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
                'dynamic_range': 'HLG',
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
                    'dynamic_range': 'HLG',
                    'filepath': output.name,
                })

    def test_hdr_verifier_skips_native_sdr_selection(self):
        verifier = InstagramHDRVerifyPP()
        verifier.get_metadata_object = lambda *_args, **_kwargs: self.fail(
            'SDR selections from the combined format list must not be HDR-verified')

        with tempfile.NamedTemporaryFile() as output:
            deleted, _info = verifier.run({
                '__instagram_hdr': True,
                'dynamic_range': 'SDR',
                'filepath': output.name,
            })
        self.assertEqual(deleted, [])

    def test_hevc_converter_writes_mp4_with_fallback_and_skips_converted_files(self):
        converter = InstagramHDRHEVCPP()
        converter.to_screen = converter.write_debug = lambda *_args, **_kwargs: None
        hlg = {
            'codec_type': 'video',
            'pix_fmt': 'yuv420p10le',
            'color_primaries': 'bt2020',
            'color_transfer': 'arib-std-b67',
            'color_space': 'bt2020nc',
        }
        codecs = {}
        converter.get_metadata_object = lambda path, **_kwargs: {'streams': [{
            **hlg, **codecs.get(path, {'codec_name': 'vp9', 'profile': 'Profile 2'}),
        }]}
        calls = []

        def run_ffmpeg(_path, out_path, opts):
            calls.append(opts)
            if 'hevc_videotoolbox' in opts:
                raise FFmpegPostProcessorError("Unknown encoder 'hevc_videotoolbox'")
            Path(out_path).write_bytes(b'hevc')
            codecs[out_path] = {'codec_name': 'hevc', 'profile': 'Main 10'}

        converter.run_ffmpeg = run_ffmpeg
        with tempfile.TemporaryDirectory() as directory:
            webm, mp4 = str(Path(directory, 'video.webm')), str(Path(directory, 'video.mp4'))
            Path(webm).write_bytes(b'vp9')
            info = {
                '__instagram_hdr': True,
                '__instagram_hdr_hevc': True,
                'dynamic_range': 'HLG',
                'filepath': webm,
                'ext': 'webm',
                'vcodec': 'vp09.02.40.10.01.09.18.09.00',
            }

            self.assertEqual(converter.run(info), ([webm], info))
            self.assertEqual((info['filepath'], info['ext'], info['vcodec']), (mp4, 'mp4', 'hevc'))
            self.assertEqual(Path(mp4).read_bytes(), b'hevc')
            self.assertEqual([opts[opts.index('-c:v:0') + 1] for opts in calls],
                             ['hevc_videotoolbox', 'libx265'])
            self.assertIn('hvc1', calls[-1])

            codecs[mp4] = {'codec_name': 'hevc', 'profile': 'Main 10'}
            calls.clear()
            self.assertEqual(converter.run(info), ([], info))
            self.assertEqual(calls, [])

    def test_post_processors_follow_per_item_options(self):
        verifier, converter = InstagramHDRVerifyPP(), InstagramHDRHEVCPP()
        for post_processor in (verifier, converter):
            post_processor.get_metadata_object = lambda *_args, **_kwargs: self.fail(
                'The options stored on the item decide which post-processor probes it')

        with tempfile.NamedTemporaryFile() as output:
            info = {'__instagram_hdr': True, 'dynamic_range': 'HLG', 'filepath': output.name}
            converter.run(info)
            verifier.run({**info, '__instagram_hdr_hevc': True})
            verifier.run({**info, '__instagram_hdr_verify': False})

    def test_hevc_pipeline_preserves_original_and_finishes_before_after_move(self):
        for ext in ('mp4', 'webm'):
            for keep in (False, True):
                with self.subTest(ext=ext, keep=keep), tempfile.TemporaryDirectory() as directory:
                    folder = Path(directory)
                    source = folder / f'video.{ext}'
                    source.write_bytes(b'vp9')
                    finaldir = folder / 'final'
                    with YoutubeDL({'quiet': True, 'keepvideo': keep}, auto_init=False) as downloader:
                        observed = []
                        observer = PostProcessor(downloader)
                        observer.run = lambda info: (observed.append(info['filepath']) or [], info)
                        downloader.add_post_processor(observer, when='after_move')
                        extractor = InstagramHDRIE(downloader)
                        extractor._get_cookies = lambda _url: {'sessionid': object(), 'ds_user_id': object()}
                        extractor._real_initialize()
                        stages = [
                            key for key, processors in downloader._pps.items()
                            if any(isinstance(pp, InstagramHDRHEVCPP) for pp in processors)
                        ]
                        self.assertEqual(stages, ['post_process'])
                        converter = next(pp for pp in downloader._pps['post_process']
                                         if isinstance(pp, InstagramHDRHEVCPP))
                        converter._verify = lambda path: Path(path).read_bytes().decode()
                        converter._encode = lambda _src, dest, *_args: Path(dest).write_bytes(b'hevc')
                        printed = []
                        with mock.patch.object(downloader, '_forceprint', side_effect=lambda key, info: (
                                printed.append(info['filepath']) if key == 'after_move' else None)):
                            result = downloader.post_process(str(source), {
                                '__instagram_hdr': True, '__instagram_hdr_hevc': True,
                                '__finaldir': str(finaldir), 'dynamic_range': 'HLG', 'ext': ext, 'vcodec': 'vp9',
                            })
                        output = finaldir / 'video.mp4'
                        self.assertEqual(printed, [str(output)])
                        self.assertEqual(observed, [str(output)])
                        self.assertEqual((result['ext'], result['vcodec']), ('mp4', 'hevc'))
                        self.assertEqual(output.read_bytes(), b'hevc')
                        original = finaldir / ('video.orig.mp4' if ext == 'mp4' else 'video.webm')
                        self.assertEqual(original.exists(), keep)
                        if keep:
                            self.assertEqual(original.read_bytes(), b'vp9')
                        self.assertFalse(source.exists())

    def test_hevc_does_not_overwrite_protected_files(self):
        for collision in ('output', 'final_output', 'original', 'final_original', 'final_webm'):
            with self.subTest(collision=collision), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                source = folder / ('video.mp4' if collision in ('original', 'final_original') else 'video.webm')
                source.write_bytes(b'vp9')
                finaldir = folder / 'final'
                finaldir.mkdir()
                protected = (finaldir / 'video.mp4' if collision == 'final_output'
                             else folder / ('video.orig.mp4' if collision == 'original' else 'video.mp4'))
                if collision in ('final_original', 'final_webm'):
                    protected = finaldir / ('video.orig.mp4' if collision == 'final_original' else 'video.webm')
                protected.write_bytes(b'previous download')
                with YoutubeDL({'quiet': True, 'overwrites': False, 'keepvideo': True}, auto_init=False) as downloader:
                    converter = InstagramHDRHEVCPP(downloader)
                    converter._verify = lambda _path: 'vp9'
                    converter._encode = mock.Mock()
                    with self.assertRaises(PostProcessingError):
                        converter.run({
                            '__instagram_hdr': True, '__instagram_hdr_hevc': True,
                            '__finaldir': str(finaldir), 'dynamic_range': 'HLG',
                            'filepath': str(source), 'ext': source.suffix[1:],
                        })
                    converter._encode.assert_not_called()
                    self.assertEqual(source.read_bytes(), b'vp9')
                    self.assertEqual(protected.read_bytes(), b'previous download')

    def test_hdr_postprocessors_skip_extracted_audio_with_stale_video_metadata(self):
        for cls in (InstagramHDRVerifyPP, InstagramHDRHEVCPP):
            for ext in ('m4a', 'mp3', 'opus', 'aac', 'wav'):
                with self.subTest(postprocessor=cls.__name__, ext=ext), tempfile.NamedTemporaryFile() as output:
                    postprocessor = cls()
                    postprocessor._verify = mock.Mock(side_effect=AssertionError('Audio must not be HDR-probed'))
                    info = {'__instagram_hdr': True, '__instagram_hdr_hevc': cls is InstagramHDRHEVCPP,
                            'dynamic_range': 'HLG', 'vcodec': 'vp9', 'ext': ext, 'filepath': output.name}
                    self.assertEqual(postprocessor.run(info), ([], info))

    def test_hevc_failure_preserves_files_and_cleans_temporary_output(self):
        for ext in ('mp4', 'webm'):
            for failure in ('encode', 'verify', 'replace'):
                with self.subTest(ext=ext, failure=failure), tempfile.TemporaryDirectory() as directory:
                    source = Path(directory, f'video.{ext}')
                    source.write_bytes(b'vp9')
                    if ext == 'webm':
                        Path(directory, 'video.mp4').write_bytes(b'previous output')
                    originals = {path.name: path.read_bytes() for path in Path(directory).iterdir()}
                    converter = InstagramHDRHEVCPP()
                    converter.to_screen = converter.write_debug = lambda *_args, **_kwargs: None

                    def encode(_src, dest, *_args):
                        Path(dest).write_bytes(b'partial output')
                        if failure == 'encode':
                            raise FFmpegPostProcessorError('encoding failed')

                    def verify(path):
                        if failure == 'verify' and path != str(source):
                            raise PostProcessingError('verification failed')
                        return 'vp9' if path == str(source) else 'hevc'

                    converter._encode, converter._verify = encode, verify
                    info = {'__instagram_hdr': True, '__instagram_hdr_hevc': True,
                            'dynamic_range': 'HLG', 'filepath': str(source), 'ext': ext}
                    if failure == 'replace':
                        with mock.patch('os.replace', side_effect=OSError('replace failed')):
                            with self.assertRaises(OSError):
                                converter.run(info)
                    else:
                        with self.assertRaises(PostProcessingError):
                            converter.run(info)
                    self.assertEqual({path.name: path.read_bytes() for path in Path(directory).iterdir()}, originals)
                    self.assertEqual(info['filepath'], str(source))

    def test_hevc_conversion_is_opt_in_for_posts_and_stories(self):
        for extractor_type in (InstagramHDRIE, InstagramHDRStoryIE):
            for args, enabled in (({}, False), ({'hevc': ['false']}, False), ({'hevc': ['']}, True)):
                with self.subTest(extractor=extractor_type.__name__, args=args), tempfile.TemporaryDirectory() as directory:
                    source = Path(directory, 'video.mp4')
                    source.write_bytes(b'original video')
                    with YoutubeDL({'quiet': True, 'extractor_args': {'instagramhdr': args}}, auto_init=False) as downloader:
                        extractor = extractor_type(downloader)
                        info = PLUGIN_MODULE._InstagramHDRMixin._extract_product_media(extractor, {
                            'pk': '123456789', 'video_dash_manifest': ladder_manifest(),
                        })
                        info.update(filepath=str(source), ext='mp4', dynamic_range='HLG')
                        converter = InstagramHDRHEVCPP(downloader)
                        converter._verify = mock.Mock(return_value='vp9')
                        converter._encode = mock.Mock(side_effect=lambda _src, dest, *_args: Path(dest).write_bytes(b'hevc'))
                        deleted, _result = converter.run(info)
                        self.assertIs(info['__instagram_hdr_hevc'], enabled)
                        if enabled:
                            converter._encode.assert_called_once()
                            self.assertEqual(source.read_bytes(), b'hevc')
                            self.assertEqual(Path(deleted[0]).read_bytes(), b'original video')
                        else:
                            converter._verify.assert_not_called()
                            converter._encode.assert_not_called()
                            self.assertEqual(deleted, [])
                            self.assertEqual(source.read_bytes(), b'original video')
                            self.assertEqual(list(Path(directory).iterdir()), [source])

    def test_hevc_option_is_resolved_per_extractor(self):
        downloader = SimpleNamespace(params={
            'extractor_args': {'instagramhdrstory': {'hevc': ['']}},
        })
        for extractor, expected in ((InstagramHDRIE(), False), (InstagramHDRStoryIE(), True)):
            extractor._downloader = downloader
            extractor.to_screen = lambda _message: None
            result = PLUGIN_MODULE._InstagramHDRMixin._extract_product_media(extractor, {
                'pk': '123456789',
                'video_dash_manifest': ladder_manifest(),
            })
            self.assertIs(result['__instagram_hdr_hevc'], expected)
            self.assertTrue(result['__instagram_hdr_verify'])

    def test_story_info_failure_keeps_tray_formats(self):
        extractor = InstagramHDRStoryIE()
        extractor.__dict__['_can_impersonate'] = False
        extractor._get_cookies = lambda _url: {}
        extractor.to_screen = lambda _message: None
        # With fatal=False, yt-dlp reports the error as a warning and returns False
        extractor._download_json = lambda *_args, fatal=True, **_kwargs: (
            False if fatal is False else self.fail('The HDR story lookup must not be fatal'))

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

        self.assertEqual([item['format_id'] for item in result['formats']], ['sdr'])

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
