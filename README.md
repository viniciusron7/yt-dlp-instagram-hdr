# yt-dlp Instagram HDR

A [yt-dlp](https://github.com/yt-dlp/yt-dlp) extractor plugin for downloading the HDR variants of Instagram Reels and video posts.

The plugin reads Instagram's authenticated iOS media response, exposes VP9 Profile 2 and AV1 10-bit HDR formats, lets yt-dlp select the best video and audio, merges them without re-encoding, and verifies the finished file with `ffprobe`.

> [!WARNING]
> This plugin relies on an undocumented Instagram API that may change without notice.

## Requirements

- Python 3.10 or newer
- yt-dlp 2026.08.19 or newer
- `ffmpeg` and `ffprobe` available in `PATH`
- Authenticated Instagram cookies for HDR extraction

No Instaloader, Selenium, Playwright, browser-cookie package, or project-local virtual environment is required.

## Installation

If yt-dlp was installed with `pip`:

```shell
python3 -m pip install -U \
  https://github.com/viniciusron7/yt-dlp-instagram-hdr/archive/master.zip
```

For a `pipx` installation:

```shell
pipx inject yt-dlp \
  https://github.com/viniciusron7/yt-dlp-instagram-hdr/archive/master.zip \
  --force
```

For standalone yt-dlp builds, place the release `.whl` in a [yt-dlp plugin directory](https://github.com/yt-dlp/yt-dlp#installing-plugins), such as:

```text
~/.config/yt-dlp/plugins/       # Linux and macOS
%APPDATA%\yt-dlp\plugins\       # Windows
```

The wheel is loaded directly and must not be extracted. Run `yt-dlp -v` and look for `InstagramHDRIE` under `Extractor Plugins` to verify the installation.

## Usage

Pass cookies from a browser where you are logged into Instagram:

```shell
yt-dlp --cookies-from-browser chrome \
  "https://www.instagram.com/reel/SHORTCODE/"
```

List the available HDR formats:

```shell
yt-dlp --cookies-from-browser chrome -F \
  "https://www.instagram.com/p/SHORTCODE/"
```

A Netscape-format cookie file also works:

```shell
yt-dlp --cookies cookies.txt \
  "https://www.instagram.com/reel/SHORTCODE/"
```

Always quote URLs containing `&` so the shell does not split them. By default, yt-dlp downloads the best HDR video and the best audio, then merges both streams without re-encoding.

If no authenticated cookies are supplied, the plugin prints a warning and delegates to yt-dlp's built-in Instagram extractor. The command still works, but only the formats normally available to the built-in extractor—usually SDR—are shown.

## Supported HDR

- VP9 Profile 2 or AV1
- 10-bit pixel format
- BT.2020 color primaries
- HLG (`arib-std-b67`) or PQ (`smpte2084`)

The final file is checked automatically with `ffprobe`. Verification can be disabled with:

```shell
yt-dlp --cookies-from-browser chrome \
  --extractor-args "instagramhdr:verify=false" \
  "https://www.instagram.com/reel/SHORTCODE/"
```

## Notes

- Supported routes are `/p/`, `/reel/`, and `/reels/`.
- The source must have an HDR representation available to the selected account.
- Supplied but expired or rejected cookies produce an authentication error; log into Instagram again and retry.
- Browser cookies grant access to your account. Never publish them or include them in bug reports.
- Dolby Vision is not currently supported.

## Development

A virtual environment is optional for development. Use any Python environment that has yt-dlp installed, and do not commit or distribute the environment itself.

```shell
PYTHONPATH="$PWD" yt-dlp --ignore-config -v --simulate \
  "https://www.instagram.com/reel/SHORTCODE/"

python3 -m unittest discover -s tests -v
python3 -m pip wheel --no-deps . -w dist
```

See the official [plugin documentation](https://github.com/yt-dlp/yt-dlp#plugins) and [sample plugin package](https://github.com/yt-dlp/yt-dlp-sample-plugins) for more details.

## License

[MIT](LICENSE) © 2026 Vinicius Roncetti
