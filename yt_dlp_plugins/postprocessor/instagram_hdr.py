from __future__ import annotations

import functools
import os
import re
import subprocess

from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor, resolve_mapping
from yt_dlp.utils import (
    MEDIA_EXTENSIONS,
    Popen,
    PostProcessingError,
    prepend_extension,
    replace_extension,
)


class _NotHDRError(PostProcessingError):
    """The file is readable but not 10-bit BT.2020 HLG/PQ video."""


def _hevc_result(downloader):
    """Return (extension of a converted item after the user's post-processors, None), or
    (None, reason) when one of them undoes the conversion."""
    ext = 'mp4'
    for pp in (pp for pps in getattr(downloader, '_pps', {}).values() for pp in pps):
        key = pp.pp_key()
        if key == 'ExtractAudio':
            return None, 'the audio is extracted (-x)'
        if key in ('VideoRemuxer', 'VideoConvertor') and getattr(pp, 'mapping', None):
            target, skip = resolve_mapping(ext, pp.mapping)
            if skip:
                continue
            if key == 'VideoConvertor':
                return None, f'--recode-video re-encodes it to {target} afterwards'
            ext = target  # --remux-video only changes the container
    return ext, None


class InstagramHDRSelectionPP(PostProcessor):
    """Check each selected format before download (the 'video' stage).

    Warns when -f/-S picked SDR video although HDR formats exist, and sets final_ext for
    items that will be converted to HEVC.
    """

    def __init__(self, downloader=None):
        super().__init__(downloader)
        self._user_final_ext = self.get_param('final_ext')

    def run(self, info):
        videos = [fmt for fmt in info.get('requested_formats') or [info] if fmt.get('vcodec') != 'none']
        hdr = [fmt.get('dynamic_range') not in (None, 'SDR') for fmt in videos]
        spec = self.get_param('format') or ''
        # Several downloads (-f "a,b", -f all) or a selector function: another one may be HDR
        several = not isinstance(spec, str) or ',' in spec or re.match(r'\s*all\b', spec)
        if info.get('_instagram_hdr') and videos and not any(hdr) and not several:
            self.report_warning(
                f'HDR formats are available, but the selected video ({info.get("format_id")}) is SDR, '
                'so it is neither verified nor converted to HEVC. Check your -f/-S options, '
                'for example -f "bv*[dynamic_range!=SDR]+ba/b"')
        if self._downloader:
            # Like --remux-video, tell yt-dlp the extension of the converted file so a re-run
            # finds it, but only for this item; everything else keeps the user's setting
            ext, _ = _hevc_result(self._downloader)
            converts = info.get('_instagram_hdr_hevc') and hdr == [True] and ext
            self._downloader.params['final_ext'] = ext if converts else self._user_final_ext
        return [], info


