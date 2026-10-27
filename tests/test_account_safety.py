import copy
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from yt_dlp import YoutubeDL
from yt_dlp.extractor.instagram import InstagramBaseIE, InstagramIE
from yt_dlp.globals import plugin_dirs
from yt_dlp.utils import ExtractorError


plugin_dirs.value = [str(Path(__file__).resolve().parents[2])]
from yt_dlp_plugins.extractor import instagram_hdr as plugin


class HTTPFailure(Exception):
    def __init__(self, status, url=None):
        super().__init__(f'HTTP {status}')
        self.status = status
        self.response = SimpleNamespace(url=url)


def media_manifest():
    return '''<MPD><Period>
      <AdaptationSet contentType="video" mimeType="video/mp4">
        <Representation id="hdr" codecs="vp09.02.40.10.01.09.18.09.00"
            bandwidth="4000000" width="1080" height="1920">
          <BaseURL>https://cdn.example/hdr.mp4</BaseURL>
        </Representation>
        <Representation id="sdr" codecs="avc1.640028" bandwidth="2000000">
          <BaseURL>https://cdn.example/sdr.mp4</BaseURL>
        </Representation>
      </AdaptationSet>
      <AdaptationSet contentType="audio" mimeType="audio/mp4">
        <Representation id="aac" codecs="mp4a.40.2" bandwidth="128000">
          <BaseURL>https://cdn.example/aac.mp4</BaseURL>
        </Representation>
        <Representation id="xhe" codecs="mp4a.40.42" bandwidth="64000">
          <BaseURL>https://cdn.example/xhe.mp4</BaseURL>
        </Representation>
      </AdaptationSet>
    </Period></MPD>'''


