from __future__ import annotations

import os
import re
import tempfile

from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor, FFmpegPostProcessorError
from yt_dlp.utils import MEDIA_EXTENSIONS, PostProcessingError, prepend_extension, replace_extension


class InstagramHDRVerifyPP(FFmpegPostProcessor):
    """Verify the final, already-muxed file with ffprobe."""

    def run(self, info):
        path = self._hdr_path(info)
        # Items being converted are verified by InstagramHDRHEVCPP instead
        if path and info.get('__instagram_hdr_verify', True) and not info.get('__instagram_hdr_hevc'):
            self._verify(path)
        return [], info

    @staticmethod
    def _hdr_path(info):
        # The entry also lists native SDR and audio formats; only HDR selections are checked
        if not info.get('__instagram_hdr') or info.get('dynamic_range') in (None, 'SDR'):
            return None
        # ExtractAudio changes ext/filepath but can leave the original video metadata.
        if info.get('vcodec') == 'none' or info.get('ext') in MEDIA_EXTENSIONS.audio:
            return None
        path = info.get('filepath')
        if not path or path == '-' or not os.path.isfile(path):
            return None
        return path

    def _verify(self, path):
        metadata = self.get_metadata_object(path, opts=['-v', 'error'])
        video = next((
            stream for stream in metadata.get('streams', [])
            if stream.get('codec_type') == 'video'
        ), None)
        if not video:
            raise PostProcessingError('Instagram HDR verification failed: no video stream')

        codec = video.get('codec_name')
        profile = str(video.get('profile') or '')
        pixel_format = str(video.get('pix_fmt') or '')
        primaries = video.get('color_primaries')
        transfer = video.get('color_transfer')
        color_space = video.get('color_space')

        codec_ok = codec in {'av1', 'hevc'} or (codec == 'vp9' and '2' in profile)
        ten_bit = bool(re.search(r'(?:p10|p010)', pixel_format, re.IGNORECASE))
        hdr_metadata = (
            primaries == 'bt2020'
            and transfer in {'arib-std-b67', 'smpte2084'}
            and color_space in {'bt2020nc', 'bt2020c'}
        )
        if not (codec_ok and ten_bit and hdr_metadata):
            raise PostProcessingError(
                'Instagram HDR verification failed: '
                f'codec={codec}, profile={profile}, pix_fmt={pixel_format}, '
                f'primaries={primaries}, transfer={transfer}, colorspace={color_space}')

        self.to_screen(
            f'HDR verified: {codec} {profile}, {pixel_format}, '
            f'{primaries}, {transfer}, {color_space}')
        return codec


class InstagramHDRHEVCPP(InstagramHDRVerifyPP):
    """Re-encode HDR video to HEVC Main 10 in MP4, which Apple devices play and save to Photos."""

    def run(self, info):
        path = self._hdr_path(info)
        if not path or not info.get('__instagram_hdr_hevc') or self._verify(path) == 'hevc':
            return [], info

        # WebM can't hold HEVC and Photos won't import MKV, so the result is always MP4
        out_path = replace_extension(path, 'mp4')
        original_path = prepend_extension(path, 'orig') if out_path == path else path
        final_dir = info.get('__finaldir') or os.path.dirname(out_path)
        protected_originals = [original_path] if original_path != path else []
        if self.get_param('keepvideo'):
            protected_originals.append(os.path.join(final_dir, os.path.basename(original_path)))
        for target in protected_originals:
            if os.path.abspath(target) != os.path.abspath(path) and os.path.exists(target):
                raise PostProcessingError(f'Refusing to overwrite the original video: "{target}"')
        if not self.get_param('overwrites', True):
            final_path = os.path.join(final_dir, os.path.basename(out_path))
            for target in (out_path, final_path):
                if os.path.abspath(target) != os.path.abspath(path) and os.path.exists(target):
                    raise PostProcessingError(f'HEVC destination already exists: "{target}" (--no-overwrites)')

        self.to_screen(f'Converting HDR video to HEVC: "{out_path}"')
        with tempfile.TemporaryDirectory(prefix='.instagram-hdr-', dir=os.path.dirname(path) or '.') as directory:
            temp_path = os.path.join(directory, 'video.mp4')
            try:
                self._encode(path, temp_path, 'hevc_videotoolbox', '-q:v:0', '50', '-pix_fmt:v:0', 'p010le')
            except FFmpegPostProcessorError as error:
                self.write_debug(f'hevc_videotoolbox failed ({error}); retrying with libx265')
                self.to_screen('Hardware HEVC encoding failed; retrying with libx265 (software, slower)')
                self._encode(
                    path, temp_path, 'libx265', '-preset:v:0', 'fast', '-crf:v:0', '22',
                    '-pix_fmt:v:0', 'yuv420p10le', '-x265-params:v:0', 'log-level=error')
            self._verify(temp_path)
            if original_path != path:
                os.rename(path, original_path)
            try:
                os.replace(temp_path, out_path)
            except OSError:
                if original_path != path:
                    os.rename(original_path, path)
                raise
        info.update(filepath=out_path, ext='mp4', vcodec='hevc')
        # yt-dlp deletes this file normally, or keeps/moves it when -k is set.
        return [original_path], info

    def _encode(self, path, out_path, encoder, *options):
        # Only the main video stream (v:0) is re-encoded; audio and cover art are copied.
        # Colour tags (BT.2020, HLG/PQ) carry over from the decoded frames; _verify checks them
        self.run_ffmpeg(path, out_path, [
            *self.stream_copy_opts(ext='mp4'), '-c:v:0', encoder, *options,
            '-profile:v:0', 'main10', '-tag:v:0', 'hvc1'])