class InstagramHDRVerifyPP(FFmpegPostProcessor):
    """Verify the final, already-muxed file with ffprobe."""

    def run(self, info):
        # Items being converted are verified by InstagramHDRHEVCPP instead
        if info.get('_instagram_hdr_verify', True) and not info.get('_instagram_hdr_hevc'):
            path = self._hdr_path(info, 'HDR verification')
            if path:
                try:
                    self._verify(path)
                except PostProcessingError as error:
                    # Read-only check: the download succeeded, so warn instead of failing it
                    # (which would also skip its --download-archive entry)
                    self.report_warning(str(error))
        return [], info

    def _hdr_path(self, info, action):
        if not info.get('_instagram_hdr'):
            return None
        # ExtractAudio changes ext/filepath but can leave the original video metadata.
        if info.get('vcodec') == 'none' or info.get('ext') in MEDIA_EXTENSIONS.audio:
            return None
        videos = [fmt for fmt in info.get('requested_formats') or () if fmt.get('vcodec') != 'none']
        if len(videos) > 1:
            # yt-dlp leaves dynamic_range unset when a merge has several video streams
            if any(fmt.get('dynamic_range') not in (None, 'SDR') for fmt in videos):
                self.report_warning(f'Skipping {action}: several video streams were selected')
            return None
        # The entry also lists native SDR and audio formats; only HDR selections are checked
        if info.get('dynamic_range') in (None, 'SDR'):
            return None
        path = info.get('filepath')
        if not path or path == '-' or not os.path.isfile(path):
            return None
        return path

    def _main_video(self, path):
        """Return (ordinal among video streams, stream) of the video that is not cover art."""
        try:
            metadata = self.get_metadata_object(path, opts=['-v', 'error'])
        except ValueError as error:  # ffprobe crashed or printed no JSON
            raise PostProcessingError(
                f'Instagram HDR verification failed: ffprobe could not read "{path}"') from error
        videos = [stream for stream in metadata.get('streams', []) if stream.get('codec_type') == 'video']
        # An embedded cover is also a video stream, and it can come first
        return next((
            (index, stream) for index, stream in enumerate(videos)
            if not (stream.get('disposition') or {}).get('attached_pic')
        ), (0, None))

    def _verify(self, path):
        _, video = self._main_video(path)
        if not video:
            raise _NotHDRError('Instagram HDR verification failed: no video stream')

        codec = video.get('codec_name')
        profile = str(video.get('profile') or '')
        pixel_format = str(video.get('pix_fmt') or '')
        primaries = video.get('color_primaries')
        transfer = video.get('color_transfer')
        color_space = video.get('color_space')

        ten_bit = bool(re.search(r'(?:p10|p010)', pixel_format, re.IGNORECASE))
        hdr_metadata = (
            primaries == 'bt2020'
            and transfer in {'arib-std-b67', 'smpte2084'}
            and color_space in {'bt2020nc', 'bt2020c'}
        )
        if not (ten_bit and hdr_metadata):
            raise _NotHDRError(
                'Instagram HDR verification failed: '
                f'codec={codec}, profile={profile}, pix_fmt={pixel_format}, '
                f'primaries={primaries}, transfer={transfer}, colorspace={color_space}')

        self.to_screen(
            f'HDR verified: {codec} {profile}, {pixel_format}, '
            f'{primaries}, {transfer}, {color_space}')
        return codec


