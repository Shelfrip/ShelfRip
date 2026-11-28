# Shelfrip

[![Latest release](https://img.shields.io/github/v/release/Shelfrip/ShelfRip?label=download&color=6C9BFF)](https://github.com/Shelfrip/ShelfRip/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/Shelfrip/ShelfRip/total?color=4CC38A)](https://github.com/Shelfrip/ShelfRip/releases)
![Windows 10 and 11](https://img.shields.io/badge/Windows-10%20%7C%2011-0E1014)
[![Tests](https://github.com/Shelfrip/ShelfRip/actions/workflows/tests.yml/badge.svg)](https://github.com/Shelfrip/ShelfRip/actions/workflows/tests.yml)
[![MIT License](https://img.shields.io/github/license/Shelfrip/ShelfRip?color=F2B33D)](LICENSE.txt)
[![Ko-fi](https://img.shields.io/badge/Ko--fi-support%20Shelfrip-FF5E5B?logo=ko-fi&logoColor=white)](https://ko-fi.com/shelfrip)

Rip your DVDs, Blu-rays and 4K discs, and Shelfrip names everything for Plex, Jellyfin, Emby or
Kodi: the movie, every episode in the right order, and the special features.

Put a disc in. Shelfrip rips it with MakeMKV, works out what's on it, and shows you the names it
picked. Check them, click **File**, and it lands in your media library exactly where your media
server expects it.

**[Download Shelfrip for Windows](https://github.com/Shelfrip/ShelfRip/releases/latest)** · free, no account, nothing to subscribe to.

![The Home tab: discs filed this week, today's goals and the discs waiting for review](docs/home.png)

## What it does

- **Rips without re-encoding.** Every title worth keeping is copied as-is (a "remux"), so there's no
  loss of quality. It can rip from several drives at once, and with auto-rip on it starts as soon as
  a disc goes in.
- **Works out what's on the disc.** Movie or TV, which film or show, which title is the main feature,
  which are episodes and in what order, and which are extras.
- **Names the special features** by matching run times against [TheDiscDB](https://thediscdb.com/),
  the Shelfrip disc database and (if you turn it on) DVDCompare, and sorts them into Featurettes,
  Deleted Scenes, Behind the Scenes and so on.
- **Skips the clutter.** Menus, copyright warnings, promos, trailers for other films and "Play All"
  titles are set to Skip for you.
- **Rips each movie and episode only once (optional).** Some discs list the movie several times, often
  as decoy playlists, or offer every scene of it as a separate title. TV Blu-rays often list each
  episode twice (as a playlist and as its raw video file), and TV DVDs add a "Play All" of every
  episode. Turn on "Rip each movie or episode only once" in Settings and copies that are the same
  video and tracks, copies a disc database says aren't the real one, scene-by-scene titles and TV
  "Play All" titles whose episodes are on the disc are left out before ripping. Different cuts,
  language versions and named extras are always ripped, and a TV disc with only a "Play All" keeps it
  so it can be split into episodes.
- **Imports videos you ripped before.** On the Review tab, click "Import a folder" and pick a folder
  of MKVs (from MakeMKV on its own, or anything else). Shelfrip moves them into its rip folder (or
  copies them, from another drive), reads each file's length and tracks with MKVToolNix, and names
  them from the folder's name like a fresh rip, ready for review.
- **Lets you choose the titles first (optional).** Turn on "Let me choose the titles before each rip
  starts" and, once a disc is read, its drive card lists every title with the ones Shelfrip would rip
  already ticked. Tick or untick any, then press Rip.
- **Sets scene-by-scene titles to Skip.** Even with that setting off, unnamed titles that are just
  scenes of the movie (made only of its video, or carrying exactly its soundtracks and subtitles)
  start as Skip on the Review tab.
- **Splits TV discs** where several episodes were mastered as one long video, by chapter.
- **Checks episode numbers against the dialogue (optional).** Turn on "Check episode numbers
  against the subtitles" (Settings > Names and lookups) and, after a TV disc is named, Shelfrip
  reads each episode's subtitles and matches the whole disc to the season at once, clip shows
  first, so discs that list episodes out of order and folders of files in any order get the right
  numbers. It only reads. Episodes whose dialogue says another number show it on the Review tab with
  a button to use it, and the disc isn't filed automatically. Blu-ray and DVD subtitles are pictures,
  so this needs the free [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki)
  (`winget install UB-Mannheim.TesseractOCR`). For much stronger matches, also turn on "Compare with
  subtitles from OpenSubtitles.com" and sign in with a free account (20 downloads a day): each
  episode's subtitles are downloaded once, kept on your PC, used only for matching, and never shared
  or added to your rips.
- **Keeps every audio and subtitle track**, names commentary tracks, and makes your language the
  default.
- **Lets you check everything first.** Nothing moves until you've seen the names. You can change the
  match, rename an extra, or Keep and Skip titles. Discs that match with full confidence can be filed
  on their own after 15 minutes.
- **Fixes mistakes later.** If something was filed as the wrong movie, or the wrong show or season,
  pick the right one on My shelf and Shelfrip moves and renames the files.
- **Skips what you already have.** A disc that's already in your library in the same or better
  quality isn't ripped again.
- **Rips music CDs** too, to FLAC with cover art and lyrics, named from MusicBrainz.
- **Makes it a bit of a game.** Every disc you file earns XP towards your next rank, with daily goals,
  streaks and badges, and your collection fills a shelf.

## macOS (experimental)

The source version includes native optical-drive discovery, mounted-disc metadata,
Keychain password storage, macOS file/folder actions and supervised background
operation. Install Python 3.10+ and MakeMKV, then run `python3 app.py`.
See [macOS setup and validation](docs/macos.md) for dependencies and the remaining
work. A packaged Mac app and full hardware validation are not available yet.

## Linux (experimental)

Linux support includes drive discovery and ejection, audio-CD table-of-contents reads,
native file/folder dialogs, file playback, desktop notifications, a tray menu and
desktop keyring storage for OpenSubtitles passwords, mounted-disc fingerprints and
optional native TheDiscDB scanner integration. Python 3.10+ is
required, along with Linux MakeMKV for DVDs/Blu-rays and cyanrip for audio CDs.

When a release includes `Shelfrip-VERSION-linux.tar.gz`, extract it and run
`python3 shelfrip/linux_install.py` to install for your user account. This adds an
app-menu launcher, optional start at login and verified in-app updates. Python 3.10+
and the native tools are still required.

From source, run `python3 app.py`, then open `http://127.0.0.1:8765`, or use
`python3 app.py --detach` to run in the background. The tray and keyring need the
appropriate desktop services and libraries. See the
[Linux setup and testing guide](docs/linux.md) for distribution-independent setup,
hardware checks and the remaining differences from Windows. Linux support is still
experimental; automated tests do not substitute for testing real discs and drives.

## Quick start (Windows 10 or 11)

1. **Install [MakeMKV](https://www.makemkv.com/download/).** It does the actual ripping. It's free
   while in beta, but needs a key: copy the current one from the
   [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?t=1053) into MakeMKV under
   Help › Register. You can also buy a permanent licence.
2. **Download Shelfrip Setup** from [Releases](https://github.com/Shelfrip/ShelfRip/releases) and
   run it. It doesn't need admin rights.

   Shelfrip isn't code-signed yet, so Windows may say **"Windows protected your PC"**. Click
   **More info**, then **Run anyway**. If you'd rather not install anything, the portable zip on the
   same page works too. It keeps its settings in its own folder (that's what its `portable.txt` does),
   so it can live on a USB stick.
3. **Open Shelfrip and answer the setup questions:**
   - which media server you use,
   - your media library folder (the one that holds `Movies` and `TV Shows`),
   - a rip folder with room to spare (about 50 GB per Blu-ray and 100 GB per 4K disc while it works).

   You don't need a TMDB key: Shelfrip has a shared one. You can add your own (see below).
4. **Put a disc in** and click **Rip loaded discs now** (or turn on Auto-rip). When the rip finishes,
   the disc shows up under **Review**.

Optional extras: [MKVToolNix](https://mkvtoolnix.download/downloads.html) to split TV discs, and
[cyanrip](https://github.com/cyanreg/cyanrip/releases) for music CDs. Settings › Programs shows
what Shelfrip has found.

### Your own TMDB key (optional, 2 minutes, free)

TMDB is where Shelfrip gets movie and episode names. Out of the box, Shelfrip asks TMDB through its
own server, which adds Shelfrip's shared key; the server passes the search on and keeps only a day's
cache of TMDB's answers and a daily count of calls per IP (as a salted hash). With your own key, Shelfrip asks TMDB directly instead, and you're
never held up if the shared key is busy. To get one:

1. Make an account at [themoviedb.org](https://www.themoviedb.org/signup).
2. Go to [Settings › API](https://www.themoviedb.org/settings/api) and click **Create** (or "click here"
   under Request an API Key). Choose **Developer**, accept the terms and describe it as personal
   use, for example "Naming my own disc backups".
3. Copy the **API Key** (the shorter one, not the long "Read Access Token") into Shelfrip.

### Other keys (optional)

- **OMDb** ([omdbapi.com/apikey.aspx](https://www.omdbapi.com/apikey.aspx), free tier): a second
  check of a movie's year and running time.
- **Plex**: to have Shelfrip refresh Plex and check that each disc was matched, add your server's
  address and token in Settings › Plex.
  [How to find your Plex token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).

## Checking a disc

![The Review tab: each title on the disc with Keep or Skip, its type and its name](docs/review.png)

Each ripped disc gets a card with the box art. If the cover isn't the disc in your hand, click
**No, pick the right one**. Each title gets a row:

- **Keep** or **Skip.** Skipped titles are deleted once the disc is filed.
- **Type:** the movie, an episode, or a kind of extra.
- **Name:** what the file will be called. The full path in your library is shown underneath.

The ▶ button plays a title in your video player, if you want to see what it is. When the names look
right, click **File N titles into Plex** (or your media server). The files are copied into your library and checked, the rip is cleared
out, and the disc is ejected.

## My shelf

![My shelf: your filed discs as cases on a shelf, with format bands](docs/shelf.png)

Everything you've filed, as cases on a shelf. Discs from the same season sit together as a box set.
Click a case for its details. **Wrong movie?** and **Wrong show or season?** are there too, if
something was filed under the wrong name.

## Media servers

Pick yours during setup. Ripping and review work the same either way; only the names differ.

| Server   | Folder              | Another cut of a film             | Extras                         |
|----------|---------------------|-----------------------------------|--------------------------------|
| Plex     | `Alien (1979) {tmdb-348}`   | `Alien (1979) {tmdb-348} {edition-Director's Cut}` | `Featurettes\`, `Deleted Scenes\`, ... |
| Jellyfin | `Alien (1979) [tmdbid-348]` | `Alien (1979) [tmdbid-348] - Director's Cut`       | same folders as Plex           |
| Emby     | `Alien (1979) [tmdbid=348]` | `Alien (1979) [tmdbid=348] - Director's Cut`       | same folders as Plex           |
| Kodi     | `Alien (1979)`              | `Alien (1979) - Director's Cut`                    | `Extras\`                      |

TV shows go in `TV Shows\Show (Year)\Season 1\Show (Year) - s01e01 - Title.mkv`. Shelfrip
recognises an existing library whichever style it uses. You can also keep each format in its own
folder (`Movies\Blu-Ray\...`, `Movies\DVD\...`).

On Plex, Jellyfin and Emby you can also file special features tagged in the file name, next to the
film, instead of in folders: `Making Of-featurette.mkv`, `Teaser-trailer.mkv` (Settings › Library
and folders › How special features are filed).

Already have movie and TV folders with other names, like `movies`, `Films` or `TVShows`? Shelfrip
finds them whatever their case or spacing and files into them. To pick them yourself, use
**Settings › Library and folders › Movies folder / TV shows folder**. They can be anywhere, even on
different drives. Music CDs work the same way: an
existing `music` folder is found, or choose any folder under **Music folder**.

Keep more than one library, such as 4K films in their own folder for a separate Plex server? Turn on
**Settings › Library and folders › Use multiple libraries** and add each other library's folder.
Tick the formats that should go there by default (4K discs, say). Each disc on the Review tab then
has a **Library** menu next to File, so you can send any disc to any library.

## Good to know

- Shelfrip opens in its own window. Closing the window keeps it running as an icon by the clock, so
  discs keep ripping. Click the icon to open it again, or right-click it to pause auto-rip or quit.
- Behind the window is a small web server at `http://127.0.0.1:8765` that only your own PC can reach.
- Your settings, your disc history and a daily backup are kept in `%APPDATA%\Shelfrip`.
- Shelfrip checks for new versions and can install them for you (Settings › Shelfrip).

## Troubleshooting

**"No disc drives found".** Check that MakeMKV is installed and that Settings › Programs found it.
If MakeMKV itself says its key has expired, copy the new beta key from the forum (step 1 above).

**It picked the wrong movie or show.** Before filing, click **No, pick the right one** on the card.
After filing, open the disc on My shelf and use **Wrong movie?** or **Wrong show or season?**.

**An extra has a name like "Extra 03 (2m31s)".** Nothing could name it. Play it, then type a name or
set it to Skip.

**Anything else.** Click **Feedback** at the top of the window. For a problem, Settings › Shelfrip ›
**Make a problem report** saves a zip of the logs, with your keys removed, which you can attach to
a [GitHub issue](https://github.com/Shelfrip/ShelfRip/issues).

## Feedback

The **Feedback** button at the top of the window sends your idea or problem straight to the people
who make Shelfrip, with no account needed. Leave your email if you'd like a reply. For a problem
(or Other), **Include logs** (ticked unless you untick it) adds the latest rips' logs: MakeMKV's list of
the disc's titles and tracks, the rip and activity logs, the proposed names, the setup check and
Shelfrip's own log, with your user name, keys and tokens left out. **See what's sent** shows it first.
You can also email
[support@shelfrip.com](mailto:support@shelfrip.com) or open
an issue on [GitHub](https://github.com/Shelfrip/ShelfRip/issues/new/choose).

- **A disc was named wrong?** [Tell us which one](https://github.com/Shelfrip/ShelfRip/issues/new?template=1-wrong-name.yml).
  These reports make the biggest difference.
- **Questions, ideas, or your shelf to show off?** Post in [Discussions](https://github.com/Shelfrip/ShelfRip/discussions).
- **Want to help?** See [CONTRIBUTING.md](CONTRIBUTING.md). You don't need to write code.
- **News and new versions:** follow [@shelfrip.com](https://bsky.app/profile/shelfrip.com) on Bluesky.

## Support Shelfrip

Shelfrip is free and always will be. If it saves you time, you can
[buy the project a coffee on Ko-fi](https://ko-fi.com/shelfrip) (it's the ♥ in the app's top bar too).
Donations go towards code signing, so Windows stops warning people when they install it, and towards
running the shared disc database.

## The shared disc database

Every disc you file makes the next person's rip of the same disc more reliable. When sharing is on
(you're asked during setup, and it's in Settings), Shelfrip sends what each title on the disc turned
out to be: the movie, which episode, the extra's name and category, or that you skipped it. When
anyone rips that disc later, Shelfrip asks the database first and uses the answer most people agreed
on. An answer that only one person has given yet is still used, but the review page says so and the
disc waits for you instead of being filed automatically. If you file a disc as something other than
the shared answer, that counts against the answer, so a wrong entry gets corrected.

Each title on the review page shows how sure the database is about it. Filing a title exactly as the
shared answer showed it counts as half a vote, so people clicking through without checking can't
confirm an answer on their own; anything you changed or identified yourself counts fully.

Only the disc's fingerprint, its label, the title lengths and sizes, each title's tracks (video
format, audio and subtitle languages, commentary names), the names chosen, and whether you changed
what Shelfrip suggested (so its wrong guesses can be found and fixed) are sent.
Nothing about you, your files or your library is sent. Your install is identified by a random id,
which is stored only as a salted hash. The whole database can be downloaded (CC0) from `/v1/export`.
The server code, and how answers are decided, are in [server/](server/).

## Where the names come from

- [TMDB](https://www.themoviedb.org/): movie and episode names and numbers (always the main source).
  This application uses TMDB and the TMDB APIs but is not endorsed, certified, or otherwise approved by TMDB.
- [OpenSubtitles.com](https://www.opensubtitles.com/) (optional, your own free account): episode
  subtitles to check which episode a video is. Never used for names.
- [TheDiscDB](https://thediscdb.com/): recognises specific discs and names their titles and extras.
- [TVmaze](https://www.tvmaze.com/) and, with your own key, [OMDb](https://www.omdbapi.com/): second opinions.
- [DVDCompare](https://www.dvdcompare.net/): extras lists by release. Off by default; turn it on in Settings.
- [MusicBrainz](https://musicbrainz.org/), Cover Art Archive and [LRCLIB](https://lrclib.net/) for music.

## Run from source

You need Python 3.10 or newer. Only the Python standard library is used. Windows
is the original supported platform; experimental Linux setup is described above.

```
python app.py
```

You can also double-click `Run Shelfrip.bat`. To build the installer yourself, run
`Build Installer.bat`. The Setup exe needs [Inno Setup 6](https://jrsoftware.org/isdl.php); without
it you get the portable zip. Tests: `python tests\run_all.py`.

## Legal

Shelfrip doesn't decrypt anything. MakeMKV does the ripping, and you install and license it
yourself. Shelfrip is for organising backups of discs you own. Copyright law differs between
countries, and you are responsible for following the law where you live.

Licensed under the [MIT License](LICENSE.txt). See [THIRD-PARTY-NOTICES.txt](THIRD-PARTY-NOTICES.txt).
