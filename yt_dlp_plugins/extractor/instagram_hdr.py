from __future__ import annotations

import html
import random
import re
import time
import uuid
from datetime import datetime, timedelta
from fractions import Fraction
from typing import Any

from yt_dlp.extractor.instagram import InstagramBaseIE, InstagramIE, InstagramStoryIE
from yt_dlp.utils import (
    ExtractorError,
    encode_base_n,
    float_or_none,
    int_or_none,
    parse_codecs,
    traverse_obj,
    url_or_none,
)
from yt_dlp_plugins.postprocessor.instagram_hdr import InstagramHDRVerifyPP


_SHORTCODE_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
_IOS_USER_AGENT = (
    'Instagram 361.0.0.35.82 (iPhone16,1; iOS 18_0; en_US; en-US; '
    'scale=2.00; 2048x2732; 674117118) AppleWebKit/420+'
)

_REPRESENTATION_RE = re.compile(
    r'<Representation\b(?P<attrs>[^>]*)>(?P<body>.*?)</Representation\s*>',
    re.IGNORECASE | re.DOTALL,
)
_ADAPTATION_RE = re.compile(
    r'<AdaptationSet\b(?P<attrs>[^>]*)>(?P<body>.*?)</AdaptationSet\s*>',
    re.IGNORECASE | re.DOTALL,
)
_BASE_URL_RE = re.compile(
    r'<BaseURL\b[^>]*>(?P<url>.*?)</BaseURL\s*>',
    re.IGNORECASE | re.DOTALL,
)
_ATTRIBUTE_RE = re.compile(
    r'([A-Za-z_][\w:.-]*)\s*=\s*(["\'])(.*?)\2',
    re.DOTALL,
)
_PROPERTY_RE = re.compile(
    r'<(?:SupplementalProperty|EssentialProperty)\b(?P<attrs>[^>]*)/?>',
    re.IGNORECASE | re.DOTALL,
)


def _parse_attributes(text: str) -> dict[str, str]:
    return {
        key: html.unescape(value)
        for key, _quote, value in _ATTRIBUTE_RE.findall(text)
    }


