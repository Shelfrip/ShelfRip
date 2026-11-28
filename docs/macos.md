# macOS support

macOS support is experimental and currently runs from a source checkout. It builds
on the shared platform work introduced for Linux. It is not yet Windows feature
parity or a packaged Mac release.

## Running

Install Python 3.10 or later and the macOS version of MakeMKV. The Python supplied
by some macOS developer-tool installations is older; check `python3 --version`.
No third-party Python packages are required. Shelfrip discovers MakeMKV in
`/Applications/MakeMKV.app` or `~/Applications/MakeMKV.app`, on PATH, or at the
executable path selected in Settings. Configure MakeMKV's licence separately.

```sh
python3 app.py
```

The interface opens in the default browser. Closing its tab leaves Shelfrip
running; use the page's Quit button to stop it. `python3 app.py --detach` starts
the server independently of the terminal and prints its URL and private log path.
`--background` suppresses the browser but stays in the foreground. A second launch
reuses the existing server. Restart preserves the port and settings. There is no
native app window or menu-bar icon yet; browser tab reuse depends on the browser.

Settings, logs, catalog and caches live in
`~/Library/Application Support/Shelfrip`. `SHELFRIP_DATA` overrides this location
for testing or a separate setup. Choose separate rip and library folders, for
example `~/Ripped` and `~/Movies/Library`. Allow access to your chosen folders and
removable volumes when macOS requests it. Do not run Shelfrip as root.

Optional tools include MKVToolNix, Tesseract, a native `thediscdb-scan` executable,
and a macOS-compatible cyanrip build. Install tools on PATH or configure their
paths in Settings. These tools are not bundled. Audio-CD ripping with cyanrip
still needs validation against a real disc and Mac build.

## Drives and disc metadata

Discovery uses Apple's DiscRecording framework and cached IOKit media information.
It can show an empty external drive without running a MakeMKV scan every poll.
Each drive has a `macos:…` ID derived from its I/O Registry path. The ID stays
stable across eject/reinsert while the drive remains on the same connection;
moving it to another USB port can change it. Use that ID in a drive filter or
per-drive offset setting, such as `macos:0123456789abcdef01234567=6`.

Before ripping, Shelfrip resolves that ID to the current optical media device,
rather than assuming its enumeration index matches MakeMKV's. Ejection only
accepts a currently enumerated optical drive; it never ejects an arbitrary disk.
Blank media and media still being inserted are not offered for ripping.

Mounted DVD/Blu-ray roots come from `diskutil`'s structured property lists,
including child partitions. Their fingerprints feed the existing identification
and saved metadata workflow. The optional native TheDiscDB scanner uses the same
CLI contract and process supervision as the other platforms. Unmounted or
inaccessible media can still rip; mounted-disc metadata is skipped. Shelfrip does
not mount volumes or change permissions itself.

Audio-CD detection reads the cached IOKit TOC. Multi-session track data is passed
through the shared MusicBrainz parser, including enhanced-CD data-track exclusion.
CD-Text is not implemented yet. Physical audio-CD validation remains outstanding.

MakeMKV, cyanrip and scanner processes run under a supervisor. Cancellation,
shutdown, terminal signals and abrupt parent death stop their owned process groups.
Partial rips stay available for retry.

## Passwords and desktop actions

OpenSubtitles passwords are stored as generic-password items in macOS Keychain
through Security.framework. Settings contain only an opaque reference. Passwords
are never passed through process arguments or saved as plaintext. Unlock the login
keychain before saving or using them; a locked or unavailable keychain produces
an error instead of waiting for an invisible background prompt.

Replacement verifies the new item before saving its reference, rolls it back if
saving settings fails, and removes the old item after success. Settings also offers
password removal. Polling settings never retrieves a password or unlocks Keychain.
Other API keys remain in the settings file, which is saved with mode 0600.

Browse uses native macOS file/folder dialogs, Play and folder opening use `open`,
and optional notifications use the system AppleScript notification command.
Dialogs and notifications require a graphical login session and appropriate
system permissions. Paths can still be entered manually. Notification delivery
depends on macOS notification settings.

## Validation and remaining work

Run `python3 tests/run_all.py`. CI includes macOS alongside Linux and Windows.
The macOS regression tests cover drive identity, media changes, ejection guards,
partitioned mounts, fingerprints, multi-session audio TOCs, native-command
argument handling and password save/rollback/removal.

On macOS, the suite additionally reads real native drive/process information and
creates a temporary Keychain for password round-trip, lock/unlock and deletion
tests. It restores the original Keychain search list and deletes the test keychain.
No test reads the user's saved passwords, ejects a drive or starts a real rip.
Subprocess tests exercise detached startup, duplicate launches, same-port restart,
quit, cancellation, terminal signals and forced-parent-death cleanup.

Development validation used an Apple Silicon Mac running macOS 26.5.2, Python
3.12, and an attached ASUS SDRW-08U9M-U drive. Native discovery reported the empty
drive, and a separate MakeMKV 2.0.0 command-line discovery check found the same
drive. There was no disc inserted, so this does not validate title reads, ripping,
mounted optical filesystems, physical ejection or the complete review/file workflow.

Still needed:

- Real DVD, Blu-ray and audio-CD tests, including cancellation, retry and filing.
- Validation with a native TheDiscDB scanner and cyanrip build.
- Interactive picker, notification and media-player checks in the Mac desktop.
- A reusable native window and menu-bar/background integration.
- A macOS app package, signing/notarization, updates and optional launch at login.
- Intel Mac and older macOS version coverage before defining a supported range.
