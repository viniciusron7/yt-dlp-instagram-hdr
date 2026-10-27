from __future__ import annotations

import html
import re
import time
import traceback
from fractions import Fraction
from typing import Any
from urllib.parse import urlparse

from yt_dlp.extractor.instagram import (
    InstagramBaseIE,
    InstagramIE,
    InstagramStoryIE,
    _id_to_pk,
    _pk_to_id,
)
from yt_dlp.utils import (
    ExtractorError,
    float_or_none,
    int_or_none,
    parse_codecs,
    traverse_obj,
    url_or_none,
)
from yt_dlp_plugins.postprocessor.instagram_hdr import (
    InstagramHDRHEVCPP,
    InstagramHDRSelectionPP,
    InstagramHDRVerifyPP,
)


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

    # Explicit colour metadata takes precedence over stale encoding names.
    if transfer == 18:
        transfer_kind = 'HLG'
    elif transfer == 16:
        transfer_kind = 'HDR10'
    elif transfer is not None:
        return None
    elif 'hlg' in tag or 'arib-std-b67' in tag:
        transfer_kind = 'HLG'
    elif 'pq' in tag or 'smpte2084' in tag:
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
        adaptation_context = _BASE_URL_RE.sub('', _REPRESENTATION_RE.sub('', body))
        for match in _REPRESENTATION_RE.finditer(body):
            attrs = {**adaptation_attrs, **_parse_attributes(match.group('attrs'))}
            base_url = _BASE_URL_RE.search(match.group('body'))
            url = base_url and _decode_base_url(base_url.group('url'))
            # Instagram's CDN serves absolute https URLs; anything else (rtmp://, ftp://,
            # //host) must not pick the protocol or the downloader
            if not url or not url.lower().startswith('https://'):
                continue

            mime_type = attrs.get('mimeType', '').lower()
            content_type = attrs.get('contentType', '').lower()
            if mime_type.startswith('video/') or content_type == 'video':
                kind = 'video'
            elif mime_type.startswith('audio/') or content_type == 'audio':
                kind = 'audio'
            else:
                continue

            rep_context = (
                f'{adaptation_context} {match.group("attrs")} '
                f'{_BASE_URL_RE.sub("", match.group("body"))}')
            tracks.append({
                **attrs,
                'kind': kind,
                'url': url,
                'hdr_kind': _hdr_kind(attrs, rep_context) if kind == 'video' else None,
                'width_int': _int_value(attrs.get('width')),
                'height_int': _int_value(attrs.get('height')),
                'bandwidth_int': _int_value(attrs.get('bandwidth')),
                'frame_rate_float': _frame_rate(attrs.get('frameRate')),
            })
    return tracks


def _format_id(prefix: str, track: dict[str, Any]) -> str:
    identifier = track.get('FBEncodingTag') or track.get('id') or str(track['bandwidth_int'])
    return f'{prefix}-{identifier}'


def _video_format(track: dict[str, Any], quality: int) -> dict[str, Any]:
    codec_info = parse_codecs(track.get('codecs', ''))
    codec_info['dynamic_range'] = track['hdr_kind'] or 'SDR'
    return {
        'url': track['url'],
        'protocol': 'https',
        'format_id': _format_id('hdr' if track['hdr_kind'] else 'dash', track),
        'format_note': f'DASH video, {codec_info["dynamic_range"]}',
        'ext': 'mp4',
        'width': track['width_int'] or None,
        'height': track['height_int'] or None,
        'fps': track['frame_rate_float'] or None,
        'vbr': float_or_none(track['bandwidth_int'], 1000),
        'tbr': float_or_none(track['bandwidth_int'], 1000),
        'filesize': int_or_none(track.get('FBContentLength')),
        'quality': quality if track['hdr_kind'] else -1,
        'http_headers': {
            'Referer': 'https://www.instagram.com/',
        },
        **codec_info,
    }