def _int_value(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _frame_rate(value: str | None) -> float:
    try:
        return float(Fraction(value or '0'))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _codec_details(codec: str) -> dict[str, Any]:
    parts = codec.lower().split('.')
    details = {
        'family': None,
        'profile': None,
        'bit_depth': None,
        'primaries': None,
        'transfer': None,
    }
    try:
        if parts[0] == 'vp09':
            details.update(
                family='vp9',
                profile=int(parts[1]),
                bit_depth=int(parts[3]),
                primaries=int(parts[5]) if len(parts) > 5 else None,
                transfer=int(parts[6]) if len(parts) > 6 else None,
            )
        elif parts[0] == 'av01':
            details.update(
                family='av1',
                profile=int(parts[1]),
                bit_depth=int(parts[3]),
                primaries=int(parts[6]) if len(parts) > 6 else None,
                transfer=int(parts[7]) if len(parts) > 7 else None,
            )
    except (IndexError, ValueError):
        pass
    return details


def _cicp_details(text: str) -> dict[str, int]:
    details = {}
    for match in _PROPERTY_RE.finditer(text):
        attrs = _parse_attributes(match.group('attrs'))
        scheme = attrs.get('schemeIdUri', '').lower()
        value = _int_value(attrs.get('value'))
        if 'colourprimaries' in scheme or 'colorprimaries' in scheme:
            details['primaries'] = value
        elif 'transfercharacteristics' in scheme:
            details['transfer'] = value
    return details


def _hdr_kind(attrs: dict[str, str], context: str) -> str | None:
    codec = _codec_details(attrs.get('codecs', ''))
    if codec['family'] not in {'vp9', 'av1'} or codec['bit_depth'] != 10:
        return None
    if codec['family'] == 'vp9' and codec['profile'] != 2:
        return None

    cicp = _cicp_details(context)
    primaries = codec['primaries'] or cicp.get('primaries')
    transfer = codec['transfer'] or cicp.get('transfer')
    tag = ' '.join((attrs.get('FBEncodingTag', ''), attrs.get('id', ''), context)).lower()

    if transfer == 18 or 'hlg' in tag or 'arib-std-b67' in tag:
        transfer_kind = 'HLG'
    elif transfer == 16 or 'pq' in tag or 'smpte2084' in tag:
        transfer_kind = 'HDR10'
    elif 'hdr' in tag:
        transfer_kind = 'HDR'
    else:
        return None

    if primaries not in {None, 9}:
        return None
    if primaries is None and not re.search(r'hdr|hlg|pq', tag, re.IGNORECASE):
        return None
    return transfer_kind


def _decode_base_url(value: str) -> str:
    # Decode only XML ampersands. Parsing remains tolerant of an already
    # unescaped '&', which ElementTree would reject as malformed XML.
    return (
        value.strip()
        .replace('&amp;', '&')
        .replace('&#38;', '&')
        .replace('&#x26;', '&')
        .replace('&#X26;', '&')
    )


def _parse_manifest(manifest: str) -> list[dict[str, Any]]:
    tracks = []
    adaptations = list(_ADAPTATION_RE.finditer(manifest))
    blocks = [
        (_parse_attributes(match.group('attrs')), match.group('body'))
        for match in adaptations
    ] or [({}, manifest)]

    for adaptation_attrs, body in blocks:
        adaptation_context = _REPRESENTATION_RE.sub('', body)
        for match in _REPRESENTATION_RE.finditer(body):
            attrs = {**adaptation_attrs, **_parse_attributes(match.group('attrs'))}
            base_url = _BASE_URL_RE.search(match.group('body'))
            if not base_url:
                continue

            mime_type = attrs.get('mimeType', '').lower()
            content_type = attrs.get('contentType', '').lower()
            if mime_type.startswith('video/') or content_type == 'video':
                kind = 'video'
            elif mime_type.startswith('audio/') or content_type == 'audio':
                kind = 'audio'
            else:
                continue

            rep_context = f'{adaptation_context} {match.group("attrs")}'
            tracks.append({
                **attrs,
                'kind': kind,
                'url': _decode_base_url(base_url.group('url')),
                'hdr_kind': _hdr_kind(attrs, rep_context) if kind == 'video' else None,
                'width_int': _int_value(attrs.get('width')),
                'height_int': _int_value(attrs.get('height')),
                'bandwidth_int': _int_value(attrs.get('bandwidth')),
                'frame_rate_float': _frame_rate(attrs.get('frameRate')),
            })
    return tracks


def _collect_manifests(data: Any) -> list[str]:
    manifests = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                if isinstance(child, str) and re.search(r'<MPD\b', child, re.IGNORECASE):
                    manifests.append(child)
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)
    return list(dict.fromkeys(manifests))


def _format_id(prefix: str, track: dict[str, Any]) -> str:
    identifier = track.get('FBEncodingTag') or track.get('id') or str(track['bandwidth_int'])
    return f'{prefix}-{identifier}'


def _video_format(track: dict[str, Any], quality: int) -> dict[str, Any]:
    codec_info = parse_codecs(track.get('codecs', ''))
    codec_info['dynamic_range'] = track['hdr_kind']
    return {
        'url': track['url'],
        'format_id': _format_id('hdr', track),
        'format_note': f'DASH video, {track["hdr_kind"]}',
        'ext': 'mp4',
        'width': track['width_int'] or None,
        'height': track['height_int'] or None,
        'fps': track['frame_rate_float'] or None,
        'vbr': float_or_none(track['bandwidth_int'], 1000),
        'tbr': float_or_none(track['bandwidth_int'], 1000),
        'filesize': int_or_none(track.get('FBContentLength')),
        'quality': quality,
        'http_headers': {
            'User-Agent': _IOS_USER_AGENT,
            'Referer': 'https://www.instagram.com/',
        },
        **codec_info,
    }


def _audio_format(track: dict[str, Any], quality: int) -> dict[str, Any]:
    bitrate = _int_value(track.get('FBAvgBitrate')) or track['bandwidth_int']
    return {
        'url': track['url'],
        'format_id': _format_id('audio', track),
        'format_note': 'DASH audio',
        'ext': 'mp4',
        'abr': float_or_none(bitrate, 1000),
        'tbr': float_or_none(track['bandwidth_int'], 1000),
        'asr': int_or_none(track.get('audioSamplingRate')),
        'filesize': int_or_none(track.get('FBContentLength')),
        'quality': quality,
        'http_headers': {
            'User-Agent': _IOS_USER_AGENT,
            'Referer': 'https://www.instagram.com/',
        },
        **parse_codecs(track.get('codecs', '')),
    }


