import contextlib
import errno
import html
import json
import os
import shutil
import subprocess
import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from yt_dlp import YoutubeDL
from yt_dlp.extractor.instagram import InstagramIE, InstagramStoryIE
from yt_dlp.globals import plugin_dirs
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessorError
from yt_dlp.utils import DownloadCancelled, ExtractorError, PostProcessingError


# Do not mix this checkout with a wheel installed in the user's plugin directory.
plugin_dirs.value = [str(Path(__file__).resolve().parents[2])]
from yt_dlp_plugins.extractor import instagram_hdr as PLUGIN_MODULE

InstagramHDRIE = PLUGIN_MODULE.InstagramHDRIE
InstagramHDRStoryIE = PLUGIN_MODULE.InstagramHDRStoryIE
InstagramHDRVerifyPP = PLUGIN_MODULE.InstagramHDRVerifyPP
InstagramHDRHEVCPP = PLUGIN_MODULE.InstagramHDRHEVCPP
InstagramHDRSelectionPP = PLUGIN_MODULE.InstagramHDRSelectionPP
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


def fake_hevc_converter(downloader=None, encode=None):
    # Files hold b'hevc' once converted; anything else is the original HDR video
    converter = InstagramHDRHEVCPP(downloader)
    converter.to_screen = converter.write_debug = converter.report_warning = lambda *_args, **_kwargs: None
    converter._available_encoders = set()
    converter._verify = lambda path: 'hevc' if Path(path).read_bytes() == b'hevc' else 'vp9'
    converter._encode = encode or (lambda _src, dest, *_args: Path(dest).write_bytes(b'hevc'))
    return converter