def _audio_format(track: dict[str, Any]) -> dict[str, Any]:
    bitrate = _int_value(track.get('FBAvgBitrate')) or track['bandwidth_int']
    return {
        'url': track['url'],
        'protocol': 'https',
        'format_id': _format_id('audio', track),
        'format_note': 'DASH audio, xHE-AAC' if track.get('codecs') == 'mp4a.40.42' else 'DASH audio',
        'ext': 'mp4',
        'abr': float_or_none(bitrate, 1000),
        'tbr': float_or_none(track['bandwidth_int'], 1000),
        'asr': int_or_none(track.get('audioSamplingRate')),
        'filesize': int_or_none(track.get('FBContentLength')),
        'http_headers': {
            'Referer': 'https://www.instagram.com/',
        },
        **parse_codecs(track.get('codecs', '')),
    }


def _media_entries(info: dict[str, Any]) -> list[dict[str, Any]]:
    if info.get('_type') == 'playlist':
        return [entry for entry in info.get('entries') or [] if isinstance(entry, dict)]
    return [info]


def _is_xhe_aac(fmt: dict[str, Any]) -> bool:
    return str(fmt.get('acodec') or '').lower() == 'mp4a.40.42'


def _is_audio_only(fmt: dict[str, Any]) -> bool:
    return fmt.get('vcodec') == 'none' and fmt.get('acodec') not in {None, 'none'}


def _disambiguate_format_ids(formats: list[dict[str, Any]]) -> None:
    counts = {}
    for fmt in formats:
        format_id = fmt.get('format_id')
        counts[format_id] = counts.get(format_id, 0) + 1

    indexes = {}
    reserved = set(counts)
    for fmt in formats:
        format_id = fmt.get('format_id')
        if format_id is None or counts[format_id] < 2:
            continue
        index = indexes.get(format_id, 0)
        while f'{format_id}-{index}' in reserved:
            index += 1
        fmt['format_id'] = f'{format_id}-{index}'
        reserved.add(fmt['format_id'])
        indexes[format_id] = index + 1


def _merge_native_formats(
        info: dict[str, Any], native_info: dict[str, Any],
        include_xhe_aac: bool = False) -> list[str]:
    # Merge locally parsed formats, including complementary API responses.
    # A progressive file cannot replace an audio-only track for bv+ba.
    native_by_id = {
        str(entry['id']): entry
        for entry in _media_entries(native_info) if entry.get('id') is not None
    }
    missing = []

    for entry in _media_entries(info):
        # Pair by id so carousel or Story entries cannot borrow another item's audio.
        native_formats = [dict(fmt) for fmt in traverse_obj(
            native_by_id, (str(entry.get('id')), 'formats', ..., {dict}))]
        if native_formats:
            formats = list(entry.get('formats') or [])
            urls = {fmt['url'] for fmt in formats if fmt.get('url')}
            for fmt in native_formats:
                if fmt.get('url') and fmt['url'] in urls:
                    continue
                formats.append(fmt)
                if fmt.get('url'):
                    urls.add(fmt['url'])
            entry['formats'] = formats
            native_entry = native_by_id[str(entry.get('id'))]
            if native_entry.get('_instagram_hdr') and not entry.get('_instagram_hdr'):
                entry.update({key: native_entry[key] for key in (
                    '_instagram_hdr', '_instagram_hdr_verify', '_instagram_hdr_hevc',
                    '_format_sort_fields') if key in native_entry})
        else:
            missing.append(str(entry.get('id')))
        formats = entry.get('formats') or []
        # Quality ranks must cover the combined HDR ladder, not each response's
        # ladder independently (a lower resolution may have a higher local rank).
        hdr_formats = sorted(
            (fmt for fmt in formats if fmt.get('dynamic_range') not in {None, 'SDR'}),
            key=lambda fmt: (
                (fmt.get('width') or 0) * (fmt.get('height') or 0),
                fmt.get('vbr') or fmt.get('tbr') or 0,
                fmt.get('fps') or 0))
        for quality, fmt in enumerate(hdr_formats):
            fmt['quality'] = quality
        if not include_xhe_aac and any(
                _is_audio_only(fmt) and not _is_xhe_aac(fmt) for fmt in formats):
            entry['formats'] = [fmt for fmt in formats if not _is_xhe_aac(fmt)]
        _disambiguate_format_ids(entry.get('formats') or [])
    return missing


