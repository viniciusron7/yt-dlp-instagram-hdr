# yt-dlp Instagram HDR

A [yt-dlp](https://github.com/yt-dlp/yt-dlp) plugin that adds HDR formats for Instagram Reels, Stories, and video posts.

Supports VP9 Profile 2 and AV1 10-bit HDR. By default, the format list combines
all HDR video formats found by the plugin with all formats returned by yt-dlp's
built-in Instagram extractor. xHE-AAC is excluded unless explicitly enabled.
Video and audio are merged without re-encoding (unless the `hevc` option is enabled)
and the HDR video is verified with `ffprobe`.

## Requirements

- Python 3.10+
- yt-dlp 2026.08.19+
- FFmpeg and `ffprobe` on your PATH (the executables, not the Python `ffmpeg` package)
- Authenticated Instagram cookies for HDR extraction

Optional HEVC conversion requires an FFmpeg build with `hevc_videotoolbox`
(macOS) or `libx265`. No conversion is performed unless you enable it.

## Installation

If you know how yt-dlp was installed, jump to [pip](#pip), [pipx](#pipx), or
[manual installation](#manual) (Homebrew and standalone executables).
Otherwise, identify the installation with the commands below; unavailable
commands can be skipped.

**macOS**

```shell
command -v yt-dlp
pipx list
python3 -m pip show yt-dlp
brew list --versions yt-dlp
```

**Linux**

```shell
command -v yt-dlp
pipx list
python3 -m pip show yt-dlp
```

**Windows (PowerShell)**

```powershell
Get-Command yt-dlp
pipx list
py -m pip show yt-dlp
```

Use the result that matches your output:

| Output                                                       | Installation type |
| ------------------------------------------------------------ | ----------------- |
| `package yt-dlp ...` in `pipx list`                          | [pipx](#pipx)     |
| `Name: yt-dlp` from `pip show`                               | [pip](#pip)       |
| `yt-dlp <version>` from `brew list`                          | [manual](#manual) |
| yt-dlp has a path, but the checks above report it as missing | [manual](#manual) |

`Package(s) not found`, `nothing has been installed with pipx`, and `No such keg`
are negative results; continue to the next check. If `command -v` or `Get-Command`
cannot find yt-dlp, install yt-dlp first.

If more than one check finds yt-dlp, use the manual method to avoid installing the
plugin into the wrong Python environment.

### pip

Run these commands in the same Python environment as yt-dlp.

macOS/Linux:

```shell
python3 -m pip install -U yt-dlp-instagram-hdr
```

Windows:

```powershell
py -m pip install -U yt-dlp-instagram-hdr
```

### pipx

```shell
pipx inject yt-dlp yt-dlp-instagram-hdr --force
```

### Manual

Download the latest `.whl` from [Releases](https://github.com/viniciusron7/yt-dlp-instagram-hdr/releases/latest), and place it, without extracting it, in:

- macOS/Linux: `$XDG_CONFIG_HOME/yt-dlp/plugins/`, or `~/.config/yt-dlp/plugins/` when it is not set
- Windows: `%APPDATA%/yt-dlp/plugins/`

Create the folder if necessary. Keep only one version of this plugin there;
when updating, replace the old `yt_dlp_instagram_hdr-*.whl` with the new wheel.
See yt-dlp's [plugin installation documentation](https://github.com/yt-dlp/yt-dlp#installing-plugins)
for other supported locations.

### Updating

For pip, repeat the installation command with `-U`. For pipx:

```shell
pipx upgrade yt-dlp --include-injected
```

For manual installations, replace the wheel as described above. Updating
yt-dlp itself does not update a manually installed plugin.

## Usage

Log into Instagram in your browser first. Replace `VIDEO_URL` with a Reel, post,
or Story URL, keeping the quotes (URLs can contain shell-special characters).
Replace `chrome` with your supported browser name if needed.

**Default: download HDR when available, without converting the video codec.**

```shell
yt-dlp --cookies-from-browser chrome "VIDEO_URL"
```

yt-dlp selects the best available formats and merges video/audio when needed.
Merging is not re-encoding: an `.mp4` can still contain VP9 or AV1 video.
The `HDR verified` message is a read-only metadata check, not a conversion.

List available formats with `-F`:

```shell
yt-dlp -F --cookies-from-browser chrome "VIDEO_URL"
```

Plugin HDR formats have IDs starting with `hdr-`. To explicitly select separate
video and audio streams, add `-f "bv+ba"`. For silent videos, use the default
selection instead, since an audio-only stream may not exist.

You can also use a cookie file:

```shell
yt-dlp --cookies cookies.txt "VIDEO_URL"
```

The cookie file must use the Netscape format supported by yt-dlp. Treat it as a
password: do not share it or commit it to a repository.

Without authenticated cookies, the plugin shows a warning and falls back to yt-dlp's built-in Instagram extractor.
It also uses the built-in extractor's regular formats when authenticated media has no HDR representation.

### Plugin options

Disable the HDR plugin and force yt-dlp's built-in Instagram extractor:

```shell
yt-dlp --cookies-from-browser chrome --extractor-args "instagramhdr:disable" "VIDEO_URL"
```

Include xHE-AAC (`mp4a.40.42`) audio from **both the native extractor and the
plugin**, without removing the other native audio formats:

```shell
yt-dlp --cookies-from-browser chrome --extractor-args "instagramhdr:include_xhe_aac" -F "VIDEO_URL"
```

Remove `-F` to download. Enabling this option can make yt-dlp select xHE-AAC,
which some players do not support. Leave it off for better compatibility.

### Optional HEVC conversion

HEVC conversion is **disabled by default**: without `instagramhdr:hevc`, the
original video codec is preserved. Merging video/audio and verifying HDR metadata
do not re-encode the video.

For better compatibility with HEVC-capable Apple devices and other players,
explicitly enable conversion of HDR downloads to HEVC Main 10 (`hvc1`) in MP4.
This is a lossy re-encode and takes additional time; it preserves and checks the
HDR colour tags, but is not a lossless copy. VideoToolbox is tried first, with
`libx265` as a slower software fallback. Audio and cover art are copied:

```shell
yt-dlp --cookies-from-browser chrome --extractor-args "instagramhdr:hevc" "VIDEO_URL"
```

Use `-k` to also keep the original video (named `*.orig.mp4` when the input is
already MP4). With `--no-overwrites`, an existing HEVC destination stops the
conversion without replacing either file. Audio-only downloads, including `-x`,
skip HDR conversion and verification.

The option only affects HDR video selections; it does not turn SDR into HDR.
It also does not convert xHE-AAC audio, so enabling both options can still
produce audio that your player cannot play.

### Other controls

These `instagramhdr` options apply to Reels, posts, and Stories. Boolean values
can be disabled explicitly, for example `instagramhdr:hevc=false`. To combine
options, separate them with semicolons inside the same quoted argument:

```shell
yt-dlp --cookies-from-browser chrome --extractor-args "instagramhdr:hevc;include_xhe_aac" "VIDEO_URL"
```

Use the `instagramhdrstory` key to override an option for Stories only.
Advanced: `instagramhdr:verify=false` disables the default read-only HDR check;
HEVC conversion still verifies its input and output.

## Troubleshooting

- **No HDR formats:** check that the browser is logged in and that the upload has
  HDR. The plugin falls back to normal formats when HDR is unavailable.
- **HTTP 401/403:** refresh the Instagram login and cookies. For HTTP 429, wait
  before retrying. Disabling the plugin does not bypass Instagram access limits.
- **Missing FFmpeg/ffprobe or HEVC encoder:** install the executables, check
  `ffmpeg -version` and `ffprobe -version`, or set `--ffmpeg-location "PATH"`.
- **Unexpected conversion:** check your yt-dlp configuration for `hevc` or other
  conversion options. Use `--ignore-config` to test without saved settings.
- **Old plugin still loaded:** remove duplicate copies of this plugin and check
  the plugin paths in `yt-dlp --verbose -F --cookies-from-browser chrome "VIDEO_URL"`.
  A manually installed wheel may take precedence over a pip installation.

When reporting a problem, include the yt-dlp/plugin versions and the command
used. Redact cookies, credentials, private URLs and other personal data from logs.

## Testing a checkout

Run from the repository root with a Python environment containing yt-dlp:

```shell
python -m unittest discover -s tests -v
yt-dlp --ignore-config --no-plugin-dirs --plugin-dirs .. --cookies-from-browser chrome -F "VIDEO_URL"
```

The tests use the local source, not an installed wheel. The second command also
loads this checkout; add `--extractor-args "instagramhdr:hevc"` and remove `-F`
only when you want to test conversion. The automated tests do not contact Instagram.

## License

[MIT](LICENSE) © 2026 Vinicius Roncetti
