# yt-dlp Instagram HDR

A [yt-dlp](https://github.com/yt-dlp/yt-dlp) plugin that adds HDR formats for Instagram Reels, Stories, and video posts.

Supports VP9 Profile 2 and AV1 10-bit HDR. Video and audio are downloaded by yt-dlp,
merged without re-encoding, and verified with `ffprobe`.

## Requirements

- Python 3.10+
- yt-dlp 2026.08.19+
- FFmpeg
- Authenticated Instagram cookies

## Installation

### pip

```shell
python3 -m pip install -U https://github.com/viniciusron7/yt-dlp-instagram-hdr/archive/master.zip
```

### pipx

```shell
pipx inject yt-dlp https://github.com/viniciusron7/yt-dlp-instagram-hdr/archive/master.zip --force
```

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

## License

[MIT](LICENSE) © 2026 Vinicius Roncetti