def _add_post_processor(downloader, pp, when, before=()):
    # Insert ahead of the first PP whose key is in `before`, so e.g. --embed-metadata,
    # --xattrs and --exec act on the converted file rather than on the original
    processors = getattr(downloader, '_pps', {}).get(when)
    if not isinstance(processors, list):
        return downloader.add_post_processor(pp, when=when)
    if any(other.pp_key() == pp.pp_key() for other in processors):
        return None  # already added, e.g. with --use-postprocessor
    index = next((i for i, other in enumerate(processors) if other.pp_key() in before), len(processors))
    processors.insert(index, pp)
    return None


class _InstagramSessionError(ExtractorError):
    """Stop authenticated requests, including non-fatal Story/comment lookups."""


class _InstagramHDRMixin:
    @property
    def _app_id(self):
        mode = self._api_mode()
        return self._APP_IDS['web' if mode == 'auto' else mode]

    def _api_mode(self):
        values = self._plugin_configuration_arg('api')
        mode = values[-1] if values else 'auto'
        if mode not in {'auto', 'web', 'ios'}:
            raise ExtractorError('instagramhdr:api must be auto, web or ios', expected=True)
        return mode

    @property
    def _api_headers(self):
        mode = self._api_mode()
        return self._api_headers_for('web' if mode == 'auto' else mode)

    def _api_headers_for(self, mode):
        headers = {**super()._api_headers, 'X-IG-App-ID': self._APP_IDS[mode]}
        if mode == 'ios':
            # Never fabricate device IDs, bandwidth, telemetry or a fresh session.
            headers['User-Agent'] = _IOS_USER_AGENT
            headers['X-IG-Capabilities'] = '36r/F/8='
        if csrf := self._get_cookies(self._BASE_URL).get('csrftoken'):
            headers['X-CSRFToken'] = csrf.value
        return headers

    def _download_media_info(self, media_id, display_id, *, mode=None, fatal=True,
                             note='Downloading video info'):
        mode = mode or self._api_mode()
        mode = 'web' if mode == 'auto' else mode
        host = 'www.instagram.com' if mode == 'web' else 'i.instagram.com'
        return traverse_obj(self._download_json(
            f'https://{host}/api/v1/media/{media_id}/info/', display_id,
            note, 'Video info extraction failed', fatal=fatal,
            impersonate=self._can_impersonate and mode == 'web',
            headers=self._api_headers_for(mode)), ('items', 0, {dict}))

    def _enrich_formats(self, result, media_id, display_id):
        entries = [entry for entry in _media_entries(result) if any(
            fmt.get('vcodec') != 'none' for fmt in entry.get('formats') or [])]
        mode = self._api_mode()
        include_xhe_aac = self._plugin_flag('include_xhe_aac')
        if mode == 'auto' and any(
                not entry.get('_instagram_hdr') or not any(
                    _is_audio_only(fmt) for fmt in entry.get('formats') or [])
                for entry in entries):
            alternate = 'ios'
            note = 'Checking iOS media info for HDR and audio formats'
        elif mode == 'ios' and any(not any(
                _is_audio_only(fmt) and (include_xhe_aac or not _is_xhe_aac(fmt))
                for fmt in entry.get('formats') or []) for entry in entries):
            alternate = 'web'
            note = 'Checking web media info for compatible audio'
        else:
            return result

        # Only supplement successful responses. Access rejections still stop the
        # run; no other client is tried after a rejected request.
        item = self._download_media_info(
            media_id, display_id, mode=alternate, fatal=False, note=note)
        if not item:
            return result
        ids = {entry.get('id') for entry in entries}
        media_items = traverse_obj(item, ('carousel_media', ..., {dict})) or [item]
        supplemental = {'_type': 'playlist', 'entries': [
            _InstagramHDRMixin._extract_product_media(self, media)
            for media in media_items if traverse_obj(media, ('pk', {_pk_to_id})) in ids
        ]}
        _merge_native_formats(result, supplemental, include_xhe_aac)
        return result

    @property
    def _access_state(self):
        # Reels, Stories and comments in the same run share the stop flag and pacing.
        return self._downloader if self._downloader is not None else self

    def _check_access(self):
        if message := getattr(self._access_state, '_instagram_hdr_access_blocked', None):
            raise _InstagramSessionError(message, expected=True)

    def _stop_access(self, reason):
        message = (
            f'Instagram {reason}. Further Instagram requests through this plugin are stopped for this run. '
            'Check the account in Instagram and resolve any verification or restriction '
            'before trying again; do not repeatedly refresh cookies or retry')
        self._access_state._instagram_hdr_access_blocked = message
        raise _InstagramSessionError(message, expected=True)

    @staticmethod
    def _restricted_url(url):
        return bool(re.match(
            r'^/(?:accounts/(?:login|suspended|disabled)|challenge|checkpoint|consent)(?:/|$)',
            urlparse(url or '').path))

    def _request_webpage(self, url_or_request, video_id, note=None, errnote=None, fatal=True, **kwargs):
        self._check_access()
        # yt-dlp implements an explicit --sleep-requests value itself. Supply a
        # conservative default when it was not set, without mutating global options.
        if self.get_param('sleep_interval_requests') is None:
            last = getattr(self._access_state, '_instagram_hdr_last_request', None)
            if last is not None:
                delay = 2.0 - (time.monotonic() - last)
                if delay > 0:
                    time.sleep(delay)
        self._access_state._instagram_hdr_last_request = time.monotonic()
        try:
            # Inspect errors even when the caller requested fatal=False.
            response = super()._request_webpage(
                url_or_request, video_id, note, None if errnote is False else errnote,
                fatal=True, **kwargs)
        except ExtractorError as error:
            status = getattr(error.cause, 'status', None)
            response_url = getattr(getattr(error.cause, 'response', None), 'url', None)
            if status in {401, 403, 404, 429} or self._restricted_url(response_url):
                self._stop_access(f'rejected access (HTTP {status})' if status else 'requested account verification')
            if fatal:
                raise
            if errnote is not False:
                self.report_warning(error.orig_msg)
            return False
        if response and getattr(response, 'status', None) in {401, 403, 404, 429}:
            self._stop_access(f'rejected access (HTTP {response.status})')
        if response and self._restricted_url(getattr(response, 'url', None)):
            self._stop_access('redirected to login or account verification')
        return response

    def _download_json(self, *args, **kwargs):
        self._check_access()
        result = super()._download_json(*args, **kwargs)
        if isinstance(result, dict):
            signals = ' '.join(str(result.get(key) or '') for key in (
                'message', 'error_type', 'feedback_title', 'feedback_message')).lower()
            if (result.get('status') == 'fail'
                    or any(result.get(key) for key in (
                        'challenge', 'challenge_required', 'checkpoint_url',
                        'feedback_required', 'feedback_url', 'spam'))
                    or re.search(r'challenge|checkpoint|login_required|login required|feedback_required|'
                                 r'consent_required|rate.?limit|please wait|wait a few minutes|'
                                 r'sentry_block|suspend|disabled|try again later|'
                                 r'we restrict certain activity', signals)):
                self._stop_access('requested verification or restricted this session')
        return result

    @classmethod
    def suitable(cls, url):
        # The built-in extractor runs on the same URL too, so never claim one it rejects
        return super().suitable(url) and cls._NATIVE_IE.suitable(url)

    @staticmethod
    def _is_auth_rejection(error: ExtractorError) -> bool:
        return getattr(error.cause, 'status', None) in {401, 403}

    def _plugin_configuration_arg(self, key):
        if not self._downloader:
            return []
        values = self._configuration_arg(key, ie_key=self.ie_key())
        if values or self.ie_key() == 'InstagramHDR':
            return values
        return self._configuration_arg(key, ie_key='InstagramHDR')

    def _plugin_flag(self, key, default=False):
        values = self._plugin_configuration_arg(key)
        if not values:
            return default
        return values[-1] not in {'0', 'false', 'no', 'off'}

    def _verification_enabled(self):
        return self._plugin_flag('verify', default=True)

    def _real_initialize(self):
        self._check_access()
        if self._plugin_flag('disable'):
            self._fallback_to_native = True
            self.to_screen(
                'Instagram HDR plugin disabled; using yt-dlp\'s built-in extractor')
            return

        cookies = self._get_cookies(self._BASE_URL)
        if not cookies.get('sessionid'):
            self._fallback_to_native = True
            self.report_warning(
                'Authenticated Instagram cookies were not supplied; HDR formats are '
                'unavailable. Falling back to yt-dlp\'s built-in Instagram extractor. '
                'Pass --cookies-from-browser BROWSER or --cookies FILE to enable HDR.',
            )
            return
        if self._api_mode() == 'ios':
            self.report_warning(
                'The iOS API uses a mobile client with browser cookies and can '
                'put the account at risk. Use instagramhdr:api=web to use only the web API',
                only_once=True)
        # Added once per YoutubeDL (later calls are no-ops); each item carries its own options.
        # The HEVC conversion runs after -x but before the steps that act on the final file,
        # including --remux-video, so the user's container choice applies last
        downloader = self._downloader
        _add_post_processor(downloader, InstagramHDRSelectionPP(downloader), 'video')
        _add_post_processor(downloader, InstagramHDRHEVCPP(downloader), 'post_process', before={
            'VideoRemuxer', 'VideoConvertor', 'EmbedSubtitle', 'ModifyChapters', 'Metadata',
            'EmbedThumbnail', 'SplitChapters', 'XAttrMetadata', 'Exec'})
        _add_post_processor(downloader, InstagramHDRVerifyPP(downloader), 'after_move', before={'Exec'})
        return super()._real_initialize()

    def _native_url_result(self, url):
        match = self._match_valid_url(url)
        video_id = match.groupdict().get('id') or match.groupdict().get('user')
        return self.url_result(url, ie=self._NATIVE_IE, video_id=video_id)

    def _extract_api(self, url):
        return super()._real_extract(url)

    def _real_extract(self, url):
        self._check_access()
        if getattr(self, '_fallback_to_native', False):
            return self._native_url_result(url)
        try:
            return self._extract_api(url)
        except _InstagramSessionError:
            raise
        except (ExtractorError, AttributeError, KeyError, IndexError, TypeError, ValueError) as error:
            if isinstance(error, ExtractorError):
                status = getattr(error.cause, 'status', None)
                if status in {401, 403, 404, 429}:
                    self._stop_access(f'rejected access (HTTP {status})')
                # Do not repeat authenticated extraction with another client, including
                # 404 (which Instagram also uses for session restrictions).
                raise
            else:
                self.write_debug(''.join(traceback.format_exception(error)))
                raise ExtractorError(
                    'Instagram returned unexpected media data; stopped without another '
                    'authenticated extraction', cause=error) from error

    def _extract_product_media(self, product_media):
        # Only the item's own manifest: other strings are user-written (caption) or belong
        # to other media (original sound, reshared post) and must never become formats
        tracks = _parse_manifest(traverse_obj(product_media, ('video_dash_manifest', {str})) or '')

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
            (track for track in unique_tracks
             if track['kind'] == 'audio'),
            key=lambda track: track['bandwidth_int'],
        )
        # Read progressive formats and metadata with yt-dlp's parser, then add all
        # DASH tracks locally. Our manifest parser also tolerates Instagram's
        # unescaped ampersands.
        native = InstagramBaseIE._extract_product_media(
            self, {**product_media, 'video_dash_manifest': ''})
        native['formats'] = [
            *(_video_format(track, -1) for track in unique_tracks
              if track['kind'] == 'video' and not track['hdr_kind']),
            *map(_audio_format, audios),
            *native['formats'],
        ]
        result = {**native, 'formats': [
            _video_format(track, quality) for quality, track in enumerate(videos)]}
        include_xhe_aac = self._plugin_flag('include_xhe_aac')
        _merge_native_formats(result, native, include_xhe_aac)
        _disambiguate_format_ids(result['formats'])
        if not videos:
            self.to_screen(
                'No VP9/AV1 10-bit HDR representation in this media response; keeping available SDR formats')
            return result

        best_video = videos[-1]
        message = (
            f'HDR formats available: up to {best_video["width_int"]}x{best_video["height_int"]}, '
            f'{best_video["frame_rate_float"]:g} fps, {best_video["hdr_kind"]}')
        if include_xhe_aac:
            xhe_audios = [track for track in audios if track.get('codecs') == 'mp4a.40.42']
            message += f'; {len(xhe_audios)} xHE-AAC audio format(s) enabled'
        self.to_screen(message)

        return {
            **result,
            '_format_sort_fields': ('quality', 'res', 'br', 'fps'),
            '_instagram_hdr': True,
            # Resolved per extractor, so instagramhdrstory:* options only affect Stories
            '_instagram_hdr_verify': self._verification_enabled(),
            '_instagram_hdr_hevc': self._plugin_flag('hevc'),
        }


