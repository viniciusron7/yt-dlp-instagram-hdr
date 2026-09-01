from __future__ import annotations

import os
import re

from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor
from yt_dlp.utils import PostProcessingError


class InstagramHDRVerifyPP(FFmpegPostProcessor):
    """Verify the final, already-muxed file with ffprobe."""

    def run(self, info):
        if not info.get('__instagram_hdr'):
            return [], info

        path = info.get('filepath')
        if not path or path == '-' or not os.path.isfile(path):
            return [], info

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

        codec_ok = codec == 'av1' or (codec == 'vp9' and '2' in profile)
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
        return [], info