class AccountSafetyTests(unittest.TestCase):
    def make_extractor(self, cls=plugin.InstagramHDRIE, downloader=None, **params):
        if downloader is None:
            downloader = YoutubeDL({
                'quiet': True, 'no_warnings': True,
                'cookiefile': None, 'cookiesfrombrowser': None, **params,
            }, auto_init=False)
            self.addCleanup(downloader.close)
        extractor = cls(downloader)
        extractor.__dict__['_can_impersonate'] = False
        extractor._get_cookies = mock.Mock(return_value={
            'sessionid': SimpleNamespace(value='offline-session'),
            'csrftoken': SimpleNamespace(value='offline-csrf'),
            'ds_user_id': SimpleNamespace(value='42'),
        })
        extractor.to_screen = mock.Mock()
        extractor.report_warning = mock.Mock()
        return extractor

    def test_restricted_http_errors_stop_even_nonfatal_requests(self):
        for status in (401, 403, 404, 429):
            for fatal in (True, False):
                with self.subTest(status=status, fatal=fatal):
                    extractor = self.make_extractor()
                    failure = ExtractorError('restricted', cause=HTTPFailure(status))
                    with mock.patch.object(InstagramBaseIE, '_request_webpage', side_effect=failure) as request:
                        with self.assertRaisesRegex(plugin._InstagramSessionError, f'HTTP {status}') as caught:
                            extractor._request_webpage('https://www.instagram.com/api/v1/media/1/info/', '1',
                                                       fatal=fatal, errnote=False)
                        self.assertTrue(caught.exception.expected)
                        self.assertIs(request.call_args.kwargs['fatal'], True)
                        with self.assertRaises(plugin._InstagramSessionError):
                            extractor._request_webpage('https://www.instagram.com/api/v1/media/2/info/', '2')
                        self.assertEqual(request.call_count, 1)

    def test_non_auth_http_failure_keeps_nonfatal_semantics(self):
        extractor = self.make_extractor()
        with mock.patch.object(InstagramBaseIE, '_request_webpage', side_effect=ExtractorError(
                'temporary server error', cause=HTTPFailure(500))):
            self.assertIs(extractor._request_webpage('https://www.instagram.com/', '1', fatal=False), False)
        extractor.report_warning.assert_called_once()
        extractor._check_access()

    def test_accepted_error_response_status_still_stops_access(self):
        for status in (401, 403, 404, 429):
            with self.subTest(status=status):
                extractor = self.make_extractor()
                response = SimpleNamespace(status=status, url='https://www.instagram.com/api/v1/media/1/info/')
                with mock.patch.object(InstagramBaseIE, '_request_webpage', return_value=response) as request:
                    with self.assertRaisesRegex(plugin._InstagramSessionError, f'HTTP {status}'):
                        extractor._request_webpage(response.url, '1', fatal=False, expected_status=status)
                    self.assertEqual(request.call_args.kwargs['expected_status'], status)
                    with self.assertRaises(plugin._InstagramSessionError):
                        extractor._request_webpage(response.url, '1')
                    self.assertEqual(request.call_count, 1)

    def test_login_verification_and_suspension_redirects_stop_access(self):
        for path in ('accounts/login/?next=/reel/ABC/', 'challenge/123/', 'checkpoint/',
                     'accounts/suspended/', 'accounts/disabled/', 'consent/'):
            with self.subTest(path=path):
                extractor = self.make_extractor()
                with mock.patch.object(InstagramBaseIE, '_request_webpage', return_value=SimpleNamespace(
                        url=f'https://www.instagram.com/{path}')):
                    with self.assertRaisesRegex(plugin._InstagramSessionError, 'login or account verification'):
                        extractor._request_webpage('https://www.instagram.com/reel/ABC/', 'ABC', fatal=False)

    def test_stop_flag_is_shared_by_reels_and_stories(self):
        post = self.make_extractor()
        story = self.make_extractor(plugin.InstagramHDRStoryIE, downloader=post._downloader)
        with mock.patch.object(InstagramBaseIE, '_request_webpage', side_effect=ExtractorError(
                'limited', cause=HTTPFailure(429))) as request:
            with self.assertRaises(plugin._InstagramSessionError):
                post._request_webpage('https://www.instagram.com/reel/ABC/', 'ABC')
            with self.assertRaises(plugin._InstagramSessionError):
                story._request_webpage('https://www.instagram.com/stories/example/', 'example')
            self.assertEqual(request.call_count, 1)
        self.make_extractor()._check_access()

    def test_default_request_spacing_is_two_seconds_and_shared(self):
        post = self.make_extractor()
        story = self.make_extractor(plugin.InstagramHDRStoryIE, downloader=post._downloader)
        with mock.patch.object(InstagramBaseIE, '_request_webpage', return_value=SimpleNamespace(
                url='https://www.instagram.com/')) as request, \
                mock.patch.object(plugin.time, 'monotonic', side_effect=[10.0, 10.25, 12.0]), \
                mock.patch.object(plugin.time, 'sleep') as sleep:
            post._request_webpage('https://www.instagram.com/reel/ABC/', 'ABC')
            story._request_webpage('https://www.instagram.com/stories/example/', 'example')
        sleep.assert_called_once_with(1.75)
        self.assertEqual(request.call_count, 2)
        self.assertNotIn('sleep_interval_requests', post._downloader.params)

    def test_explicit_request_sleep_is_left_to_yt_dlp(self):
        for interval in (0, 1, 5):
            with self.subTest(interval=interval):
                extractor = self.make_extractor(sleep_interval_requests=interval)
                with mock.patch.object(InstagramBaseIE, '_request_webpage', return_value=False), \
                        mock.patch.object(plugin.time, 'sleep') as sleep:
                    extractor._request_webpage('https://www.instagram.com/', '1')
                    extractor._request_webpage('https://www.instagram.com/', '2')
                sleep.assert_not_called()
                self.assertEqual(extractor.get_param('sleep_interval_requests'), interval)

    def test_http_200_restriction_json_stops_and_prevents_followup(self):
        responses = [
            {'challenge': {'url': '/challenge/123/'}},
            {'challenge_required': True},
            {'checkpoint_url': '/checkpoint/'},
            {'feedback_required': True},
            {'spam': True},
            {'message': 'challenge_required'},
            {'message': 'login_required'},
            {'message': 'Please wait a few minutes before you try again.'},
            {'error_type': 'sentry_block'},
            {'message': 'Your account has been suspended'},
            {'status': 'fail', 'message': 'Another unrecognized rejection'},
        ]
        for response in responses:
            with self.subTest(response=response):
                extractor = self.make_extractor()
                cookies = extractor._get_cookies.return_value.copy()
                with mock.patch.object(InstagramBaseIE, '_download_json', return_value=response) as download:
                    with self.assertRaises(plugin._InstagramSessionError):
                        extractor._download_json('https://www.instagram.com/api/v1/media/1/info/', '1', fatal=False)
                    with self.assertRaises(plugin._InstagramSessionError):
                        extractor._download_json('https://www.instagram.com/api/v1/media/2/info/', '2')
                    self.assertEqual(download.call_count, 1)
                self.assertEqual(extractor._get_cookies.return_value, cookies)

    def test_normal_json_does_not_set_stop_flag(self):
        extractor = self.make_extractor()
        response = {'status': 'ok', 'items': [{'pk': '123'}]}
        with mock.patch.object(InstagramBaseIE, '_download_json', return_value=response):
            self.assertIs(extractor._download_json('https://www.instagram.com/api/v1/media/1/info/', '1'), response)
        extractor._check_access()

    def test_web_headers_default_without_mobile_device_or_telemetry(self):
        extractor = self.make_extractor()
        self.assertTrue(extractor._is_web_app)
        self.assertEqual(extractor._api_headers['X-IG-App-ID'], InstagramBaseIE._APP_IDS['web'])
        self.assertEqual(extractor._api_headers['X-CSRFToken'], 'offline-csrf')
        self.assertNotIn('User-Agent', extractor._api_headers)
        for name in extractor._api_headers:
            self.assertFalse(any(marker in name.lower() for marker in ('device', 'bandwidth', 'pigeon', 'session')))

    def test_ios_api_is_explicit_and_retains_csrf_without_fabricated_device(self):
        extractor = self.make_extractor(extractor_args={'instagramhdr': {'api': ['ios']}})
        self.assertFalse(extractor._is_web_app)
        headers = extractor._api_headers
        self.assertEqual(headers['X-IG-App-ID'], InstagramBaseIE._APP_IDS['ios'])
        self.assertEqual(headers['User-Agent'], plugin._IOS_USER_AGENT)
        self.assertEqual(headers['X-CSRFToken'], 'offline-csrf')
        self.assertFalse(any('device' in name.lower() or 'bandwidth' in name.lower() for name in headers))

    def test_authenticated_errors_never_trigger_native_fallback_or_delete_cookies(self):
        failures = [
            ExtractorError('media unavailable'),
            ExtractorError('rejected', cause=HTTPFailure(403)),
            ValueError('unexpected payload'),
        ]
        for cls in (plugin.InstagramHDRIE, plugin.InstagramHDRStoryIE):
            for failure in failures:
                with self.subTest(extractor=cls.__name__, error=str(failure)):
                    extractor = self.make_extractor(cls)
                    cookies = extractor._get_cookies.return_value.copy()
                    extractor._extract_api = mock.Mock(side_effect=failure)
                    extractor._set_cookie = mock.Mock()
                    extractor._native_url_result = mock.Mock()
                    with self.assertRaises(ExtractorError):
                        extractor._real_extract('https://www.instagram.com/reel/ABC/')
                    extractor._native_url_result.assert_not_called()
                    extractor._set_cookie.assert_not_called()
                    self.assertEqual(extractor._get_cookies.return_value, cookies)

    def test_one_media_response_keeps_hdr_sdr_and_aac(self):
        extractor = self.make_extractor()
        item = {
            'pk': '123', 'media_type': 2, 'video_dash_manifest': media_manifest(),
            'video_versions': [{'id': 'progressive', 'url': 'https://cdn.example/progressive.mp4'}],
            'user': {'pk': '42', 'username': 'example'},
        }
        original = copy.deepcopy(item)
        with mock.patch.object(InstagramBaseIE, '_download_json', return_value={'items': [item]}) as download, \
                mock.patch.object(InstagramIE, '_real_extract', side_effect=AssertionError('native pass repeated')):
            result = extractor._real_extract('https://www.instagram.com/reel/ABC/')
        self.assertEqual(download.call_count, 1)
        self.assertTrue(download.call_args.args[0].endswith('/media/66/info/'))
        self.assertFalse(download.call_args.kwargs['impersonate'])
        self.assertTrue(result['_instagram_hdr'])
        self.assertEqual({fmt['url'] for fmt in result['formats']}, {
            'https://cdn.example/hdr.mp4', 'https://cdn.example/sdr.mp4',
            'https://cdn.example/aac.mp4', 'https://cdn.example/progressive.mp4',
        })
        self.assertEqual({fmt.get('dynamic_range') for fmt in result['formats']}, {'HLG', 'SDR', None})
        self.assertIn('mp4a.40.2', [fmt.get('acodec') for fmt in result['formats']])
        self.assertEqual(item, original)

    def test_default_auto_finds_ios_hdr_and_selects_web_audio_with_bv_plus_ba(self):
        extractor = self.make_extractor(format='bv+ba', simulate=True, check_formats=False)
        web = {
            'pk': '123', 'media_type': 2,
            'video_dash_manifest': media_manifest().replace(
                'vp09.02.40.10.01.09.18.09.00', 'avc1.640028').replace(
                    'https://cdn.example/hdr.mp4', 'https://cdn.example/web.mp4'),
            'video_versions': [{'id': 'progressive', 'url': 'https://cdn.example/progressive.mp4'}],
        }
        ios = {**web, 'video_dash_manifest': media_manifest().replace(
            '<Representation id="aac" codecs="mp4a.40.2" bandwidth="128000">'
            '\n          <BaseURL>https://cdn.example/aac.mp4</BaseURL>'
            '\n        </Representation>', '')}
        original = copy.deepcopy((web, ios))
        with mock.patch.object(InstagramBaseIE, '_download_json', side_effect=[
                {'items': [web]}, {'items': [ios]}]) as download:
            result = extractor._real_extract('https://www.instagram.com/p/De7p7t9Rfaw/')
        self.assertEqual(download.call_count, 2)
        first, second = download.call_args_list
        self.assertIn('https://www.instagram.com/', first.args[0])
        self.assertIn('https://i.instagram.com/', second.args[0])
        self.assertEqual(first.kwargs['headers']['X-IG-App-ID'], extractor._APP_IDS['web'])
        self.assertEqual(second.kwargs['headers']['X-IG-App-ID'], extractor._APP_IDS['ios'])
        self.assertEqual(second.kwargs['headers']['User-Agent'], plugin._IOS_USER_AGENT)
        self.assertEqual(second.kwargs['headers']['X-CSRFToken'], 'offline-csrf')
        self.assertFalse(second.kwargs['impersonate'])
        self.assertTrue(result['_instagram_hdr'])
        self.assertEqual(len({fmt['url'] for fmt in result['formats']}), len(result['formats']))
        self.assertFalse(any(plugin._is_xhe_aac(fmt) for fmt in result['formats']))
        selected = extractor._downloader.process_ie_result({**result, 'title': 'Video'}, download=False)
        self.assertEqual([fmt['url'] for fmt in selected['requested_formats']], [
            'https://cdn.example/hdr.mp4', 'https://cdn.example/aac.mp4'])
        self.assertEqual(selected['dynamic_range'], 'HLG')
        self.assertEqual((web, ios), original)

    def test_explicit_web_does_not_request_ios_for_sdr(self):
        extractor = self.make_extractor(extractor_args={'instagramhdr': {'api': ['web']}})
        item = {'pk': '123', 'video_versions': [{'url': 'https://cdn.example/sdr.mp4'}]}
        with mock.patch.object(InstagramBaseIE, '_download_json', return_value={'items': [item]}) as download:
            result = extractor._real_extract('https://www.instagram.com/reel/ABC/')
        download.assert_called_once()
        self.assertNotIn('_instagram_hdr', result)
        self.assertEqual(result['formats'][0]['url'], 'https://cdn.example/sdr.mp4')

    def test_auto_fills_missing_audio_and_ranks_the_combined_hdr_ladder(self):
        extractor = self.make_extractor(format='bv+ba', simulate=True, check_formats=False)
        web = {'pk': '123', 'video_dash_manifest': media_manifest().split(
            '<AdaptationSet contentType="audio"')[0] + '</Period></MPD>'}
        ios = {'pk': '123', 'video_dash_manifest': media_manifest().replace(
            'width="1080" height="1920"', 'width="720" height="1280"').replace(
                'https://cdn.example/hdr.mp4', 'https://cdn.example/ios-low.mp4').replace(
                    'id="sdr" codecs="avc1.640028" bandwidth="2000000"',
                    'id="mid-hdr" codecs="vp09.02.40.10.01.09.18.09.00" '
                    'bandwidth="3000000" width="900" height="1600"')}
        with mock.patch.object(InstagramBaseIE, '_download_json', side_effect=[
                {'items': [web]}, {'items': [ios]}]) as download:
            result = extractor._real_extract('https://www.instagram.com/reel/ABC/')
        self.assertEqual(download.call_count, 2)
        selected = extractor._downloader.process_ie_result({**result, 'title': 'Video'}, download=False)
        self.assertEqual([fmt['url'] for fmt in selected['requested_formats']], [
            'https://cdn.example/hdr.mp4', 'https://cdn.example/aac.mp4'])
        self.assertEqual(selected['height'], 1920)

    def test_ios_supplements_compatible_audio_from_web(self):
        extractor = self.make_extractor(
            format='bv+ba', simulate=True, check_formats=False,
            extractor_args={'instagramhdr': {'api': ['ios']}})
        ios = {'pk': '123', 'video_dash_manifest': media_manifest().replace(
            'mp4a.40.2', 'mp4a.40.42').replace(
                'https://cdn.example/aac.mp4', 'https://cdn.example/ios-xhe.mp4')}
        web = {'pk': '123', 'video_dash_manifest': media_manifest().replace(
            'vp09.02.40.10.01.09.18.09.00', 'avc1.640028').replace(
                'https://cdn.example/hdr.mp4', 'https://cdn.example/web.mp4')}
        with mock.patch.object(InstagramBaseIE, '_download_json', side_effect=[
                {'items': [ios]}, {'items': [web]}]) as download:
            result = extractor._real_extract('https://www.instagram.com/reel/ABC/')
        self.assertEqual(download.call_count, 2)
        self.assertIn('https://i.instagram.com/', download.call_args_list[0].args[0])
        self.assertIn('https://www.instagram.com/', download.call_args_list[1].args[0])
        self.assertNotIn('User-Agent', download.call_args_list[1].kwargs['headers'])
        selected = extractor._downloader.process_ie_result({**result, 'title': 'Video'}, download=False)
        self.assertEqual([fmt['acodec'] for fmt in selected['requested_formats']], ['none', 'mp4a.40.2'])

    def test_xhe_audio_survives_progressive_video_when_it_is_the_only_separate_audio(self):
        extractor = self.make_extractor(format='bv+ba', simulate=True, check_formats=False)
        item = {
            'pk': '123', 'video_dash_manifest': media_manifest().replace('mp4a.40.2', 'mp4a.40.42'),
            'video_versions': [{'url': 'https://cdn.example/progressive.mp4'}],
        }
        result = extractor._extract_product(item)
        selected = extractor._downloader.process_ie_result({**result, 'title': 'Video'}, download=False)
        self.assertEqual([fmt['acodec'] for fmt in selected['requested_formats']], ['none', 'mp4a.40.42'])

    def test_auto_supplement_never_borrows_formats_from_other_carousel_items(self):
        extractor = self.make_extractor()
        web = {'pk': '1', 'carousel_media': [
            {'pk': '123', 'video_versions': [{'url': 'https://cdn.example/123.mp4'}]},
            {'pk': '456', 'video_versions': [{'url': 'https://cdn.example/456.mp4'}]},
        ]}
        ios = {'pk': '1', 'carousel_media': [
            {'pk': '789', 'video_dash_manifest': media_manifest()},
            {'pk': '456', 'video_dash_manifest': media_manifest()},
        ]}
        with mock.patch.object(InstagramBaseIE, '_download_json', side_effect=[
                {'items': [web]}, {'items': [ios]}]):
            result = extractor._real_extract('https://www.instagram.com/p/ABC/')
        first, second = result['entries']
        self.assertEqual([entry['id'] for entry in result['entries']], [plugin._pk_to_id('123'), plugin._pk_to_id('456')])
        self.assertEqual(first['formats'], [{'url': 'https://cdn.example/123.mp4'}])
        self.assertNotIn('_instagram_hdr', first)
        self.assertTrue(second['_instagram_hdr'])
        self.assertEqual({fmt['url'] for fmt in second['formats']}, {
            'https://cdn.example/456.mp4', 'https://cdn.example/hdr.mp4',
            'https://cdn.example/sdr.mp4', 'https://cdn.example/aac.mp4'})

    def test_restricted_auto_supplement_stops_all_followup_requests(self):
        for status in (401, 403, 404, 429):
            with self.subTest(status=status):
                extractor = self.make_extractor()
                web = {'pk': '123', 'video_versions': [{'url': 'https://cdn.example/sdr.mp4'}]}
                with mock.patch.object(InstagramBaseIE, '_download_json', side_effect=[
                        {'items': [web]}, ExtractorError('restricted', cause=HTTPFailure(status))]) as download:
                    with self.assertRaises(plugin._InstagramSessionError):
                        extractor._real_extract('https://www.instagram.com/reel/ABC/')
                    with self.assertRaises(plugin._InstagramSessionError):
                        extractor._real_extract('https://www.instagram.com/reel/DEF/')
                self.assertEqual(download.call_count, 2)

    def test_empty_supplement_keeps_successful_web_formats(self):
        extractor = self.make_extractor()
        web = {'pk': '123', 'video_versions': [{'url': 'https://cdn.example/sdr.mp4'}]}
        with mock.patch.object(InstagramBaseIE, '_download_json', side_effect=[
                {'items': [web]}, None]) as download:
            result = extractor._real_extract('https://www.instagram.com/reel/ABC/')
        self.assertEqual(download.call_count, 2)
        self.assertEqual(result['formats'], [{'url': 'https://cdn.example/sdr.mp4'}])

    def test_story_with_manifest_skips_detail_request(self):
        extractor = self.make_extractor(plugin.InstagramHDRStoryIE)
        with mock.patch.object(InstagramBaseIE, '_download_json', side_effect=AssertionError('unnecessary detail lookup')):
            result = extractor._extract_product_media({
                'pk': '123', 'media_type': 2, 'video_dash_manifest': media_manifest(),
            })
        self.assertTrue(result['_instagram_hdr'])

    def test_story_sdr_tray_manifest_is_enriched_with_hdr_and_audio(self):
        extractor = self.make_extractor(plugin.InstagramHDRStoryIE)
        tray = {
            'pk': '123', 'media_type': 2,
            'video_dash_manifest': media_manifest().replace('vp09.02.40.10.01.09.18.09.00', 'avc1.640028'),
            'video_versions': [{'id': 'progressive', 'url': 'https://cdn.example/progressive.mp4'}],
        }
        detail = {'items': [{'pk': '123', 'video_dash_manifest': media_manifest()}]}
        original_tray, original_detail = copy.deepcopy(tray), copy.deepcopy(detail)
        with mock.patch.object(InstagramBaseIE, '_download_json', return_value=detail) as download:
            result = extractor._extract_product_media(tray)
        self.assertEqual(download.call_count, 1)
        self.assertTrue(download.call_args.args[0].endswith('/media/123/info/'))
        self.assertTrue(result['_instagram_hdr'])
        self.assertEqual({fmt['url'] for fmt in result['formats']}, {
            'https://cdn.example/hdr.mp4', 'https://cdn.example/sdr.mp4',
            'https://cdn.example/aac.mp4', 'https://cdn.example/progressive.mp4',
        })
        self.assertIn('mp4a.40.2', [fmt.get('acodec') for fmt in result['formats']])
        self.assertEqual(tray, original_tray)
        self.assertEqual(detail, original_detail)

    def test_story_auto_finds_ios_hdr_without_losing_web_tray_audio(self):
        extractor = self.make_extractor(
            plugin.InstagramHDRStoryIE, format='bv+ba', simulate=True, check_formats=False)
        tray = {'pk': '123', 'media_type': 2, 'video_dash_manifest': media_manifest().replace(
            'vp09.02.40.10.01.09.18.09.00', 'avc1.640028').replace(
                'https://cdn.example/hdr.mp4', 'https://cdn.example/web.mp4')}
        web_detail = {'pk': '123', 'media_type': 2,
                      'video_versions': [{'url': 'https://cdn.example/progressive.mp4'}]}
        ios_detail = {'pk': '123', 'video_dash_manifest': media_manifest().replace(
            'mp4a.40.2', 'mp4a.40.42').replace(
                'https://cdn.example/aac.mp4', 'https://cdn.example/ios-xhe.mp4')}
        with mock.patch.object(InstagramBaseIE, '_download_json', side_effect=[
                {'items': [web_detail]}, {'items': [ios_detail]}]) as download:
            result = extractor._extract_product_media(tray)
        self.assertEqual(download.call_count, 2)
        self.assertTrue(result['_instagram_hdr'])
        selected = extractor._downloader.process_ie_result({**result, 'title': 'Story'}, download=False)
        self.assertEqual([fmt['url'] for fmt in selected['requested_formats']], [
            'https://cdn.example/hdr.mp4', 'https://cdn.example/aac.mp4'])

    def test_generated_format_ids_do_not_collide_with_original_ids(self):
        formats = [
            {'format_id': 'hdr-video', 'url': 'https://cdn.example/one.mp4'},
            {'format_id': 'hdr-video', 'url': 'https://cdn.example/two.mp4'},
            {'format_id': 'hdr-video-0', 'url': 'https://cdn.example/three.mp4'},
        ]
        plugin._disambiguate_format_ids(formats)
        ids = [fmt['format_id'] for fmt in formats]
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(ids[-1], 'hdr-video-0')
        self.assertEqual([fmt['url'] for fmt in formats], [
            'https://cdn.example/one.mp4', 'https://cdn.example/two.mp4', 'https://cdn.example/three.mp4',
        ])

    def test_no_playlist_selects_requested_story_before_detail_lookups(self):
        extractor = self.make_extractor(plugin.InstagramHDRStoryIE, noplaylist=True)
        extractor._download_webpage = mock.Mock(return_value='"user":{"pk":"42","username":"example"}')
        tray = {'reels': {'42': {'user': {'pk': '42', 'username': 'example'}, 'items': [
            {'pk': '122', 'media_type': 2, 'video_versions': [{'url': 'https://cdn.example/other.mp4'}]},
            {'pk': '123', 'media_type': 2, 'video_versions': [{'url': 'https://cdn.example/requested.mp4'}]},
        ]}}}
        detail = {'items': [{'pk': '123', 'media_type': 2, 'video_dash_manifest': media_manifest()}]}
        with mock.patch.object(InstagramBaseIE, '_download_json', side_effect=[tray, detail]) as download:
            result = extractor._real_extract('https://www.instagram.com/stories/example/123/')
        self.assertTrue(result['_instagram_hdr'])
        self.assertEqual(download.call_count, 2)
        self.assertTrue(download.call_args_list[0].args[0].endswith('/feed/reels_media/'))
        self.assertTrue(download.call_args_list[1].args[0].endswith('/media/123/info/'))
        self.assertEqual(download.call_args_list[0].kwargs['query'], {'reel_ids': '42'})


if __name__ == '__main__':
    unittest.main()