class InstagramHDRIE(_InstagramHDRMixin, InstagramIE):
    IE_NAME = 'instagram:hdr'
    IE_DESC = 'Instagram videos and Reels with HDR formats'
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

    def _extract_api(self, url):
        # Keep the session intact if a request is rejected. The native extractor
        # clears sessionid on login redirects and continues with more requests.
        video_id = self._match_id(url)
        media_id = _id_to_pk(video_id)
        item = self._download_media_info(media_id, video_id)
        if not item:
            raise ExtractorError('Instagram returned no media item')
        return self._enrich_formats(self._extract_product(item), media_id, video_id)


class InstagramHDRStoryIE(_InstagramHDRMixin, InstagramStoryIE):
    IE_NAME = 'instagram:story:hdr'
    IE_DESC = 'Instagram Stories with HDR formats'
    _NATIVE_IE = InstagramStoryIE
    _TESTS = [{
        'url': 'https://www.instagram.com/stories/pablo_quero_leite/',
        'only_matching': True,
    }, {
        'url': 'https://www.instagram.com/stories/pablo_quero_leite/1234567890/',
        'only_matching': True,
    }]

    def _extract_api(self, url):
        username, story_id = self._match_valid_url(url).group('user', 'id')
        if username == 'highlights' and not story_id:
            raise ExtractorError('Input URL is missing a highlight ID', expected=True)
        display_id = story_id or username
        webpage = self._download_webpage(
            url, display_id, impersonate=self._can_impersonate and self._is_web_app)
        user_info = self._search_json(r'"user":', webpage, 'user info', display_id, fatal=False)
        if not user_info:
            raise ExtractorError('Instagram Story user information is unavailable', expected=True)
        user_id = str(user_info.get('pk') or user_info.get('id') or '')
        if username != 'highlights' and not user_id:
            raise ExtractorError('Unable to extract user id', expected=True)
        reel_id = f'highlight:{story_id}' if username == 'highlights' else user_id
        reels = traverse_obj(self._download_json(
            f'{self._API_BASE_URL}/feed/reels_media/', display_id,
            query={'reel_ids': reel_id}, headers=self._api_headers,
            impersonate=self._can_impersonate and self._is_web_app), ('reels', {dict})) or {}
        reel = reels.get(reel_id) or {}
        highlights = traverse_obj(reel, ('items', ..., {dict}))
        if not highlights:
            raise ExtractorError('No accessible Stories were returned by Instagram', expected=True)
        only_story = username != 'highlights' and story_id and not self._yes_playlist(username, story_id)
        if only_story:
            # Select before detail lookups: a single Story must not query the rest.
            highlights = [item for item in highlights
                          if str(item.get('pk', '')).split('_')[0] == story_id]
        owner = reel.get('user') or user_info
        entries = []
        for item in highlights:
            product = {**item, 'user': {**(item.get('user') or {}), **owner}}
            entry = self._extract_product(product)
            if entry.get('formats'):
                entries.append(entry)
        if only_story:
            return next(iter(entries), None)
        return self.playlist_result(
            entries, playlist_id=story_id, playlist_title=reel.get('title') or f'Story by {username}')

    def _extract_product_media(self, product_media):
        is_video = bool(
            product_media.get('media_type') == 2
            or product_media.get('video_versions')
            or product_media.get('video_dash_manifest')
        )
        if not is_video:
            return InstagramBaseIE._extract_product_media(self, product_media)

        media_id = str(product_media.get('pk') or '')
        tray_manifest = traverse_obj(product_media, ('video_dash_manifest', {str})) or ''
        result = super()._extract_product_media(product_media)
        if not media_id or any(track['hdr_kind'] for track in _parse_manifest(tray_manifest)):
            return self._enrich_formats(result, media_id, media_id) if media_id else result
        # The tray may omit HDR even when it includes SDR DASH. Enrich only items
        # without an HDR manifest, using the same client and access protections.
        detailed_media = self._download_media_info(
            media_id, media_id, note='Downloading HDR story info', fatal=False)
        if detailed_media:
            detailed_result = super()._extract_product_media({**product_media, **detailed_media})
            _merge_native_formats(result, detailed_result, self._plugin_flag('include_xhe_aac'))
        return self._enrich_formats(result, media_id, media_id)
