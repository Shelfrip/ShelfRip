# Linux support

Linux support is experimental. The shared naming, review,
filing and subtitle-processing code remains the same as on Windows.

## Installing, updating and removing

Linux releases can include `Shelfrip-VERSION-linux.tar.gz`. This is an
architecture-independent application package requiring Python 3.10 or later;
it does not bundle Python, MakeMKV or desktop libraries. Download it from the
[project's releases](https://github.com/Shelfrip/ShelfRip/releases), extract it,
then run `python3 shelfrip/linux_install.py`. Use `--autostart` to opt into starting
at graphical login. No source checkout or administrator access is required.

The installer puts versioned program files under `$XDG_DATA_HOME/shelfrip` (normally
`~/.local/share/shelfrip`), a launcher at `~/.local/bin/shelfrip`, and an app-menu
entry under `$XDG_DATA_HOME/applications`. Open **Shelfrip** from your app menu, or
run `~/.local/bin/shelfrip`. The launcher detaches the server and opens its window;
repeat launches open the running instance. Settings > Shelfrip has a **Start Shelfrip
when I sign in** switch. Its XDG autostart entry runs in the background without
opening a window; no particular init system is required.

Installed copies offer **update now** when the upstream release includes a Linux
archive and its SHA-256 digest in GitHub's asset metadata. The updater verifies
the download, safely unpacks and checks the package, then atomically selects the
new version and restarts on the same port. Downloads or validation failures keep
the previous version active. Active work blocks updates; new jobs are held while
updating. Previous version directories are retained so files used by the running
application do not change beneath it. Settings, keyring items and media are kept
separate from installed code. Source checkouts receive the release link instead.

To uninstall, quit Shelfrip and run:

```sh
python3 "${XDG_DATA_HOME:-$HOME/.local/share}/shelfrip/current/linux_install.py" --uninstall
```

This removes installed code and Shelfrip's menu/login launchers while keeping your
settings, keyring and media. Uninstall refuses while an installed instance is running.

Maintainers can build the archive with `python3 build_linux.py`. The Linux package
workflow builds a reviewable artifact for pull requests and attaches it, with a
checksum file, when a matching-version GitHub release is published. A Linux asset
must actually be published before users can download or update to that release.

## Running

Install Python 3.10 or later and the native Linux version of MakeMKV, including
`makemkvcon`. Configure MakeMKV's licence separately. Put `makemkvcon` on PATH or
select its full executable path under Settings > Programs.

From the checkout:

```sh
python3 app.py
```

The interface uses a reusable GTK3/WebKitGTK app window when available, otherwise
Chromium/Chrome app mode or your default browser. You can also open
`http://127.0.0.1:8765` yourself. Keep the terminal
running, or use `python3 app.py --detach` to start an independent background process.
The launcher prints the actual URL and log path, then exits. A second launch reuses
the running instance. Logs go to `shelfrip.log` in the settings folder with mode 0600.
`python3 app.py --background` suppresses the browser but stays in the foreground,
which is useful when launching through a service manager of your choice.
Use the page's Quit button or the desktop tray menu to exit.
The Restart button stops active work after confirmation and relaunches Shelfrip
on the same port with the same settings. `--port=8765` selects a starting port.

MakeMKV and cyanrip run under a small process supervisor. Quit, terminal interrupts
and termination signals stop owned rip processes; the supervisor also stops their
process groups if Shelfrip crashes or is killed. Unresponsive processes receive a
forced stop after a short grace period. Partial rips stay in the rip folder.

Set Linux paths for the rip and library folders, for example `/home/you/Ripped`
and `/home/you/Media`. Settings live in `$XDG_CONFIG_HOME/shelfrip`, or
`~/.config/shelfrip`. `SHELFRIP_DATA` overrides this location. Newly saved settings
have mode 0600 because API keys are stored there. New OpenSubtitles passwords go
into the desktop Secret Service keyring; settings contain only an opaque reference.

System tools used by the Linux integration:

- `udevadm`: cached optical-drive/media discovery, without launching a new MakeMKV
  scan every poll. Usually supplied by the system's udev/systemd package.
- `eject`: opens a drive's tray. Failures are reported rather than treated as success.
- `xdg-open` (xdg-utils): opens files, video players and report folders.
- `zenity` or `kdialog`: native file/folder dialogs. KDE prefers kdialog when
  available. If neither is installed, type paths into settings.
- `notify-send` (libnotify): optional desktop notifications.
- `secret-tool` (libsecret tools) and a Secret Service provider, such as GNOME
  Keyring or a compatible KWallet configuration: OpenSubtitles password storage.
- GTK3 and Ayatana AppIndicator3 (or legacy AppIndicator3), plus a desktop
  StatusNotifier/AppIndicator host: optional tray icon.
- WebKitGTK 4.1 (or 4.0) and GTK3: optional reusable app window. Closing it leaves
  the server running; opening Shelfrip again reuses or recreates the window.
- `cyanrip`: audio-CD ripping. CD drive offsets accept `/dev/sr0=6, /dev/sr1=667`.
- MKVToolNix (`mkvmerge`, `mkvextract`, `mkvpropedit`): media inspection and splitting.
- `tesseract`: optional subtitle OCR.

Desktop tools need a graphical login session. They are optional for using the
local web interface on a headless machine. Drive access is still required for ripping.

## Passwords and the desktop keyring

Unlock your desktop keyring before saving or using the OpenSubtitles password.
Shelfrip passes the password to `secret-tool` over standard input, never on the
command line. If the helper or keyring is unavailable, saving fails with an
actionable message and keeps the previous settings; it does not save plaintext.
Other ripping features remain available without a keyring. Settings polling does
not unlock the keyring; a password lookup happens when OpenSubtitles is used.

In Settings > Names and lookups, **Remove saved password** clears the settings
reference and removes the saved keyring item. Replacement verifies the new item before updating settings
and deleting the old item. Failed writes keep the previous credentials. If deletion
fails, Settings asks you to remove the unused item in your keyring manager.
A settings reference alone is not a password backup:
after moving to another machine or account, enter your password again.

## Tray and background operation

Enable **Show a desktop tray icon** under Settings > App and restart Shelfrip.
The menu opens the interface or pending reviews, toggles auto-ripping, opens the
log when available, and restarts or quits. Closing the browser or app window leaves
Shelfrip running. Restart or Quit from the tray opens the page for confirmation if
work is active, so an accidental click does not interrupt a rip.

The tray uses the desktop's StatusNotifier/AppIndicator protocol, on X11 or Wayland
through GTK3. Some desktops supply a host directly; GNOME needs an AppIndicator
extension. GTK runs in a separate helper process that exits when Shelfrip stops or
crashes. A connected indicator reconnects when its tray host returns. Missing
libraries, a missing host or a headless session leave the web interface usable;
Settings > App shows why the tray could not connect. After installing or enabling
tray support, restart Shelfrip. Detached mode works without a tray.

Run desktop features within your graphical login session so they inherit its
display and session bus. Window activation uses GTK's desktop APIs; focus remains
subject to the compositor's policy. The native window opens external web links in
your default browser and keeps its web storage under the settings folder's `webview`
directory. Browser fallbacks retain their browser's window-management behaviour.
Neither `--detach` nor the opt-in login entry promises to survive logout.

## Distribution compatibility

The application uses standard Linux interfaces and discovers external tools on
PATH. It does not require a specific distribution, package manager or init system.
Install the tools above using your distribution's packages or the tool's own Linux
installation instructions; package names and availability vary. No Python packages
from pip are required.

Optical-drive discovery uses sysfs and udev's cached properties. This needs a running
udev-compatible device manager and `udevadm`, but does not require systemd as PID 1.
Containers and restricted environments must expose the optical devices and the
necessary system interfaces to use hardware features.

Headless systems can use the local web interface without desktop helper programs.
For desktop features, install the appropriate helpers listed above and run Shelfrip
in your graphical login session. Missing optional helpers do not prevent ripping.

## Drives and permissions

Devices are identified by their Linux path (such as `/dev/sr0`), not by assuming
sysfs order matches MakeMKV's drive indices. `/dev/cdrom` aliases are accepted for
ejection and offset configuration. Ejection validates that the resolved path is
an optical block device before invoking `eject`.

If a drive is visible but cannot be read, inspect its permissions and the matching
SCSI generic device's permissions. Configure access through your distribution's
udev rules, session ACLs or optical-drive group, then sign in again if group
membership changed. Do not run Shelfrip as root. A USB DVD drive can test DVDs and
audio CDs; Blu-ray/4K testing requires a compatible drive.

MakeMKV also needs the Linux `sg` driver. If `/dev/sr0` exists but its corresponding
SCSI-generic device is missing, a system administrator can load `sg` and configure
it to load on boot. Access is needed to both the optical block device and its
matching SCSI-generic device, not to other disks' device nodes. Shelfrip reports
missing access in the drive card; it does not change permissions or load drivers.

If udev discovery is unavailable, video drive discovery falls back to MakeMKV.
That fallback may scan other drives, and does not provide Linux audio-CD detection.

## Disc fingerprints and TheDiscDB manifests

Mount a DVD or Blu-ray through your file manager or your system's normal mounting
tools before starting Shelfrip's disc identification or ripping. Shelfrip discovers
the mounted root from `/proc/self/mountinfo` and the optical device's major/minor
numbers. It accepts device aliases, escaped mount paths and whole-disc bind mounts;
it does not assume a `/media` or `/run/media` layout. Subdirectory mounts are skipped.
The mounted filesystem must be visible and readable by Shelfrip's user and process.
Shelfrip does not mount discs automatically or change permissions.

Fingerprints use file sizes from `VIDEO_TS` or `BDMV/STREAM`, following the
[TheDiscDB content-hash recipe](https://github.com/TheDiscDb/optical-disc-manifest/blob/main/specification/v1/README.md).
They support catalog/TheDiscDB identification before ripping and are recorded in
`disc_info.json` for subsequent matching. Lower-case disc directory names are
supported on case-sensitive filesystems. Unmounted or unreadable discs still rip
through MakeMKV; the log explains when mounted-disc metadata is skipped.

For an optional upload manifest, supply a native `thediscdb-scan` executable in
Settings > Programs > More options, or place it in Shelfrip's `tools` folder or on
PATH. Shelfrip does not bundle or download the scanner. It uses the same CLI contract
as the Windows integration:

```sh
thediscdb-scan /path/to/mounted/disc --output /path/to/disc.odm.json --force --quiet
```

The scanner runs before ripping under the application's process supervision.
Cancel and shutdown stop it; a five-minute timeout skips it. Only a successful JSON
document containing a disc object is published as `disc.odm.json` in the rip folder.
Missing programs, nonzero exits, invalid output and timeouts are logged and do not
stop the rip. Nothing is uploaded automatically.

## Tests and real hardware validation

```sh
python3 tests/test_linux.py
python3 tests/test_processes.py
python3 tests/test_keyring.py
python3 tests/test_background.py
python3 tests/test_disc_metadata.py
python3 tests/test_linux_install.py
python3 tests/run_all.py
```

The Linux tests cover fake sysfs/udev discovery, empty and loaded drives, audio CDs,
Unicode labels, stable device selection, busy-drive handling, TOC parsing (including
enhanced CDs), read failures, eject failures, desktop helpers, process ownership,
settings permissions and starting the real application with an isolated data folder.
The application smoke test requests the HTML, settings, version and drive-status APIs,
then stops its child process. No tests eject a physical drive or start a real rip.
Process tests create real subprocess trees and verify cancellation, graceful
shutdown and abrupt parent death, including children that ignore termination.
The application smoke test also verifies restart and quit through their APIs.
Password tests cover missing/locked keyrings, exact password round-trips, secret
redaction and rollback when settings cannot be written. Background tests
launch the real detached application, reuse it, restart it and quit it without a
graphical session.
Disc metadata tests cover mount identity and escaping, published DVD/Blu-ray hash
vectors, case-sensitive folders, pre-rip identification, saved rip metadata and
scanner discovery. An executable test scanner verifies command arguments, output
publication, failure cleanup, timeout and cancellation. This validates the integration
contract, not an upstream scanner binary or its disc parsing.
Package tests build and install the real archive into temporary homes, check XDG
entries and uninstall protection, reject damaged/unsafe updates, and update a
running installed app to a new test version on the same port without losing settings.

An additional native integration suite runs with a temporary home, private D-Bus
session, Xvfb, a real GNOME Keyring and a test StatusNotifier host:

```sh
python3 tests/desktop_services.py
```

It needs `dbus-run-session`, Xvfb, xauth, GTK3, WebKitGTK 4.1, Ayatana AppIndicator3,
libsecret tools, GNOME Keyring, Openbox, wmctrl, desktop-file-utils and PyGObject.
PyGObject is only used by the test host, not Shelfrip.
The suite checks real password saving/replacement/removal, exported tray menus,
menu activation, status updates, host reconnection, missing-host fallback and helper
cleanup after abrupt parent death. It also checks native desktop-entry argument
parsing and launches an installed app through its real menu entry, checking window
reuse, closure, reopening and shutdown. CI runs this separately from the
standard-library test suite and the Debian/Alpine portability tests. It does not establish appearance
or window behaviour in a user's actual GNOME/KDE Wayland session.

Before calling Linux support hardware-validated, test with a separate rip/library
folder and auto-rip/auto-file disabled:

1. Connect an empty drive; confirm it appears. Insert a DVD; confirm the label and
   loaded state change. Remove/reinsert it and unplug/reconnect the USB drive.
2. Rip a DVD, review its files, play a title, then file it into the test library.
   Confirm the expected files and automatic ejection. Test manual ejection too.
3. Cancel a rip; confirm its MakeMKV process stops. Retry the disc and check recovery.
4. Insert an audio CD; confirm detection and a stable MusicBrainz disc ID, then rip
   with cyanrip and check track count, durations, FLAC tags and filing.
5. With two drives, rip concurrently and confirm polling does not interrupt either.
6. Test folder selection, playback, notifications and app-window behaviour in actual
   GNOME/KDE sessions, including Wayland. Headless API tests do not establish this.

## Recorded DVD validation

On 2026-11-27, a USB HL-DT-ST DVDRAM GP75N was tested with a DVD:

- Shelfrip detected the device, inserted media and volume label; MakeMKV read four titles.
- A selected-title rip was cancelled after saving began. The supervisor returned a
  cancellation status and Shelfrip removed the process from its active-rip registry.
- Retrying that title completed successfully: one 1,294,829,175-byte MKV, with
  1,610.52 seconds of video, two audio tracks and eleven subtitle tracks confirmed
  by ffprobe. Test files were kept outside the user's media library.
- Ejection succeeded and subsequent discovery reported the drive empty.

A subsequent mounted-disc check used a temporary read-only mount of the attached
DVD. Shelfrip resolved `/dev/sr0` to that mount, classified it as DVD and fingerprinted
16 files as `CE73F24883ADEF612A96399412965FEC`. The same hash reached the rip metadata.
The temporary mount was removed afterward, and unmounted-disc detection was checked.
No native TheDiscDB scanner was available for physical-disc scanner validation.

The host initially lacked the `sg` driver and access to the optical device nodes.
Loading that driver and granting temporary access enabled the tests. Device-manager
events reset the temporary permissions on media removal, so normal use needs
persistent optical-device access configured through the system's device rules or
group/session permissions. Shelfrip now diagnoses these missing prerequisites.

A copy of the real DVD title was also passed through the review/save/play/file HTTP
workflow using a manual review plan and a separate test library. Play decoded 30
seconds of video and audio without errors using a test player command; it was not
a visual playback check. Filing completed, saved its catalog/plan records and
cleared the temporary rip folder. The filed MKV retained its 1,610.52-second duration
and identical encoded video packets. The original validation rip was preserved.

This validates one DVD drive and disc. Audio CDs, Blu-rays, multiple simultaneous
drives, automatic identification of additional discs and actual GNOME/KDE graphical
sessions still need hardware or desktop validation. Native desktop tests use an
isolated X11 display; Wayland session behaviour and human-visible playback remain
unvalidated. Application shutdown/crash and restart coverage uses isolated
subprocess and HTTP integration tests.

## Remaining Windows parity work

- Reading CD-Text from audio discs on Linux. MusicBrainz lookup works independently.
- Validation with an upstream native TheDiscDB scanner binary.
- Full physical-drive and graphical-session testing as listed above.

Linux support does not yet provide complete Windows feature parity.