class _InstagramHDRMixin:
    @property
    def _app_id(self):
        return self._APP_IDS['ios']

    @property
    def _api_headers(self):
        cookies = self._get_cookies('https://i.instagram.com/')

        def cookie(name):
            value = cookies.get(name)
            return value.value if value else None

        user_id = cookie('ds_user_id')
        device_id = cookie('ig_did')
        headers = {
            'User-Agent': _IOS_USER_AGENT,
            'Accept': '*/*',
            'x-ads-opt-out': '1',
            'x-bloks-is-panorama-enabled': 'true',
            'x-bloks-version-id': '16b7bd25c6c06886d57c4d455265669345a2d96625385b8ee30026ac2dc5ed97',
            'x-fb-client-ip': 'True',
            'x-fb-connection-type': 'wifi',
            'x-fb-http-engine': 'Liger',
            'x-fb-server-cluster': 'True',
            'x-fb': '1',
            'x-ig-abr-connection-speed-kbps': '2',
            'x-ig-app-id': self._APP_IDS['ios'],
            'x-ig-app-locale': 'en-US',
            'x-ig-app-startup-country': 'US',
            'x-ig-bandwidth-speed-kbps': '0.000',
            'x-ig-capabilities': '36r/F/8=',
            'x-ig-connection-speed': f'{random.randint(1000, 20000)}kbps',
            'x-ig-connection-type': 'WiFi',
            'x-ig-device-locale': 'en-US',
            'x-ig-mapped-locale': 'en-US',
            'x-ig-timezone-offset': str(
                (datetime.now().astimezone().utcoffset() or timedelta()).seconds),
            'x-ig-www-claim': '0',
            'x-pigeon-rawclienttime': f'{time.time():.6f}',
            'x-pigeon-session-id': str(uuid.uuid4()),
            'x-tigon-is-retry': 'False',
            'x-whatsapp': '0',
        }
        optional_headers = {
            'ig-intended-user-id': user_id,
            'x-mid': cookie('mid'),
            'ig-u-ds-user-id': user_id,
            'x-ig-device-id': device_id,
            'x-ig-family-device-id': device_id,
            'family_device_id': device_id,
            'X-CSRFToken': cookie('csrftoken'),
        }
        headers.update({key: value for key, value in optional_headers.items() if value})
        if rur := cookie('rur'):
            headers['ig-u-rur'] = rur.strip('"').encode().decode('unicode_escape')
        return headers

    @staticmethod
    def _is_auth_rejection(error: ExtractorError) -> bool:
        return getattr(error.cause, 'status', None) in {401, 403}

    def _verification_enabled(self):
        values = self._configuration_arg('verify', ['true'], ie_key=self.ie_key())
        return values[0] not in {'0', 'false', 'no', 'off'}

    def _real_initialize(self):
        cookies = self._get_cookies('https://i.instagram.com/')
        if not cookies.get('sessionid') or not cookies.get('ds_user_id'):
            self._fallback_to_native = True
            self.report_warning(
                'Authenticated Instagram cookies were not supplied; HDR formats are '
                'unavailable. Falling back to yt-dlp\'s built-in Instagram extractor. '
                'Pass --cookies-from-browser BROWSER or --cookies FILE to enable HDR.',
            )
            return
        if self._verification_enabled() and not getattr(
                self._downloader, '_instagram_hdr_verifier_registered', False):
            self._downloader.add_post_processor(
                InstagramHDRVerifyPP(self._downloader), when='after_move')
            self._downloader._instagram_hdr_verifier_registered = True
        return super()._real_initialize()

    def _real_extract(self, url):
        if getattr(self, '_fallback_to_native', False):
            match = self._match_valid_url(url)
            video_id = match.groupdict().get('id') or match.groupdict().get('user')
            return self.url_result(url, ie=self._NATIVE_IE, video_id=video_id)
        try:
            return super()._real_extract(url)
        except ExtractorError as error:
            if not self._is_auth_rejection(error):
                raise
        raise ExtractorError(
            'Instagram rejected the cookies supplied to yt-dlp (HTTP 401/403). '
            'Log into Instagram again and refresh --cookies-from-browser or --cookies.',
            expected=True,
        ) from error

    def _extract_product_media(self, product_media):
        tracks = []
        for manifest in _collect_manifests(product_media):
            tracks.extend(_parse_manifest(manifest))

        unique_tracks = list({
            (track['kind'], track['url']): track
            for track in tracks
        }.values())
        videos = sorted(
            (track for track in unique_tracks
             if track['kind'] == 'video' and track['hdr_kind']),
            key=lambda track: (
                track['width_int'] * track['height_int'],
                track['bandwidth_int'],
                track['frame_rate_float'],
            ),
        )
        audios = sorted(
            (track for track in unique_tracks if track['kind'] == 'audio'),
            key=lambda track: track['bandwidth_int'],
        )
        if not videos:
            self.to_screen(
                'No VP9/AV1 10-bit HDR representation found; using formats from '
                'yt-dlp\'s built-in Instagram extractor')
            return InstagramBaseIE._extract_product_media(self, product_media)
        if not audios:
            raise ExtractorError(
                'The HDR manifest contains no downloadable audio representation', expected=True)

        best_video = videos[-1]
        best_audio = audios[-1]
        self.to_screen(
            f'Selected HDR ladder: up to {best_video["width_int"]}x{best_video["height_int"]}, '
            f'{best_video["frame_rate_float"]:g} fps, {best_video["hdr_kind"]}; '
            f'best audio {best_audio["bandwidth_int"] / 1000:.0f} kb/s')

        media_id = traverse_obj(product_media, ('pk', {str}))
        if not media_id:
            media_id = str(product_media.get('pk') or '')
        shortcode = encode_base_n(
            int(media_id.split('_')[0]), table=_SHORTCODE_ALPHABET) if media_id else None

        return {
            'id': shortcode,
            'formats': [
                *(_video_format(track, quality) for quality, track in enumerate(videos)),
                *(_audio_format(track, quality) for quality, track in enumerate(audios)),
            ],
            'duration': traverse_obj(product_media, ('video_duration', {float_or_none})),
            'thumbnails': list(reversed(traverse_obj(product_media, (
                'image_versions2', 'candidates',
                lambda _, candidate: url_or_none(candidate['url']), {
                    'url': ('url', {url_or_none}),
                    'width': ('width', {int_or_none}),
                    'height': ('height', {int_or_none}),
                },
            )))),
            '_format_sort_fields': ('quality', 'res', 'br', 'fps'),
            '__instagram_hdr': True,
        }


