import unittest
from pathlib import Path

from yt_dlp.globals import plugin_dirs


# Keep the test bound to this checkout even when another plugin wheel is installed.
plugin_dirs.value = [str(Path(__file__).resolve().parents[2])]
from yt_dlp_plugins.extractor import instagram_hdr as PLUGIN_MODULE


def video_manifest(codec, tag='video', properties='', adaptation_url=''):
    return f'''<MPD><Period><AdaptationSet contentType="video">
      {adaptation_url}
      {properties}
      <Representation id="video" codecs="{codec}" FBEncodingTag="{tag}">
        <BaseURL>https://cdn.example/video.mp4</BaseURL>
      </Representation>
    </AdaptationSet></Period></MPD>'''


def cicp_properties(transfer):
    return f'''
      <EssentialProperty schemeIdUri="urn:mpeg:mpegB:cicp:ColourPrimaries" value="9"/>
      <EssentialProperty schemeIdUri="urn:mpeg:mpegB:cicp:TransferCharacteristics" value="{transfer}"/>
    '''


class HDRMetadataReviewTests(unittest.TestCase):
    def assert_hdr_kind(self, manifest, expected):
        tracks = PLUGIN_MODULE._parse_manifest(manifest)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]['hdr_kind'], expected)

    def test_explicit_sdr_transfer_is_not_overridden_by_hdr_name(self):
        for codec, properties in (
            ('vp09.02.40.10.01.09.01.09.00', ''),
            ('vp09.02.40.10', cicp_properties(1)),
            ('av01.0.08M.10.0.110.09.01.09.0', ''),
        ):
            with self.subTest(codec=codec, properties=bool(properties)):
                self.assert_hdr_kind(video_manifest(codec, 'dash-hdr', properties), None)

    def test_explicit_pq_transfer_is_not_overridden_by_hlg_name(self):
        for codec, properties in (
            ('vp09.02.40.10.01.09.16.09.00', ''),
            ('vp09.02.40.10', cicp_properties(16)),
            ('av01.0.08M.10.0.110.09.16.09.0', ''),
        ):
            with self.subTest(codec=codec, properties=bool(properties)):
                self.assert_hdr_kind(video_manifest(codec, 'dash-hlg', properties), 'HDR10')

    def test_hdr_metadata_without_name_heuristics_is_detected(self):
        for codec, properties, expected in (
            ('vp09.02.40.10.01.09.18.09.00', '', 'HLG'),
            ('vp09.02.40.10.01.09.16.09.00', '', 'HDR10'),
            ('av01.0.08M.10.0.110.09.18.09.0', '', 'HLG'),
            ('av01.0.08M.10.0.110.09.16.09.0', '', 'HDR10'),
            ('vp09.02.40.10', cicp_properties(18), 'HLG'),
            ('vp09.02.40.10', cicp_properties(16), 'HDR10'),
        ):
            with self.subTest(codec=codec, expected=expected, properties=bool(properties)):
                self.assert_hdr_kind(video_manifest(codec, properties=properties), expected)

    def test_hdr_text_in_adaptation_base_url_is_not_metadata(self):
        for url in (
            '<BaseURL>https://cdn.example/hdr/</BaseURL>',
            '<BaseURL>https://cdn.example/hlg-pq/</BaseURL>',
        ):
            with self.subTest(url=url):
                self.assert_hdr_kind(video_manifest(
                    'vp09.02.40.10', adaptation_url=url), None)


if __name__ == '__main__':
    unittest.main()