class InstagramHDRHEVCPP(InstagramHDRVerifyPP):
    """Re-encode HDR video to HEVC Main 10 in MP4, which Apple devices play and save to Photos."""

    # Tried in order. _encode adds the main video's stream specifier to every option, so
    # --ppa "InstagramHDRHEVC:-q:v 40 -crf 26" (applied after these) overrides the quality
    _ENCODERS = (
        ('hevc_videotoolbox', '-q', '50', '-pix_fmt', 'p010le'),
        ('libx265', '-preset', 'fast', '-crf', '22', '-pix_fmt', 'yuv420p10le',
         '-x265-params', 'log-level=error'),
    )

    def run(self, info):
        path = info.get('_instagram_hdr_hevc') and self._hdr_path(info, 'HEVC conversion')
        if not path:
            return [], info
        _, undone = _hevc_result(self._downloader)
        if undone:
            self.report_warning(f'Not converting to HEVC: {undone}')
            return [], info
        try:
            codec = self._verify(path)
        except _NotHDRError as error:
            # Keep the download (and its --download-archive entry), as the read-only check does
            self.report_warning(
                f'Not converting to HEVC: {error}. If "{path}" is an earlier download, '
                'pass --force-overwrites to download it again')
            return [], info
        if codec == 'hevc':  # e.g. converted on a previous run
            info['vcodec'] = 'hevc'
            return [], info

        # WebM can't hold HEVC and Photos won't import MKV, so the result is MP4 (a later
        # --remux-video can still change the container)
        out_path = replace_extension(path, 'mp4', info.get('ext'))
        final_dir = info.get('__finaldir') or os.path.dirname(out_path)
        force = self.get_param('overwrites') is True
        # Like -k, --no-overwrites keeps an original that this run did not download
        keep = self.get_param('keepvideo') or (
            self.get_param('overwrites') is False and not info.get('__real_download'))
        # An MP4 is replaced in place, so keeping its original needs a *.orig.mp4 copy
        original_path = prepend_extension(path, 'orig') if out_path == path and keep else path

        # Like yt-dlp itself, never replace an existing video file without --force-overwrites
        targets = [out_path, os.path.join(final_dir, os.path.basename(out_path))]
        if keep:
            targets += [original_path, os.path.join(final_dir, os.path.basename(original_path))]
        for target in targets:
            if not force and os.path.abspath(target) != os.path.abspath(path) and os.path.lexists(target):
                raise PostProcessingError(
                    f'Not converting to HEVC: "{target}" already exists; '
                    'move it away or pass --force-overwrites')

        # Fixed and visible, like yt-dlp's own temporary files, so a killed run leaves at most
        # this file, and the next run overwrites and removes it
        temp_path = prepend_extension(out_path, 'temp')
        self.to_screen(f'Converting HDR video to HEVC: "{out_path}"')
        try:
            self._convert(path, temp_path)
            backup = self._back_up(path, original_path, force) if original_path != path else None
            try:
                os.replace(temp_path, out_path)
            except OSError:
                if backup == 'renamed':
                    os.replace(original_path, path)
                elif backup == 'linked':
                    os.remove(original_path)
                raise
        finally:
            if os.path.lexists(temp_path):
                os.remove(temp_path)

        info.update(filepath=out_path, ext='mp4', vcodec='hevc')
        if not keep:
            return ([] if original_path == out_path else [original_path]), info
        if self.get_param('keepvideo'):
            return [original_path], info  # yt-dlp keeps it and moves it with the video
        # --no-overwrites: keep the original and move it out of the temp dir like -k would
        info.setdefault('__files_to_move', {}).setdefault(original_path, '')
        return [], info

    @functools.cached_property
    def _available_encoders(self):
        # Empty when unknown; every encoder is then tried
        if not self.available:
            return set()
        stdout, _, returncode = Popen.run(
            [self.executable, '-hide_banner', '-encoders'], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        return set(re.findall(r'(?m)^\s*V\S*\s+(\S+)', stdout)) if returncode == 0 else set()

    def _convert(self, path, temp_path):
        errors = []
        for encoder, *options in self._ENCODERS:
            if self._available_encoders and encoder not in self._available_encoders:
                errors.append(f'{encoder} is not in this FFmpeg build')
                continue
            try:
                self._encode(path, temp_path, encoder, *options)
                # Extra FFmpeg options can override the encoder. HDR metadata alone
                # cannot establish that the requested HEVC conversion succeeded.
                codec = self._verify(temp_path)
                if codec != 'hevc':
                    raise PostProcessingError(
                        f'HEVC conversion verification failed: expected hevc, got {codec}')
                return
            except PostProcessingError as error:
                errors.append(f'{encoder}: {error}')
                self.to_screen(f'HEVC encoding with {encoder} failed: {error}')
        raise PostProcessingError(f'HEVC conversion failed ({"; ".join(errors)})')

    def _encode(self, path, out_path, encoder, *options):
        # Only the main video stream is re-encoded; audio, subtitles and cover art are copied.
        # Colour tags (BT.2020, HLG/PQ) carry over from the decoded frames; _verify checks them
        spec = f'v:{self._main_video(path)[0]}'
        pairs = ('-c', encoder, *options, '-profile', 'main10', '-tag', 'hvc1')
        self.run_ffmpeg(path, out_path, [
            *self.stream_copy_opts(ext='mp4'), '-map', '-0:t',
            *(f'{arg}:{spec}' if index % 2 == 0 else arg for index, arg in enumerate(pairs))])

    @staticmethod
    def _back_up(path, backup, force):
        # A hard link leaves the original in place until the atomic swap and, unlike
        # rename(), never replaces a file that appeared while encoding
        if force and os.path.lexists(backup):
            os.remove(backup)
        try:
            os.link(path, backup)
            return 'linked'
        except FileExistsError:
            raise PostProcessingError(
                f'Not converting to HEVC: "{backup}" appeared while encoding; move it away')
        except OSError:  # no hard links (FAT, exFAT, some network shares)
            if os.path.lexists(backup):
                raise PostProcessingError(
                    f'Not converting to HEVC: "{backup}" appeared while encoding; move it away')
            os.rename(path, backup)
            return 'renamed'
