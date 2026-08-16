<p align="center">
  <img src="https://github.com/kianfotovat/simple-playify/releases/download/readme-assets/banner.png" alt="Playify banner" width="900">
</p>

# Playify

Playify is a self-hosted Discord music bot for small servers. Give it a song name or a link, and it joins your voice channel with an interactive player for controlling playback and managing the queue.

You run Playify on your own Windows or Linux computer. There is no public Playify bot to invite.

<p align="center">
  <img src="https://github.com/kianfotovat/simple-playify/releases/download/readme-assets/dashboard_preview.svg" alt="Playify dashboard" width="900">
</p>

## Features

- Search for music or play links from YouTube, YouTube Music, SoundCloud, Twitch, and Bandcamp.
- Import tracks, albums, and playlists from Spotify, Deezer, Apple Music, Tidal, and Amazon Music. Playify reads the track information and searches for playable matches.
- Control playback from a message in Discord with buttons for play, pause, previous, skip, volume, shuffle, loop, autoplay, and more.
- Search with artwork and choose a result from a menu.
- Manage the queue with paginated lists and menus for removing tracks or jumping to one.
- Seek with live progress, ten- and thirty-second controls, or an exact timestamp.
- Remember the queue and playback state between restarts.
- Manage setup, settings, logs, updates, and FFmpeg from a terminal dashboard.

## Differences from upstream Playify

