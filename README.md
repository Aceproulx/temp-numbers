# temp-numbers

A CLI tool to fetch SMS messages from free temporary phone numbers on [esimplus.me](https://esimplus.me).

## Features

- **Lists cached numbers** without opening a browser
- **Fetches SMS messages** directly via the site's API using saved cookies/Cloudflare clearance
- **Automatic browser fallback** only when clearance is missing or expired (uses Playwright + Xvfb)
- **Caches results** for 24 hours to avoid unnecessary requests
- **No browser needed for normal use** once configured

## Requirements

- Python 3.8+
- Linux (tested on Linux with X11/Xvfb)
- For first-time setup with browser fallback: `playwright`, `chromium`, and `xvfb`

## Installation

1. Clone the repository:

```bash
git clone https://github.com/Aceproulx/temp-numbers.git
cd temp-numbers
```

2. Install Python dependencies:

```bash
pip install playwright
```

3. (Optional) Install browser dependencies upfront:

```bash
python sms_fetcher.py --install
```

The script will automatically install missing dependencies on first run if needed.

## How it works

1. **Number discovery**: Scrapes the front page of esimplus.me's temporary numbers list and caches them to `numbers.json` (also writes `numbers.noise` for legacy use). The cache is valid for 24 hours by default.
2. **Cloudflare clearance**: Uses Playwright to obtain Cloudflare clearance when needed. The clearance and cookies are saved to `clearance.json` and `cookie.http` for reuse.
3. **Direct API calls**: Once clearance/cookies are cached, `-n` and `--all` make direct `curl`/API requests without launching a browser.
4. **Smart fallback**: If the saved clearance is rejected (403/challenge), the script invalidates it and launches the browser to obtain fresh clearance.

## Usage

### List available numbers (no browser if cached)

```bash
python sms_fetcher.py --list
```

### Fetch messages for a specific number

```bash
python sms_fetcher.py -n "+1 289-268-7292"
# or without the + sign
python sms_fetcher.py -n "12892687292"
```

### Fetch messages for all cached numbers

```bash
python sms_fetcher.py --all
```

### Force refresh

```bash
# Refresh number list
python sms_fetcher.py --refresh

# Or use --xnumbers
python sms_fetcher.py --xnumbers
```

### Use with saved cookies only (no browser)

```bash
python sms_fetcher.py --no-browser -n "12892687292"
```

### Options

- `--count N` - Number of messages to fetch per number (default: 10)
- `--ttl MINUTES` - Cache TTL for numbers list (default: 1440 = 24 hours)
- `--cookie "..."` - Override cookie header for curl mode
- `-v, --verbose` - Show debug output
- `--show` - Show browser window (default runs hidden on Xvfb)
- `--install` - Install dependencies and exit

## Generated files (not tracked in git)

- `numbers.json` - Cached list of available numbers with metadata
- `numbers.noise` - Legacy plaintext format
- `clearance.json` - Saved Cloudflare clearance cookies
- `cookie.http` - Cookie header for curl/API use
- `.pw-profile/` - Playwright browser profile
- `.deps-ok` - Dependency check marker

## Notes

- The number list is only scraped from the front page (deeper pages are behind Cloudflare captcha)
- After obtaining clearance once, subsequent runs use direct API calls without opening a browser
- If you get a 403, the clearance may have expired - the script will handle this automatically on next run
