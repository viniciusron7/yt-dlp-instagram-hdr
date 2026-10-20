# yt-dlp Instagram HDR

A [yt-dlp](https://github.com/yt-dlp/yt-dlp) plugin that adds HDR formats for Instagram Reels, Stories, and video posts.

Supports VP9 Profile 2 and AV1 10-bit HDR. By default, the plugin uses Instagram's
web API and builds HDR, SDR, progressive video and audio formats from the same
media response, avoiding a second authenticated extraction. xHE-AAC is excluded
unless explicitly enabled, or unless it is the only audio available.
Video and audio are merged without re-encoding (unless the `hevc` option is enabled)
and the HDR video is verified with `ffprobe`.

## Requirements

- Python 3.10+
- yt-dlp 2026.08.19+
- FFmpeg and `ffprobe` on your PATH (the executables, not the Python `ffmpeg` package)
- Authenticated Instagram cookies for the plugin's API extraction

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

Authenticated downloading can expose the account to challenges, session
invalidation or suspension. A yt-dlp maintainer explicitly documents this
[risk of flagged or banned accounts](https://github.com/yt-dlp/yt-dlp/issues/17074).
The plugin cannot guarantee that an account will not be suspended, and cannot
determine which browser account is your main account. Avoid supplying a session
for an account you cannot afford to lose. A separate browser profile using the
same account still exposes that account.

For public content, you can first try without authenticated cookies:

```shell
yt-dlp "VIDEO_URL"
```

This uses yt-dlp's built-in extractor; available formats and anonymous access
depend on Instagram. For authenticated extraction, choose the browser/profile
deliberately and log in there first. Replace `VIDEO_URL` with a Reel, post or
Story URL, keeping the quotes. `--cookies-from-browser chrome` is still supported;
to select a Chrome profile explicitly, use `--cookies-from-browser
"chrome:PROFILE_DIRECTORY"`, replacing `PROFILE_DIRECTORY` with that profile's
directory name or path (shown at `chrome://version`). Replace `chrome` with your
supported browser name if needed. Cookies are credentials, not a protection
against account restrictions.

**Default: download HDR when available, without converting the video codec.**

```shell
yt-dlp --cookies-from-browser chrome "VIDEO_URL"
```

yt-dlp selects the best available formats and merges video/audio when needed.
Merging is not re-encoding: an `.mp4` can still contain VP9 or AV1 video.
The `HDR verified` message is a read-only metadata check, not a conversion; if the
check fails, a warning is printed and the download is kept.

Apple's players (QuickTime, Photos, iPhone and iPad) cannot play VP9 in MP4, which
is how Instagram delivers HDR. For files that play and save on Apple devices,
enable the [optional HEVC conversion](#optional-hevc-conversion).

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

Without authenticated cookies, the plugin shows a warning and delegates to
yt-dlp's built-in Instagram extractor. With authentication, media without HDR
keeps the available SDR and audio formats from the same response.

### Account access and request limits

The default `api=web` uses the web application ID and yt-dlp's browser headers.
It does not assign browser device cookies to a fictitious mobile device, generate
random telemetry, or automatically switch clients. Changing headers cannot make
authenticated automation indistinguishable from normal browsing.

Metadata requests made by this plugin are spaced by two seconds by default,
shared between Reels and Stories in the same yt-dlp run. Use yt-dlp's
`--sleep-requests SECONDS` to set an explicit interval. This limits request bursts;
it is not an Instagram-approved rate or a guarantee against suspension. Avoid
running multiple download processes with the same session.

HTTP 401/403/404/429, login/verification redirects and recognized challenge or
restriction JSON responses stop further metadata requests through this plugin
for the current run, including with `--ignore-errors`. There is no automatic
authenticated fallback after these errors. A 404 can indicate unavailable media
or a session restriction, so it also stops the run's plugin requests. Check the
account in Instagram and resolve restrictions before trying again; repeatedly
refreshing cookies is not a fix for account limits. The stop flag is local to
that yt-dlp process, not a persistent or cross-process account lock. Extractors
outside this plugin, including the explicitly disabled mode, retain yt-dlp's own
behavior.

Stories reuse an existing HDR manifest; a detail request is made only when the
tray omits HDR (a tray's SDR manifest can omit available HDR). With a specific
Story URL and `--no-playlist`, other Stories are
not queried for media details.

Instagram can return different formats for web and mobile clients. For uploads
whose HDR formats are only available through the iOS API, the previous API path
remains available as an explicit compatibility option:

```shell
yt-dlp --cookies-from-browser "chrome:PROFILE_DIRECTORY" --extractor-args "instagramhdr:api=ios" "VIDEO_URL"
```

This option uses a mobile application identity with browser cookies, carries
additional account risk and prints a warning. It retains the request interval
and stop protections. It is never enabled automatically when web HDR is absent.
Both modes preserve the available VP9/AV1 HDR downloads, optional HEVC
conversion, Stories and the audio options below. Availability still depends on
the API response; the plugin cannot force Instagram to expose an HDR rendition.

### Plugin options

Disable the HDR plugin and force yt-dlp's built-in Instagram extractor:

```shell
yt-dlp --cookies-from-browser chrome --extractor-args "instagramhdr:disable" "VIDEO_URL"
```

Include xHE-AAC (`mp4a.40.42`) audio alongside the other audio formats:

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
explicitly enable conversion of HDR downloads to HEVC Main 10 (`hvc1`). The result
is an `.mp4` whatever `--merge-output-format` says; `--remux-video` can still change
its container afterwards. `--recode-video` would re-encode it again, so with it the
conversion is skipped with a warning; likewise with `-x`. This is a lossy
re-encode and takes additional time; it preserves and checks the HDR colour tags,
but is not a lossless copy. VideoToolbox is used when your FFmpeg has it, with
`libx265` as a slower software fallback. Audio, subtitles and cover art are copied:

```shell
yt-dlp --cookies-from-browser chrome --extractor-args "instagramhdr:hevc" "VIDEO_URL"
```

The HEVC file is usually larger than the download (about 1.3–3× in tests). For
smaller files at lower quality, pass encoder options; they are applied after the
plugin's defaults. `-q:v` sets the VideoToolbox quality (default 50, higher is
better) and `-crf` the `libx265` quality (default 22, lower is better):

```shell
yt-dlp --cookies-from-browser chrome --extractor-args "instagramhdr:hevc" --ppa "InstagramHDRHEVC:-q:v 40 -crf 26" "VIDEO_URL"
```

After a successful conversion the original is deleted, like with
`--recode-video`, unless:

- `-k` is used: the original is kept (as `*.orig.mp4` when it was already an MP4),
  together with the separately downloaded video/audio streams (`*.f<format>.*`);
- `--no-overwrites` is used and the original was not downloaded by this run (for
  example a file that yt-dlp reports as already downloaded): it is kept too (as
  `*.orig.mp4` when it was already an MP4).

Running the same command again skips files that are already converted: like
`--remux-video`, an existing `<name>.mp4` (or the container chosen with
`--remux-video`) counts as the finished result. If that
file is an earlier download that is not HDR, a warning says so and it is kept;
pass `--force-overwrites` to download it again. An existing file at the HEVC
destination, or an existing `*.orig.mp4` that would be kept, also stops the
conversion unless `--force-overwrites` is given.

The option only affects HDR video selections; it does not turn SDR into HDR, and
it is skipped (with a warning) when several video streams are merged with
`--video-multistreams`. If your `-f`/`-S` options pick an SDR format although HDR
formats exist, a warning says so before the download. Audio-only downloads,
including `-x`, skip HDR conversion and verification. The option does not convert
xHE-AAC audio, so enabling both options can still produce audio that your player
cannot play.

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

The options are stored in the info JSON when the video is extracted. When
downloading from `--load-info-json`, no extraction runs, so add the plugin's
post-processors yourself:

```shell
yt-dlp --load-info-json FILE --use-postprocessor "InstagramHDRSelection:when=video" --use-postprocessor InstagramHDRHEVC --use-postprocessor "InstagramHDRVerify:when=after_move"
```

## Troubleshooting

- **No HDR formats:** check that the selected browser/profile is logged in and
  that the upload has HDR. Available SDR/audio formats are kept. Web and iOS can
  expose different formats; see the explicit iOS option and its account risk above.
- **HTTP 401/403/404/429 or account verification:** the plugin stops its metadata
  requests for that run. Check the account and content directly in Instagram;
  resolve challenges and allow limits to clear before another attempt. Neither
  switching clients nor disabling the plugin bypasses Instagram's restrictions.
- **Missing FFmpeg/ffprobe or HEVC encoder:** install the executables, check
  `ffmpeg -version` and `ffprobe -version`, or set `--ffmpeg-location "PATH"`.
  `ffmpeg -hide_banner -encoders` must list `hevc_videotoolbox` or `libx265`.
- **SDR downloaded although HDR formats are listed:** check `-f`/`-S` in your
  command and configuration; `-f "bv*[dynamic_range!=SDR]+ba/b"` prefers HDR.
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