This repository is a fork of [alan7383/playify](https://github.com/alan7383/playify). It retains the same basic purpose and most of the same music sources, but deliberately takes a smaller product direction and substantially reworks playback, Discord UI, persistence, and runtime management.

| Area | Upstream Playify | This fork |
|---|---|---|
| Controller | An optional persistent controller assigned to a text channel with `/setup controller` | One controller is managed automatically in the active Voice or Stage channel's text chat; `/nowplaying` recreates it at the bottom |
| Playback lifecycle | Optional 24/7 modes can keep the bot connected indefinitely | When everyone leaves, the connection becomes dormant while the track, position, and queue are retained for later |
| Discord UI | Traditional embeds, interactive views, and `/play` autocomplete | Redesigned controller and command views with artwork, one-second playback/seek updates, automatic queue refreshes, and short-lived responses |
| Server policy | A replace-or-reset channel allowlist | Set/add/remove/clear/show allowlist commands plus `allow` and `protect` policies for moving playback between channels |
| Persistence | Playback and server settings are saved | Full playback persistence or a Settings-only mode that discards playback state |
| Installation and updates | ZIP-based updater, Docker configuration, and Python 3.9+ support | Managed Python 3.11+ environment, managed FFmpeg maintenance, and confirmation-based Git updates; no Docker setup |

### Added or substantially changed in this fork

- `/replay` for restarting the current finite track.
- Dormant sessions that preserve the current track and timestamp without remaining connected to voice.
- Ordered background imports with visible pending counts; unfinished imports can resume after a restart.
- Expanded queue controls, including separate removal and jump menus that update when the queue changes.
- Search results with artwork, duration, uploader information, and automatic dismissal after a selection.
- A controller with artwork, volume controls, one-second timestamps, and no extra confirmation messages from its playback buttons.
- `/setup channelmove` and more granular allowlist management.
- A Settings-only persistence option and a reworked terminal dashboard, maintenance flow, and updater.

### Present upstream but removed from this fork

- Discord file uploads and local-file playback (`/play-files` and the file option on `/playnext`).
- Audio filters such as slowed, nightcore, reverb, and bass boost.
- Lyrics and synced karaoke, including the optional Genius integration.
- 24/7 voice modes; dormant sessions are used instead.
- Kawaii mode and its alternate message set.
- Per-server default volume configuration.
- `/play` autocomplete suggestions and the `/support` command.
- Docker files and the separate documentation website.

## Requirements

- Windows 10/11 x64 or Linux x86-64
- Python 3.11 or newer
- Git
- A Discord bot token

Playify can install and manage FFmpeg for you. Spotify credentials are optional.

macOS and ARM systems are not currently supported.

## Installation

### 1. Create a Discord bot

Open the [Discord Developer Portal](https://discord.com/developers/applications), create an application, and obtain its token from the **Bot** page. Keep this token private: anyone who has it can control your bot.

Discord also has a [beginner guide to creating a bot](https://docs.discord.com/developers/quick-start/getting-started) if you have not done this before.

### 2. Download Playify

```text
git clone https://github.com/kianfotovat/simple-playify.git
cd simple-playify
```

Using Git is recommended because Playify's built-in updater needs a Git checkout.

### 3. Start Playify

On Windows, double-click `start.bat` or run it from a terminal:

```text
start.bat
```

On Linux:

```bash
chmod +x start.sh
./start.sh
```

The launcher prepares a private Python environment, installs Playify's dependencies, and offers to install FFmpeg if needed.

If Playify detects that you are already inside another virtual environment, choose the project environment unless you intentionally want Playify to take ownership of and rebuild that environment.

### 4. Follow the setup wizard

On the first start, Playify asks for your Discord bot token and checks that it works. Spotify credentials can be entered or left blank.

The wizard then displays an invite link. Open that link, add the bot to your server, and return to the dashboard. Playify will start and register its slash commands automatically.

## Using Playify

Join a voice channel, open that voice channel's text chat, and run:

```text
/play Around the World Daft Punk
```

Playify joins the channel and creates a controller message. Most day-to-day actions can be performed from that controller without running another command.

At least one person must be in the voice channel when playback starts. Commands that change playback normally belong in the text chat attached to the Voice or Stage channel.

## Commands

### Adding music

- `/play query` — play a search or link, or add it to the active queue
- `/playnext query` — add a search, track, album, or playlist at the front of the queue
- `/search query` — show ten results and choose one

### Playback

- `/pause` and `/resume`
- `/replay` — restart the current track
- `/seek [timestamp]` — seek directly or open the seek controls
- `/skip` and `/previous`
- `/volume value` — set the volume from 0% to 200%
- `/reconnect` — reconnect a saved session without starting playback
- `/stop` — stop playback and clear the session

### Queue and playback modes

- `/queue` — show the current queue
- `/remove` — choose a queued track to remove
- `/jumpto` — choose a queued track to play next
- `/clearqueue` — clear queued tracks and cancel unfinished imports
- `/shuffle` — shuffle upcoming tracks
- `/loop` — toggle looping for the current track
- `/autoplay [query]` — toggle recommendations or start them from a particular song

### Information and server setup

- `/nowplaying` — move the controller to the bottom of the channel
- `/status` — show the running Playify version and player totals
- `/setup allowlist ...` — choose which server channels may control Playify
- `/setup channelmove ...` — choose whether playback may be moved between voice channels

The `/setup` commands require the **Manage Server** permission. Discord will show the available subcommands and options as you type.

## Supported input

| Input | What Playify does |
|---|---|
| A song, artist, or other search | Searches YouTube, with a SoundCloud fallback |
| YouTube or YouTube Music link | Plays a video, playlist, or mix |
| SoundCloud, Twitch, or Bandcamp link | Plays the supported track, collection, or stream |
| Spotify, Deezer, Apple Music, Tidal, or Amazon Music link | Reads the listed tracks and searches for playable matches |
| Public `.mp3`, `.wav`, `.ogg`, `.m4a`, `.mp4`, `.webm`, or `.flac` link | Streams the file directly |

Spotify links work without Spotify credentials through a fallback, but adding a Spotify client ID and secret generally provides more reliable imports.

## Dashboard

The terminal dashboard starts with Playify and shows whether the bot is online, what is playing, recent logs, and basic resource use.

Its main shortcuts are:

- `C` — edit the Discord and Spotify credentials
- `S` — change Playify settings
- `L` — open the full log viewer
- `U` — check for Playify updates
- `M` — update dependencies or install/update FFmpeg
- `R` — restart the bot
- `Q` — quit Playify

Settings and logs can be opened while the bot is running. The dashboard tells you when a change requires a restart.

## Files and privacy

Playify keeps its configuration, queue, logs, and temporary files inside the project folder:

- `.env` contains the Discord token and optional Spotify credentials.
- `data/` contains settings, saved playback state, cookies, and logs.
- `bin/` contains the managed FFmpeg executable, if installed.

These paths are ignored by Git. Playify has no telemetry or hosted database, and it does not download or keep copies of the music it streams.

By default, queue and playback state are restored after a restart. You can select **Settings only** in the dashboard if you want Playify to remember server settings but discard playback state.

### YouTube cookies

Most YouTube links work anonymously. If YouTube requires an account for something you are allowed to access, export cookies in Netscape `.txt` format and place the file in `data/cookies/`. Playify only tries those files after an anonymous request fails.

## License and origin

Playify is released under the [MIT License](LICENSE) and was originally created by [alan7383](https://github.com/alan7383/playify).