def hevc_info(path, ext=None, **extra):
    return {'_instagram_hdr': True, '_instagram_hdr_hevc': True, 'dynamic_range': 'HLG',
            'filepath': str(path), 'ext': ext or Path(path).suffix[1:], **extra}


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
        self.assertFalse(InstagramHDRIE.suitable('https://www.instagram.com/reels/audio/1234567890/'))
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
        self.assertTrue(result['_instagram_hdr'])
        self.assertEqual(
            [item['height'] for item in result['formats'] if str(item.get('format_id', '')).startswith('hdr-')],
            [1280, 1920],
        )
        self.assertIn('https://cdn.example/fallback.mp4', [item['url'] for item in result['formats']])

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

        self.assertNotIn('_instagram_hdr', result)
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

        self.assertNotIn('_instagram_hdr', result)
        self.assertEqual(result['formats'][0]['format_id'], 'sdr')
        self.assertEqual(result['formats'][0]['height'], 1280)
        self.assertIn('available SDR formats', messages[0])

    def test_image_story_does_not_request_media_info(self):
        extractor = InstagramHDRStoryIE()
        extractor._download_json = lambda *_args, **_kwargs: self.fail(
            'Image stories must not request video info')

        result = extractor._extract_product_media({
            'pk': '123456789',
            'media_type': 1,
        })

        self.assertEqual(result['formats'], [])

    def test_sdr_with_unescaped_ampersands_keeps_formats_without_another_extraction(self):
        mpd = manifest('vp09.00.40.08.01.01.01.01.00', 'dash-vp9-sdr').replace(
            '<MPD>', '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT1S">').replace(
            'mp4a.40.42', 'mp4a.40.2')
        product = {
            'pk': '123456789',
            'video_dash_manifest': mpd,
            'video_versions': [{'id': 'progressive', 'url': 'https://cdn.example/progressive.mp4'}],
        }
        with YoutubeDL({'quiet': True}, auto_init=False) as downloader:
            extractor = InstagramHDRIE(downloader)
            media = extractor._extract_product_media(product)
            self.assertNotIn('_instagram_hdr', media)
            self.assertEqual(product['video_dash_manifest'], mpd)
            self.assertEqual({fmt['url'] for fmt in media['formats']}, {
                'https://cdn.example/progressive.mp4',
                'https://cdn.example/video.mp4?a=1&b=2',
                'https://cdn.example/audio.mp4?a=1&b=2',
            })
            extractor._NATIVE_IE = mock.Mock(side_effect=AssertionError('No second client'))
            with mock.patch.object(InstagramHDRIE, '_extract_api', return_value=media):
                result = extractor._real_extract('https://www.instagram.com/p/HW80V/')
            extractor._NATIVE_IE.assert_not_called()
            self.assertIs(result, media)

    def test_media_result_contains_sorted_hdr_video_ladder_and_compatible_audio(self):
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
        self.assertEqual([item['acodec'] for item in result['formats'] if item.get('vcodec') == 'none'],
                         ['mp4a.40.2', 'mp4a.40.2'])
        self.assertEqual(videos[-1]['dynamic_range'], 'HLG')
        self.assertEqual(result['duration'], 12.5)
        self.assertTrue(result['_instagram_hdr'])
        self.assertIn('1080x1920', messages[0])

    def test_native_formats_are_merged_and_xhe_aac_is_rejected_by_default(self):
        hdr = {
            'id': 'ABC123',
            '_instagram_hdr': True,
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
            '_instagram_hdr': True,
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
            '_instagram_hdr': True,
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

    def test_sdr_items_keep_xhe_aac_only_when_enabled_if_compatible_audio_exists(self):
        mpd = manifest('vp09.00.40.08.01.01.01.01.00', 'dash-vp9-sdr').replace('</Period>', '''
          <AdaptationSet contentType="audio">
            <Representation id="compatible" codecs="mp4a.40.5" mimeType="audio/mp4">
              <BaseURL>https://cdn.example/compatible.m4a</BaseURL>
            </Representation>
          </AdaptationSet></Period>''')
        for args, expected in (({}, ['mp4a.40.5']),
                               ({'include_xhe_aac': ['']}, ['mp4a.40.5', 'mp4a.40.42'])):
            extractor = InstagramHDRIE()
            extractor._downloader = SimpleNamespace(params={'extractor_args': {'instagramhdr': args}})
            extractor.to_screen = lambda _message: None
            extractor._NATIVE_IE = mock.Mock(side_effect=AssertionError('No second client'))
            result = extractor._extract_product_media({'pk': '123456789', 'video_dash_manifest': mpd})
            self.assertNotIn('_instagram_hdr', result)
            self.assertEqual([fmt['format_id'] for fmt in result['formats'] if fmt.get('vcodec') != 'none'],
                             ['dash-dash-vp9-sdr'])
            self.assertEqual([fmt['acodec'] for fmt in result['formats'] if fmt.get('vcodec') == 'none'], expected)
            extractor._NATIVE_IE.assert_not_called()

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

        audio = [item for item in result['formats'] if item.get('vcodec') == 'none']
        self.assertEqual([item['acodec'] for item in audio], ['mp4a.40.42', 'mp4a.40.42'])
        self.assertFalse(any('quality' in item for item in audio))

    def test_hdr_with_only_xhe_aac_keeps_audio_without_requesting_another_client(self):
        with YoutubeDL({'quiet': True}, auto_init=False) as downloader:
            extractor = InstagramHDRIE(downloader)
            extractor.__dict__['_can_impersonate'] = False
            extractor._get_cookies = lambda _url: {}
            warnings = []
            extractor.report_warning = warnings.append
            extractor._NATIVE_IE = mock.Mock(side_effect=AssertionError('No second client'))
            extractor._download_json = mock.Mock(return_value={'items': [{
                'pk': '123456789', 'video_dash_manifest': manifest(
                    'vp09.02.40.10.01.09.18.09.00', 'dash-vp9-hdr'),
            }]})
            result = extractor._real_extract('https://www.instagram.com/p/HW80V/')
            self.assertEqual([fmt['format_id'] for fmt in result['formats']],
                             ['hdr-dash-vp9-hdr', 'audio-audio'])
            self.assertEqual(result['formats'][-1]['acodec'], 'mp4a.40.42')
            self.assertEqual(warnings, [])
            extractor._download_json.assert_called_once()
            extractor._NATIVE_IE.assert_not_called()

    def test_authenticated_failures_stop_without_native_fallback_or_cookie_mutation(self):
        class HTTPStatusError(Exception):
            status = 401

        with YoutubeDL({'quiet': True}, auto_init=False) as downloader:
            post, story = InstagramHDRIE(downloader), InstagramHDRStoryIE(downloader)
            post.__dict__['_can_impersonate'] = False
            cookies = {'sessionid': SimpleNamespace(value='test-session')}
            post._get_cookies = lambda _url: cookies
            post._download_json = mock.Mock(side_effect=ExtractorError(
                'Video info extraction failed: HTTP Error 401', cause=HTTPStatusError()))
            post._NATIVE_IE = mock.Mock(side_effect=AssertionError('No native fallback'))
            story._extract_api = mock.Mock(side_effect=AssertionError('Session must stay stopped'))
            # The built-in path can clear sessionid on a login redirect; it must not run.
            with mock.patch.object(InstagramIE, '_real_extract', side_effect=AssertionError):
                with self.assertRaisesRegex(ExtractorError, 'stopped for this run'):
                    post._real_extract('https://www.instagram.com/p/ABC123/')
                with self.assertRaisesRegex(ExtractorError, 'stopped for this run'):
                    story._real_extract('https://www.instagram.com/stories/example/')
            self.assertEqual(cookies['sessionid'].value, 'test-session')
            post._download_json.assert_called_once()
            post._NATIVE_IE.assert_not_called()
            story._extract_api.assert_not_called()

        with YoutubeDL({'quiet': True}, auto_init=False) as downloader:
            story, debug = InstagramHDRStoryIE(downloader), []
            story.write_debug = debug.append
            story._extract_api = mock.Mock(side_effect=AttributeError('setdefault'))
            story._NATIVE_IE = mock.Mock(side_effect=AssertionError('No native fallback'))
            with self.assertRaisesRegex(ExtractorError, 'unexpected media data'):
                story._real_extract('https://www.instagram.com/stories/example/')
            story._extract_api.assert_called_once()
            story._NATIVE_IE.assert_not_called()
            self.assertIn('Traceback', debug[0])

    def test_post_web_request_preserves_hdr_and_audio_from_the_same_response(self):
        media_id = PLUGIN_MODULE._pk_to_id('123456789')
        extractor = InstagramHDRIE()
        extractor._downloader = SimpleNamespace(params={})
        extractor.__dict__['_can_impersonate'] = False
        extractor._get_cookies = lambda _url: {}
        extractor.to_screen = lambda _message: None
        requests = []
        extractor._download_json = lambda url, *_args, headers, **_kwargs: requests.append(
            (url, headers['X-IG-App-ID'])) or {'items': [{
                'pk': '123456789', 'video_dash_manifest': ladder_manifest(), 'user': {'username': 'user'},
            }]}
        extractor._NATIVE_IE = mock.Mock(side_effect=AssertionError('No second client'))
        result = extractor._real_extract(f'https://www.instagram.com/p/{media_id}/')
        self.assertEqual(requests, [(
            'https://www.instagram.com/api/v1/media/123456789/info/', extractor._APP_IDS['web'])])
        self.assertEqual([fmt['format_id'] for fmt in result['formats']],
                         ['hdr-dash-vp9-hdr_q70', 'hdr-dash-vp9-hdr-hfr_q90',
                          'audio-audio-low', 'audio-audio-high'])
        extractor._NATIVE_IE.assert_not_called()

    def test_xhe_aac_is_kept_only_when_it_is_the_only_audio(self):
        xhe = {'format_id': 'ios-xhe', 'vcodec': 'none', 'acodec': 'mp4a.40.42'}
        info = {'_type': 'playlist', 'entries': [
            {'id': 'HDR', 'formats': [{'format_id': 'hdr-v', 'vcodec': 'vp9', 'acodec': 'none'}, dict(xhe)]},
            {'id': 'SDR', 'formats': [{'format_id': 'progressive', 'vcodec': 'h264', 'acodec': None}, dict(xhe)]},
        ]}
        native = {'_type': 'playlist', 'entries': [{'id': 'HDR', 'formats': []}]}
        self.assertEqual(_merge_native_formats(info, native), ['HDR', 'SDR'])
        self.assertEqual([[fmt['format_id'] for fmt in entry['formats']] for entry in info['entries']],
                         [['hdr-v', 'ios-xhe'], ['progressive', 'ios-xhe']])

    def test_missing_story_returns_nothing_without_the_native_pass(self):
        extractor = InstagramHDRStoryIE()
        extractor._NATIVE_IE = mock.Mock(side_effect=AssertionError('no second request'))
        with mock.patch.object(InstagramHDRStoryIE, '_extract_api', return_value=None):
            self.assertIsNone(extractor._real_extract('https://www.instagram.com/stories/example/123/'))

    def test_only_the_items_own_https_manifest_is_parsed(self):
        fake = manifest('vp09.02.50.10.01.09.18.09.00', 'hdr', 'https://attacker.example/x.mp4').replace(
            'width="1080" height="1920"', 'width="4320" height="7680"')
        extractor = InstagramHDRIE()
        extractor.to_screen = lambda _message: None
        result = extractor._extract_product_media({
            'pk': '123456789',
            'caption': {'text': f'nice sunset {fake}'},
            'clips_metadata': {'original_sound_info': {'dash_manifest': fake}},
            'video_dash_manifest': ladder_manifest().replace(
                'https://cdn.example/video-low.mp4?a=1&amp;b=2', 'http://cdn.example/video-low.mp4'),
        })
        self.assertEqual([(fmt['url'], fmt['protocol']) for fmt in result['formats']],
                         [('https://cdn.example/video-high.mp4?a=1&b=2', 'https'),
                          ('https://cdn.example/audio-low.mp4', 'https'),
                          ('https://cdn.example/audio-high.mp4', 'https')])
        self.assertIsNone(extractor._extract_product_media({
            'pk': 'not-a-number', 'video_dash_manifest': ladder_manifest()})['id'])
        self.assertEqual(extractor._extract_product_media({
            'pk': '1', 'video_dash_manifest': {'not': 'a string'}})['formats'], [])

    def test_api_headers_use_csrf_cookie_without_mobile_identity_fields(self):
        extractor = InstagramHDRIE()
        values = {'csrftoken': 'test-csrf', 'ds_user_id': '42', 'rur': 'NHA,42,1790000000:01fe\\x4'}
        extractor._get_cookies = lambda _url: {
            name: SimpleNamespace(value=value) for name, value in values.items()}
        headers = extractor._api_headers
        self.assertEqual(headers['X-CSRFToken'], values['csrftoken'])
        self.assertEqual(headers['X-IG-App-ID'], extractor._APP_IDS['web'])
        self.assertNotIn('ig-u-rur', headers)
        self.assertNotIn('ig-u-ds-user-id', headers)
        self.assertNotIn('User-Agent', headers)

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
                '_instagram_hdr': True,
                'dynamic_range': 'HLG',
                'filepath': output.name,
            })
        self.assertEqual(deleted, [])
        self.assertTrue(info['_instagram_hdr'])
        self.assertIn('HDR verified', messages[0])

    def test_hdr_verifier_warns_on_sdr_without_failing_the_download(self):
        verifier = InstagramHDRVerifyPP()
        warnings = []
        verifier.report_warning = warnings.append
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
            deleted, _info = verifier.run({
                '_instagram_hdr': True,
                'dynamic_range': 'HLG',
                'filepath': output.name,
            })
        self.assertEqual(deleted, [])
        self.assertIn('HDR verification failed', warnings[0])

    def test_hdr_verifier_accepts_any_10bit_hdr_codec_and_explains_ffprobe_failures(self):
        verifier = InstagramHDRVerifyPP()
        verifier.to_screen = lambda _message: None
        verifier.get_metadata_object = lambda *_args, **_kwargs: {'streams': [{
            'codec_type': 'video', 'codec_name': 'h264', 'profile': 'High 10',
            'pix_fmt': 'yuv420p10le', 'color_primaries': 'bt2020',
            'color_transfer': 'arib-std-b67', 'color_space': 'bt2020nc',
        }]}
        self.assertEqual(verifier._verify('video.mkv'), 'h264')

        verifier.get_metadata_object = mock.Mock(side_effect=ValueError('Expecting value'))
        with self.assertRaisesRegex(PostProcessingError, 'ffprobe could not read'):
            verifier._verify('video.mp4')

    def test_hdr_verifier_skips_native_sdr_selection(self):
        verifier = InstagramHDRVerifyPP()
        verifier.get_metadata_object = lambda *_args, **_kwargs: self.fail(
            'SDR selections from the combined format list must not be HDR-verified')

        with tempfile.NamedTemporaryFile() as output:
            deleted, _info = verifier.run({
                '_instagram_hdr': True,
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
                '_instagram_hdr': True,
                '_instagram_hdr_hevc': True,
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
            self.assertIn('-0:t', calls[-1])

            codecs[mp4] = {'codec_name': 'hevc', 'profile': 'Main 10'}
            calls.clear()
            rerun = {**info, 'vcodec': 'vp09.02.40.10.01.09.18.09.00'}
            self.assertEqual(converter.run(rerun), ([], rerun))
            self.assertEqual((rerun['filepath'], rerun['ext'], rerun['vcodec']), (mp4, 'mp4', 'hevc'))
            self.assertEqual(calls, [])

    def test_post_processors_follow_per_item_options(self):
        verifier, converter = InstagramHDRVerifyPP(), InstagramHDRHEVCPP()
        for post_processor in (verifier, converter):
            post_processor.get_metadata_object = lambda *_args, **_kwargs: self.fail(
                'The options stored on the item decide which post-processor probes it')

        with tempfile.NamedTemporaryFile() as output:
            info = {'_instagram_hdr': True, 'dynamic_range': 'HLG', 'filepath': output.name}
            converter.run(info)
            verifier.run({**info, '_instagram_hdr_hevc': True})
            verifier.run({**info, '_instagram_hdr_verify': False})

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
                                '_instagram_hdr': True, '_instagram_hdr_hevc': True,
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
                            '_instagram_hdr': True, '_instagram_hdr_hevc': True,
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
                    info = {'_instagram_hdr': True, '_instagram_hdr_hevc': cls is InstagramHDRHEVCPP,
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
                    converter = InstagramHDRHEVCPP(YoutubeDL({'quiet': True, 'overwrites': True}, auto_init=False))
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
                    info = {'_instagram_hdr': True, '_instagram_hdr_hevc': True,
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
                        converter._verify = mock.Mock(side_effect=lambda path: (
                            'hevc' if Path(path).read_bytes() == b'hevc' else 'vp9'))
                        converter._encode = mock.Mock(side_effect=lambda _src, dest, *_args: Path(dest).write_bytes(b'hevc'))
                        deleted, _result = converter.run(info)
                        self.assertIs(info['_instagram_hdr_hevc'], enabled)
                        if enabled:
                            converter._encode.assert_called_once()
                            self.assertEqual(source.read_bytes(), b'hevc')
                            self.assertEqual(deleted, [])
                            self.assertEqual(list(Path(directory).iterdir()), [source])
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
            self.assertIs(result['_instagram_hdr_hevc'], expected)
            self.assertTrue(result['_instagram_hdr_verify'])

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

    def test_hevc_never_replaces_an_existing_destination_without_force_overwrites(self):
        for params, replaced in (({}, False), ({'overwrites': False}, False), ({'overwrites': True}, True)):
            with self.subTest(params=params), tempfile.TemporaryDirectory() as directory, \
                    YoutubeDL({'quiet': True, **params}, auto_init=False) as downloader:
                source, existing = Path(directory, 'video [ID].mkv'), Path(directory, 'video [ID].mp4')
                source.write_bytes(b'vp9')
                existing.write_bytes(b'another video')
                converter = fake_hevc_converter(downloader)
                if replaced:
                    self.assertEqual(converter.run(hevc_info(source))[0], [str(source)])
                    self.assertEqual(existing.read_bytes(), b'hevc')
                else:
                    with self.assertRaisesRegex(PostProcessingError, 'force-overwrites'):
                        converter.run(hevc_info(source))
                    self.assertEqual((source.read_bytes(), existing.read_bytes()), (b'vp9', b'another video'))

    def test_hevc_output_keeps_dotted_names_without_an_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, 'clip.v1')
            source.write_bytes(b'vp9')
            info = hevc_info(source, ext='mp4')
            self.assertEqual(fake_hevc_converter().run(info)[0], [str(source)])
            self.assertEqual(info['filepath'], str(Path(directory, 'clip.v1.mp4')))

    def test_hevc_keeps_originals_only_for_keep_video_or_no_overwrites(self):
        # (params, __real_download, stale backup present) -> expected outcome
        cases = [
            ({}, True, True, 'replaced in place'),
            ({'keepvideo': True}, True, True, 'refused'),
            ({'keepvideo': True, 'overwrites': True}, True, True, 'kept by -k'),
            ({'keepvideo': True}, True, False, 'kept by -k'),
            ({'overwrites': False}, False, False, 'kept by -w'),
            ({'overwrites': False}, True, False, 'replaced in place'),
        ]
        for params, real_download, stale, outcome in cases:
            with self.subTest(params=params, real_download=real_download, stale=stale), \
                    tempfile.TemporaryDirectory() as directory, \
                    YoutubeDL({'quiet': True, **params}, auto_init=False) as downloader:
                source, backup = Path(directory, 'video.mp4'), Path(directory, 'video.orig.mp4')
                source.write_bytes(b'vp9')
                if stale:
                    backup.write_bytes(b'stale backup')
                info = hevc_info(source, __real_download=real_download)
                converter = fake_hevc_converter(downloader)
                if outcome == 'refused':
                    with self.assertRaisesRegex(PostProcessingError, 'force-overwrites'):
                        converter.run(info)
                    self.assertEqual((source.read_bytes(), backup.read_bytes()), (b'vp9', b'stale backup'))
                    continue
                deleted, info = converter.run(info)
                self.assertEqual(source.read_bytes(), b'hevc')
                if outcome == 'replaced in place':
                    self.assertEqual(deleted, [])
                    if stale:
                        self.assertEqual(backup.read_bytes(), b'stale backup')
                    else:
                        self.assertFalse(backup.exists())
                else:
                    self.assertEqual(backup.read_bytes(), b'vp9')
                    self.assertEqual(deleted, [str(backup)] if outcome == 'kept by -k' else [])
                    if outcome == 'kept by -w':
                        self.assertIn(str(backup), info['__files_to_move'])

    def test_hevc_never_replaces_a_backup_created_while_encoding(self):
        def encode(_src, dest, *_args):
            Path(dest).write_bytes(b'hevc')
            Path(dest).with_name('video.orig.mp4').write_bytes(b'user data')

        with tempfile.TemporaryDirectory() as directory, \
                YoutubeDL({'quiet': True, 'keepvideo': True}, auto_init=False) as downloader:
            source = Path(directory, 'video.mp4')
            source.write_bytes(b'vp9')
            with self.assertRaisesRegex(PostProcessingError, 'appeared while encoding'):
                fake_hevc_converter(downloader, encode).run(hevc_info(source))
            self.assertEqual({path.name: path.read_bytes() for path in Path(directory).iterdir()},
                             {'video.mp4': b'vp9', 'video.orig.mp4': b'user data'})

    def test_hevc_backup_falls_back_to_rename_without_hard_links(self):
        with tempfile.TemporaryDirectory() as directory, \
                YoutubeDL({'quiet': True, 'keepvideo': True}, auto_init=False) as downloader:
            source = Path(directory, 'video.mp4')
            source.write_bytes(b'vp9')
            with mock.patch('os.link', side_effect=OSError(errno.ENOTSUP, 'no hard links')):
                deleted, _info = fake_hevc_converter(downloader).run(hevc_info(source))
            self.assertEqual(deleted, [str(Path(directory, 'video.orig.mp4'))])
            self.assertEqual({path.name: path.read_bytes() for path in Path(directory).iterdir()},
                             {'video.mp4': b'hevc', 'video.orig.mp4': b'vp9'})

    def test_hevc_temporary_file_is_visible_and_always_removed(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                source, stale = Path(directory, 'video.webm'), Path(directory, 'video.temp.mp4')
                source.write_bytes(b'vp9')
                stale.write_bytes(b'left by a killed run')
                destinations = []

                def encode(_src, dest, *_args):
                    destinations.append(dest)
                    Path(dest).write_bytes(b'broken' if fail else b'hevc')

                converter = fake_hevc_converter(encode=encode)
                if fail:
                    converter._verify = lambda path: 'vp9' if path == str(source) else (_ for _ in ()).throw(
                        PostProcessingError('Instagram HDR verification failed'))
                    with self.assertRaises(PostProcessingError):
                        converter.run(hevc_info(source))
                else:
                    converter.run(hevc_info(source))
                self.assertEqual(set(destinations), {str(stale)})
                self.assertFalse(stale.exists())

    def test_hevc_encoder_selection_and_fallback(self):
        cases = {
            'libx265 only': ({'libx265'}, {}, ['libx265'], True),
            'VideoToolbox output fails verification': (set(), {'hevc_videotoolbox': b'8-bit'},
                                                       ['hevc_videotoolbox', 'libx265'], True),
            'no HEVC encoder': ({'h264_videotoolbox'}, {}, [], False),
        }
        for name, (available, outputs, expected, succeeds) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                source = Path(directory, 'video.webm')
                source.write_bytes(b'vp9')
                used, messages = [], []

                def encode(_src, dest, encoder, *_args, outputs=outputs, used=used):
                    used.append(encoder)
                    Path(dest).write_bytes(outputs.get(encoder, b'hevc'))

                converter = fake_hevc_converter(encode=encode)
                converter._available_encoders = available
                converter.to_screen = messages.append
                converter._verify = lambda path: {b'hevc': 'hevc', b'vp9': 'vp9'}.get(
                    Path(path).read_bytes()) or (_ for _ in ()).throw(PostProcessingError('not Main 10'))
                if succeeds:
                    converter.run(hevc_info(source))
                    self.assertEqual(Path(directory, 'video.mp4').read_bytes(), b'hevc')
                else:
                    with self.assertRaisesRegex(PostProcessingError, 'hevc_videotoolbox.*libx265'):
                        converter.run(hevc_info(source))
                    self.assertEqual(source.read_bytes(), b'vp9')
                self.assertEqual(used, expected)
                self.assertEqual(any('failed' in message for message in messages), len(expected) > 1)

    def test_hevc_targets_the_main_video_when_cover_art_comes_first(self):
        converter = InstagramHDRHEVCPP()
        converter.to_screen = lambda *_args, **_kwargs: None
        converter.get_metadata_object = lambda *_args, **_kwargs: {'streams': [
            {'codec_type': 'video', 'codec_name': 'mjpeg', 'disposition': {'attached_pic': 1}},
            {'codec_type': 'video', 'codec_name': 'vp9', 'profile': 'Profile 2', 'pix_fmt': 'yuv420p10le',
             'color_primaries': 'bt2020', 'color_transfer': 'arib-std-b67', 'color_space': 'bt2020nc'},
        ]}
        converter.run_ffmpeg = mock.Mock()
        self.assertEqual(converter._verify('video.mp4'), 'vp9')
        converter._encode('video.mp4', 'video.temp.mp4', 'libx265', '-crf', '22')
        opts = converter.run_ffmpeg.call_args.args[2]
        self.assertEqual(opts[opts.index('-c:v:1') + 1], 'libx265')
        self.assertIn('-tag:v:1', opts)
        self.assertNotIn('-c:v:0', opts)

    def test_ppa_options_follow_the_plugin_encoder_options(self):
        self.assertEqual(InstagramHDRHEVCPP.pp_key(), 'InstagramHDRHEVC')
        with tempfile.TemporaryDirectory() as directory, YoutubeDL({
                'quiet': True, 'no_warnings': True,
                'postprocessor_args': {'instagramhdrhevc': ['-q:v', '40']}}, auto_init=False) as downloader:
            source = Path(directory, 'video.mp4')
            source.write_bytes(b'vp9')
            converter = InstagramHDRHEVCPP(downloader)
            # CI has no ffmpeg: pin the binary so the real command is built with Popen mocked
            converter.__dict__['basename'] = 'ffmpeg'
            converter._paths = {'ffmpeg': 'ffmpeg'}
            converter.check_version = lambda: None
            converter._main_video = lambda _path: (0, {})
            with mock.patch('yt_dlp.postprocessor.ffmpeg.Popen.run', return_value=('', '', 0)) as run:
                converter._encode(str(source), str(Path(directory, 'out.mp4')), 'hevc_videotoolbox', '-q', '50')
            command = run.call_args.args[0]
            self.assertLess(command.index('-q:v:0'), command.index('-q:v'))

    def test_multi_video_selection_warns_once_and_skips(self):
        videos = [{'format_id': 'hdr-a', 'vcodec': 'vp9', 'dynamic_range': 'HLG'},
                  {'format_id': 'dash-b', 'vcodec': 'vp9', 'dynamic_range': 'SDR'},
                  {'format_id': 'dash-c', 'vcodec': 'none', 'acodec': 'mp4a.40.5'}]
        with tempfile.NamedTemporaryFile(suffix='.mkv') as output:
            for cls, hevc in ((InstagramHDRVerifyPP, False), (InstagramHDRHEVCPP, True)):
                with self.subTest(postprocessor=cls.__name__):
                    postprocessor, warnings = cls(), []
                    postprocessor.report_warning = warnings.append
                    postprocessor._verify = mock.Mock(side_effect=AssertionError('must not probe'))
                    info = {'_instagram_hdr': True, '_instagram_hdr_hevc': hevc, 'ext': 'mkv',
                            'filepath': output.name, 'requested_formats': videos}
                    self.assertEqual(postprocessor.run(info), ([], info))
                    self.assertEqual(len(warnings), 1)
                    self.assertIn('several video streams', warnings[0])

    def test_selection_warns_when_sdr_video_is_chosen_although_hdr_exists(self):
        hdr = {'format_id': 'hdr-q90', 'vcodec': 'vp9', 'dynamic_range': 'HLG'}
        sdr = {'format_id': 'dash-sdr', 'vcodec': 'vp9', 'dynamic_range': 'SDR'}
        audio = {'format_id': 'dash-a', 'vcodec': 'none', 'acodec': 'mp4a.40.5'}
        cases = [
            ({'_instagram_hdr': True, 'format_id': 'dash-sdr+dash-a', 'requested_formats': [sdr, audio]}, True),
            ({'_instagram_hdr': True, 'format_id': 'prog', 'vcodec': 'avc1', 'dynamic_range': 'SDR'}, True),
            ({'_instagram_hdr': True, 'format_id': 'hdr-q90+dash-a', 'requested_formats': [hdr, audio]}, False),
            ({'_instagram_hdr': True, 'format_id': 'dash-a', 'vcodec': 'none'}, False),
            ({'format_id': 'dash-sdr+dash-a', 'requested_formats': [sdr, audio]}, False),
        ]
        for info, warns in cases:
            with self.subTest(format_id=info['format_id'], hdr=info.get('_instagram_hdr')):
                selection, warnings = InstagramHDRSelectionPP(), []
                selection.report_warning = warnings.append
                self.assertEqual(selection.run(info), ([], info))
                self.assertEqual(len(warnings), int(warns))
        # With several requested downloads another one may be the HDR one
        with YoutubeDL({'quiet': True, 'format': 'hdr-q90+dash-a,dash-sdr+dash-a'}, auto_init=False) as downloader:
            selection, warnings = InstagramHDRSelectionPP(downloader), []
            selection.report_warning = warnings.append
            selection.run(cases[0][0])
            self.assertEqual(warnings, [])

    def test_post_processors_are_registered_once_around_the_cli_steps(self):
        postprocessors = [
            {'key': 'FFmpegExtractAudio', 'preferredcodec': 'best'},
            {'key': 'FFmpegMetadata', 'add_metadata': True},
            {'key': 'Exec', 'exec_cmd': ['true'], 'when': 'after_move'},
        ]
        with YoutubeDL({'quiet': True, 'postprocessors': postprocessors}) as downloader:
            for extractor_type in (InstagramHDRIE, InstagramHDRStoryIE):
                extractor = extractor_type(downloader)
                extractor._get_cookies = lambda _url: {'sessionid': object(), 'ds_user_id': object()}
                extractor._real_initialize()
            names = {when: [pp.pp_key() for pp in pps] for when, pps in downloader._pps.items()}
            self.assertEqual(names['video'], ['InstagramHDRSelection'])
            self.assertEqual(names['post_process'], ['ExtractAudio', 'InstagramHDRHEVC', 'Metadata'])
            self.assertEqual(names['after_move'], ['InstagramHDRVerify', 'Exec'])
            self.assertIsNone(downloader.params.get('final_ext'))

    def test_hevc_runs_before_each_final_file_step(self):
        steps = {
            None: None,
            'VideoRemuxer': {'key': 'FFmpegVideoRemuxer', 'preferedformat': 'mkv'},
            'VideoConvertor': {'key': 'FFmpegVideoConvertor', 'preferedformat': 'mkv'},
            'EmbedSubtitle': {'key': 'FFmpegEmbedSubtitle'},
            'ModifyChapters': {'key': 'ModifyChapters'},
            'Metadata': {'key': 'FFmpegMetadata', 'add_metadata': True},
            'EmbedThumbnail': {'key': 'EmbedThumbnail'},
            'SplitChapters': {'key': 'FFmpegSplitChapters'},
            'XAttrMetadata': {'key': 'XAttrMetadata'},
            'Exec': {'key': 'Exec', 'exec_cmd': ['true'], 'when': 'post_process'},
        }
        for key, step in steps.items():
            postprocessors = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'best'}, *([step] if step else [])]
            with self.subTest(before=key), YoutubeDL({'quiet': True, 'postprocessors': postprocessors}) as downloader:
                extractor = InstagramHDRIE(downloader)
                extractor._get_cookies = lambda _url: {'sessionid': object(), 'ds_user_id': object()}
                extractor._real_initialize()
                self.assertEqual([pp.pp_key() for pp in downloader._pps['post_process']],
                                 ['ExtractAudio', 'InstagramHDRHEVC', *([key] if key else [])])

    def test_final_ext_is_set_only_for_items_that_will_be_converted(self):
        hdr = {'format_id': 'hdr-q90', 'vcodec': 'vp9', 'dynamic_range': 'HLG'}
        sdr = {'format_id': 'dash-sdr', 'vcodec': 'vp9', 'dynamic_range': 'SDR'}
        audio = {'format_id': 'dash-a', 'vcodec': 'none', 'acodec': 'mp4a.40.5'}

        def item(*formats, hevc=True):
            return {'_instagram_hdr': True, '_instagram_hdr_hevc': hevc, 'requested_formats': list(formats)}

        cases = [
            ({}, item(hdr, audio), 'mp4'),
            ({}, item(hdr, audio, hevc=False), None),
            ({}, item(sdr, audio), None),
            ({}, item(hdr, sdr, audio), None),  # --video-multistreams
            ({}, {'_instagram_hdr': True, '_instagram_hdr_hevc': True, 'format_id': 'dash-a', 'vcodec': 'none'}, None),
            ({}, {'format_id': 'other-site', 'vcodec': 'vp9', 'dynamic_range': 'SDR'}, None),
            # --remux-video decides the container, also with a conditional spec
            ({'final_ext': 'mkv', 'postprocessors': [
                {'key': 'FFmpegVideoRemuxer', 'preferedformat': 'mkv'}]}, item(hdr, audio), 'mkv'),
            ({'postprocessors': [{'key': 'FFmpegVideoRemuxer', 'preferedformat': 'mp4>mkv'}]}, item(hdr, audio), 'mkv'),
            # --recode-video mkv undoes the conversion (skipped); --recode-video mp4 does nothing to it
            ({'final_ext': 'mkv', 'postprocessors': [
                {'key': 'FFmpegVideoConvertor', 'preferedformat': 'mkv'}]}, item(hdr, audio), 'mkv'),
            ({'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}]}, item(hdr, audio), 'mp4'),
            ({'postprocessors': [{'key': 'FFmpegExtractAudio'}]}, item(hdr, audio), None),  # -x
            ({'postprocessors': [{'key': 'FFmpegExtractAudio', 'when': 'after_move'}]}, item(hdr, audio), None),
        ]
        for params, info, expected in cases:
            with self.subTest(params=params, info=info), \
                    YoutubeDL({'quiet': True, 'no_warnings': True, **params}) as downloader:
                selection = InstagramHDRSelectionPP(downloader)
                downloader.params['final_ext'] = 'left over by the previous item'
                selection.run(info)
                self.assertEqual(downloader.params.get('final_ext'), expected)

    def test_post_processors_added_by_the_user_are_not_duplicated(self):
        with YoutubeDL({'quiet': True, 'postprocessors': [
                {'key': 'InstagramHDRHEVC', 'when': 'post_process'},
                {'key': 'InstagramHDRVerify', 'when': 'after_move'}]}) as downloader:
            extractor = InstagramHDRIE(downloader)
            extractor._get_cookies = lambda _url: {'sessionid': object(), 'ds_user_id': object()}
            extractor._real_initialize()
            self.assertEqual([pp.pp_key() for pp in downloader._pps['post_process']], ['InstagramHDRHEVC'])
            self.assertEqual([pp.pp_key() for pp in downloader._pps['after_move']], ['InstagramHDRVerify'])

    def test_hdr_options_survive_an_info_json_round_trip(self):
        with YoutubeDL({'quiet': True, 'extractor_args': {'instagramhdr': {'hevc': ['']}}},
                       auto_init=False) as downloader, tempfile.TemporaryDirectory() as directory:
            extractor = InstagramHDRIE(downloader)
            extractor.to_screen = lambda _message: None
            info = PLUGIN_MODULE._InstagramHDRMixin._extract_product_media(extractor, {
                'pk': '123456789', 'video_dash_manifest': ladder_manifest()})
            loaded = json.loads(json.dumps(downloader.sanitize_info(info, remove_private_keys=True)))
            self.assertTrue(loaded['_instagram_hdr'] and loaded['_instagram_hdr_hevc'])
            source = Path(directory, 'video.mp4')
            source.write_bytes(b'vp9')
            loaded.update(filepath=str(source), ext='mp4', dynamic_range='HLG')
            fake_hevc_converter(downloader).run(loaded)
            self.assertEqual(source.read_bytes(), b'hevc')

    def test_hevc_skips_a_source_that_is_not_hdr_but_fails_without_ffprobe(self):
        sdr = {'codec_type': 'video', 'codec_name': 'vp9', 'profile': 'Profile 0', 'pix_fmt': 'yuv420p',
               'color_primaries': 'bt709', 'color_transfer': 'bt709', 'color_space': 'bt709'}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, 'video.mp4')
            source.write_bytes(b'older SDR download')
            converter, warnings = fake_hevc_converter(), []
            del converter._verify  # the real check, on mocked ffprobe output
            converter.report_warning = warnings.append
            converter._encode = mock.Mock()
            converter.get_metadata_object = lambda *_args, **_kwargs: {'streams': [sdr]}
            info = hevc_info(source)
            self.assertEqual(converter.run(info), ([], info))
            converter._encode.assert_not_called()
            self.assertIn('force-overwrites', warnings[0])
            self.assertEqual(source.read_bytes(), b'older SDR download')
            # A missing ffprobe is not "not HDR": the requested conversion must fail loudly
            converter.get_metadata_object = mock.Mock(side_effect=PostProcessingError('ffprobe not found'))
            with self.assertRaisesRegex(PostProcessingError, 'ffprobe not found'):
                converter.run(info)

    def test_hevc_is_skipped_when_a_later_step_undoes_it(self):
        for postprocessor, reason in (({'key': 'FFmpegVideoConvertor', 'preferedformat': 'mkv'}, 'recode-video'),
                                      ({'key': 'FFmpegExtractAudio', 'when': 'after_move'}, '-x')):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory, \
                    YoutubeDL({'quiet': True, 'postprocessors': [postprocessor]}) as downloader:
                source = Path(directory, 'video.mp4')
                source.write_bytes(b'vp9')
                converter, warnings = fake_hevc_converter(downloader), []
                converter.report_warning = warnings.append
                converter._encode = mock.Mock()
                info = hevc_info(source)
                self.assertEqual(converter.run(info), ([], info))
                converter._encode.assert_not_called()
                self.assertIn(reason, warnings[0])

    def test_selection_warning_edge_cases(self):
        hdr = {'format_id': 'hdr-q90', 'vcodec': 'vp9', 'dynamic_range': 'HLG'}
        sdr = {'format_id': 'dash-sdr', 'vcodec': 'vp9', 'dynamic_range': 'SDR'}
        audio = {'format_id': 'dash-a', 'vcodec': 'none', 'acodec': 'mp4a.40.5'}
        cases = [
            # --video-multistreams with an HDR video: the multi-video warning covers it
            ({}, {'_instagram_hdr': True, 'format_id': 'hdr+sdr+a', 'requested_formats': [hdr, sdr, audio]}, 0),
            # a progressive format without vcodec is still a video
            ({}, {'_instagram_hdr': True, 'format_id': '1080', 'dynamic_range': 'SDR', 'acodec': None}, 1),
            ({'format': 'all'}, {'_instagram_hdr': True, **sdr}, 0),
            ({'format': lambda _ctx: iter(())}, {'_instagram_hdr': True, **sdr}, 0),
        ]
        for params, info, count in cases:
            with self.subTest(format_id=info['format_id'], format=params.get('format')), \
                    YoutubeDL({'quiet': True, **params}, auto_init=False) as downloader:
                selection, warnings = InstagramHDRSelectionPP(downloader), []
                selection.report_warning = warnings.append
                selection.run(info)
                self.assertEqual(len(warnings), count)

    def test_api_extraction_lets_control_flow_errors_through(self):
        extractor = InstagramHDRIE()
        extractor._downloader = SimpleNamespace(params={})
        extractor.to_screen = lambda _message: None

        def cancelled(_url):
            raise DownloadCancelled('stop')

        extractor._extract_api = cancelled
        extractor._NATIVE_IE = mock.Mock(side_effect=AssertionError('No native fallback'))
        with self.assertRaises(DownloadCancelled):
            extractor._real_extract('https://www.instagram.com/p/ABC/')
        extractor._NATIVE_IE.assert_not_called()

    def test_hevc_encode_copies_every_other_stream_and_forces_main10(self):
        converter = InstagramHDRHEVCPP()
        converter._main_video = lambda _path: (0, {})
        converter.run_ffmpeg = mock.Mock()
        converter._encode('in.webm', 'out.temp.mp4', 'libx265', '-crf', '22')
        opts = converter.run_ffmpeg.call_args.args[2]
        pairs = list(zip(opts, opts[1:]))
        self.assertIn(('-map', '0'), pairs)
        # The per-stream encoder must come after the blanket copy so it overrides it
        self.assertLess(pairs.index(('-c', 'copy')), pairs.index(('-c:v:0', 'libx265')))
        self.assertIn(('-profile:v:0', 'main10'), pairs)

    def test_hevc_undoes_the_backup_when_the_final_swap_fails(self):
        real_replace = os.replace

        def replace(src, dst):
            if str(src).endswith('.temp.mp4'):
                raise OSError(errno.EACCES, 'replace failed')
            return real_replace(src, dst)

        for hard_links in (True, False):
            with self.subTest(hard_links=hard_links), tempfile.TemporaryDirectory() as directory, \
                    YoutubeDL({'quiet': True, 'keepvideo': True}, auto_init=False) as downloader, \
                    mock.patch('os.replace', side_effect=replace), contextlib.ExitStack() as stack:
                if not hard_links:
                    stack.enter_context(mock.patch('os.link', side_effect=OSError(errno.ENOTSUP, 'no hard links')))
                source = Path(directory, 'video.mp4')
                source.write_bytes(b'vp9')
                with self.assertRaisesRegex(OSError, 'replace failed'):
                    fake_hevc_converter(downloader).run(hevc_info(source))
                self.assertEqual({path.name: path.read_bytes() for path in Path(directory).iterdir()},
                                 {'video.mp4': b'vp9'})

    def test_hevc_rename_fallback_never_replaces_a_backup_created_while_encoding(self):
        def encode(_src, dest, *_args):
            Path(dest).write_bytes(b'hevc')
            Path(dest).with_name('video.orig.mp4').write_bytes(b'user data')

        with tempfile.TemporaryDirectory() as directory, \
                YoutubeDL({'quiet': True, 'keepvideo': True}, auto_init=False) as downloader, \
                mock.patch('os.link', side_effect=OSError(errno.ENOTSUP, 'no hard links')):
            source = Path(directory, 'video.mp4')
            source.write_bytes(b'vp9')
            with self.assertRaisesRegex(PostProcessingError, 'appeared while encoding'):
                fake_hevc_converter(downloader, encode).run(hevc_info(source))
            self.assertEqual({path.name: path.read_bytes() for path in Path(directory).iterdir()},
                             {'video.mp4': b'vp9', 'video.orig.mp4': b'user data'})

    def test_hevc_no_overwrites_keep_preserves_other_files_to_move(self):
        with tempfile.TemporaryDirectory() as directory, \
                YoutubeDL({'quiet': True, 'overwrites': False}, auto_init=False) as downloader:
            source = Path(directory, 'video.mp4')
            source.write_bytes(b'vp9')
            subtitle = str(Path(directory, 'video.en.vtt'))
            info = hevc_info(source, __real_download=False, __files_to_move={subtitle: 'final/video.en.vtt'})
            _deleted, info = fake_hevc_converter(downloader).run(info)
            self.assertEqual(info['__files_to_move'], {
                subtitle: 'final/video.en.vtt', str(Path(directory, 'video.orig.mp4')): ''})

    def test_hdr_postprocessors_skip_a_missing_file_or_a_missing_dynamic_range(self):
        with tempfile.NamedTemporaryFile(suffix='.mp4') as output:
            for cls in (InstagramHDRVerifyPP, InstagramHDRHEVCPP):
                for case in ('missing file', 'no dynamic_range'):
                    with self.subTest(postprocessor=cls.__name__, case=case):
                        postprocessor = cls()
                        postprocessor._verify = mock.Mock(side_effect=AssertionError('must not probe'))
                        info = hevc_info(output.name + '.gone' if case == 'missing file' else output.name,
                                         _instagram_hdr_hevc=cls is InstagramHDRHEVCPP)
                        if case == 'no dynamic_range':
                            del info['dynamic_range']
                        self.assertEqual(postprocessor.run(info), ([], info))

    def test_multi_video_sdr_selection_skips_without_warning(self):
        videos = [{'format_id': 'a', 'vcodec': 'vp9', 'dynamic_range': 'SDR'},
                  {'format_id': 'b', 'vcodec': 'avc1', 'dynamic_range': 'SDR'}]
        with tempfile.NamedTemporaryFile(suffix='.mkv') as output:
            converter, warnings = InstagramHDRHEVCPP(), []
            converter.report_warning = warnings.append
            converter._verify = mock.Mock(side_effect=AssertionError('must not probe'))
            info = {'_instagram_hdr': True, '_instagram_hdr_hevc': True, 'ext': 'mkv',
                    'filepath': output.name, 'requested_formats': videos}
            self.assertEqual(converter.run(info), ([], info))
            self.assertEqual(warnings, [])

    def test_hdr_verifier_skips_audio_only_downloads(self):
        verifier = InstagramHDRVerifyPP()
        verifier.get_metadata_object = lambda *_args, **_kwargs: self.fail(
            'Audio-only downloads must not be HDR-verified')

        deleted, info = verifier.run({
            '_instagram_hdr': True,
            'vcodec': 'none',
        })
        self.assertEqual(deleted, [])
        self.assertEqual(info['vcodec'], 'none')


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'needs ffmpeg and ffprobe')
class RealFFmpegTests(unittest.TestCase):
    def test_real_ffmpeg_converts_a_vp9_hlg_clip_and_copies_audio(self):
        with tempfile.TemporaryDirectory() as directory, \
                YoutubeDL({'quiet': True, 'no_warnings': True}, auto_init=False) as downloader:
            source = Path(directory, 'video.webm')
            made = subprocess.run([
                'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=128x128:rate=10:duration=0.5',
                '-f', 'lavfi', '-i', 'sine=duration=0.5', '-vf',
                'setparams=color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc,format=yuv420p10le',
                '-c:v', 'libvpx-vp9', '-profile:v', '2', '-c:a', 'libopus', str(source)])
            if made.returncode:
                self.skipTest('this ffmpeg cannot make a VP9 Profile 2 clip')
            converter = InstagramHDRHEVCPP(downloader)
            converter.to_screen = lambda *_args, **_kwargs: None
            deleted, info = converter.run(hevc_info(source))
            self.assertEqual(deleted, [str(source)])
            probe = json.loads(subprocess.run(
                ['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', info['filepath']],
                capture_output=True, text=True, check=True).stdout)
            self.assertEqual([(s['codec_type'], s['codec_name']) for s in probe['streams']],
                             [('video', 'hevc'), ('audio', 'opus')])
            self.assertEqual((probe['streams'][0]['profile'], probe['streams'][0]['codec_tag_string']),
                             ('Main 10', 'hvc1'))


if __name__ == '__main__':
    unittest.main()
