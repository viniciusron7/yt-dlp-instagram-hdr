# yt-dlp Instagram HDR

A [yt-dlp](https://github.com/yt-dlp/yt-dlp) plugin that adds HDR formats for Instagram Reels, Stories, and video posts.

Supports VP9 Profile 2 and AV1 10-bit HDR. By default, the format list combines
all HDR video formats found by the plugin with all formats returned by yt-dlp's
built-in Instagram extractor. xHE-AAC is excluded unless explicitly enabled.
Video and audio are merged without re-encoding and the HDR video is verified with
`ffprobe`.

## Requirements

- Python 3.10+
- yt-dlp 2026.08.19+
- FFmpeg
- Authenticated Instagram cookies

## Installation

First, identify how yt-dlp was installed.

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

## Usage

```shell
yt-dlp --cookies-from-browser chrome <video_url>
```

List available formats with `-F`:

```shell
yt-dlp -F --cookies-from-browser chrome <video_url>
```

You can also use a cookie file:

```shell
yt-dlp --cookies cookies.txt <video_url>
```

Without authenticated cookies, the plugin shows a warning and falls back to yt-dlp's built-in Instagram extractor.
It also uses the built-in extractor's regular formats when authenticated media has no HDR representation.

### Plugin options

Disable the HDR plugin and force yt-dlp's built-in Instagram extractor:

```shell
yt-dlp --extractor-args "instagramhdr:disable" <video_url>
```

Include the xHE-AAC (`mp4a.40.42`) audio formats found in the HDR manifest:

```shell
yt-dlp --extractor-args "instagramhdr:include_xhe_aac" -F <video_url>
```

These `instagramhdr` options apply to Reels, posts, and Stories. Boolean values
can be disabled explicitly, for example `instagramhdr:disable=false`.

## License

[MIT](LICENSE) © 2026 Vinicius Roncetti