class InstagramHDRIE(_InstagramHDRMixin, InstagramIE):
    IE_NAME = 'instagram:hdr'
    IE_DESC = 'Instagram videos and Reels HDR (authenticated iOS API)'
    _NATIVE_IE = InstagramIE
    _VALID_URL = (
        r'(?P<url>https?://(?:www\.)?instagram\.com'
        r'(?:/(?!share/)[^/?#]+)?/(?:p|reels?)/(?P<id>[^/?#&]+))'
    )
    _TESTS = [{
        'url': 'https://www.instagram.com/reel/Dcq5912OAjj/?igsh=example&foo=bar',
        'only_matching': True,
    }, {
        'url': 'https://www.instagram.com/p/Dcq9fquOcko',
        'only_matching': True,
    }]


class InstagramHDRStoryIE(_InstagramHDRMixin, InstagramStoryIE):
    IE_NAME = 'instagram:story:hdr'
    IE_DESC = 'Instagram Stories HDR (authenticated iOS API)'
    _NATIVE_IE = InstagramStoryIE
    _TESTS = [{
        'url': 'https://www.instagram.com/stories/pablo_quero_leite/',
        'only_matching': True,
    }, {
        'url': 'https://www.instagram.com/stories/pablo_quero_leite/1234567890/',
        'only_matching': True,
    }]

    def _extract_product_media(self, product_media):
        is_video = bool(
            product_media.get('media_type') == 2
            or product_media.get('video_versions')
            or product_media.get('video_dash_manifest')
        )
        if not is_video:
            return InstagramBaseIE._extract_product_media(self, product_media)

        media_id = str(product_media.get('pk') or '')
        detailed_media = traverse_obj(self._download_json(
            f'{self._API_BASE_URL}/media/{media_id}/info/', media_id,
            'Downloading HDR story info', 'HDR story info extraction failed',
            impersonate=self._can_impersonate and self._is_web_app,
            headers=self._api_headers), ('items', 0, {dict}))
        try:
            return super()._extract_product_media(detailed_media or product_media)
        except ExtractorError as error:
            if not error.expected:
                raise
            return InstagramBaseIE._extract_product_media(self, detailed_media or product_media)
