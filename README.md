# visor-dash

A local web dashboard for **Vauban VISOR+ / VERSO+** door access controllers: live door activity, alarms, long-term history, badge holders, groups and access hours, and remote door opening. It runs on any computer on the same network as the controller, and it keeps working when the controller's own web pages don't (see [the firmware mismatch it was built around](docs/PROTOCOL.md#a-known-failure-version-du-serveur-web-incompatible)).

> **Unofficial.** This project is not affiliated with, endorsed by or supported by Vauban Systems. It talks to the controller through the same web requests as the controller's own interface. Use it at your own risk on equipment you are responsible for, and keep a backup.

## Features

- **Today at the doors:** a 24-hour strip with one mark per event: doors opened, refused badges, alarms.
- **Doors:** activity per door (badge, exit button, remote), alarms, recent events, and an optional **Open door** button that waits for the controller to confirm.
- **Alerts:** grouped alarms (power-supply faults, forced doors, reader problems…) in plain language, with whether they're still active.
- **Activity:** the full event history, filtered and searchable, exportable to CSV. The controller only keeps its last 15,000 events; visor-dash archives them so history survives.
- **People:** everyone on the controller, with badge, groups, access status and **last seen**. Add, edit, block and delete people; add a badge by **swiping it on any reader**.
- **Access hours:** groups (which doors, when) and weekly time slots, with editing.
- **Backups:** the controller's own backup file plus a per-person snapshot, every night, with the last 30 kept.

Editing and door opening are **off by default** and switched on separately. Every change is checked against the controller afterwards and logged.

## Install

Download the file for your system from the [latest release](https://github.com/leokeba/visor-dash/releases/latest):

| System | File | |
|---|---|---|
| macOS (Apple silicon) | `visor-dash-<version>-macos-arm64.zip` | unzip, then use `./visor-dash` in Terminal |
| Windows (64-bit) | `visor-dash-<version>-windows-x64.zip` | unzip, then double-click `visor-dash.exe` or use it in a terminal |
| Linux (x86-64) | `visor-dash-<version>-linux-x64.tar.gz` | `tar xzf …`, then `./visor-dash` |
| Anything with Python 3.9+ (incl. Intel Macs) | `visor-dash-<version>.pyz` | `python3 visor-dash-<version>.pyz` |

The apps aren't code-signed, so your system will warn you the first time:

- **macOS:** "cannot be opened because the developer cannot be verified". Run `xattr -d com.apple.quarantine visor-dash` once in the unzipped folder, or right-click the file › Open.
- **Windows:** SmartScreen says "Windows protected your PC". Click *More info* › *Run anyway*.

Each release lists SHA-256 checksums in `SHA256SUMS.txt`.

The dashboard checks GitHub for a newer release and shows an update button when one is available. Packaged apps and `.pyz` installs can download, verify and install the matching update, then restart; source installs link to the release downloads. Checking for updates requires an internet connection.

Then:

```sh
./visor-dash setup             # asks for the controller's address and login, tests them, saves the settings
./visor-dash                   # starts the dashboard and opens http://localhost:8080
./visor-dash install-service   # optional: start at login and back up every night
```

On Windows, use `visor-dash.exe` instead of `./visor-dash`. Double-clicking `visor-dash.exe` runs the setup the first time, then starts the dashboard.

### Commands

| Command | What it does |
|---|---|
| `visor-dash` | Run the dashboard (and set it up first if needed) |
| `visor-dash setup` | Create or replace the settings |
| `visor-dash install-service` | Start the dashboard at login, restart it if it stops, back up every night at 03:15 (launchd on macOS, systemd user units on Linux, Startup folder + Task Scheduler on Windows; no admin rights needed) |
| `visor-dash uninstall-service` | Remove those background jobs (backups and history are kept) |
| `visor-dash backup` | Take a backup now |
| `visor-dash where` | Show where settings, backups, history and logs are kept |
| `visor-dash --home DIR …` | Use another data folder (for example to run two controllers) |

### Where files are kept

| System | Folder |
|---|---|
| macOS | `~/Library/Application Support/visor-dash` (logs in `~/Library/Logs/visor-dash`) |
| Windows | `%APPDATA%\visor-dash` |
| Linux | `~/.local/share/visor-dash` |
| From source | the project folder |

That folder holds `.env` (settings, including the controller password), `backups/` and `data/history.sqlite3`. They contain names, badge numbers and movements: they're created readable by you only. Copy `backups/` and `data/` somewhere safe from time to time.

### Run from source

Python 3.9 or newer, nothing else to install (standard library only):

```sh
git clone https://github.com/leokeba/visor-dash.git
cd visor-dash
python3 -m visor_dash setup
python3 -m visor_dash
```

## Settings

`visor-dash setup` writes them; you can also edit `.env` in the data folder ([example](.env.example)). Restart the dashboard after editing (`visor-dash install-service` again if you use the service).

| Setting | Meaning |
|---|---|
| `VAUBAN_HOST`, `VAUBAN_PORT` | Controller address (default port 80) |
| `VAUBAN_USER`, `VAUBAN_PASSWORD` | Controller login |
| `VAUBAN_LANG` | Language of event names: `en`, `fr` or `it` (read from the controller) |
| `DASH_SITE_NAME` | Title at the top of the page |
| `DASH_PORT` | Port of the dashboard (default 8080) |
| `DASH_BIND` | `127.0.0.1` = this computer only (default). `0.0.0.0` = other computers on the network |
| `DASH_PASSWORD` | Required when `DASH_BIND` isn't local. The browser asks for it (any username; it's recorded in the audit log) |
| `DASH_ALLOW_WRITES` | `1` = people, groups and time slots can be edited. Default `0` |
| `DASH_ALLOW_DOOR_OPEN` | `1` = doors can be opened from the dashboard. Default `0`, independent of editing |
| `HISTORY_DAYS` | Days of event history to keep (default 90; `0` = forever) |
| `BACKUP_KEEP` | Automatic backups to keep (default 30) |

### Using it from other computers

The page shows names and badge numbers, so visor-dash refuses to listen on the network without a password. Set `DASH_BIND=0.0.0.0` and a `DASH_PASSWORD`, then open `http://<this computer's IP>:8080`. Keep it on the local network; never expose it to the internet. Traffic is plain HTTP, like the controller itself.

## Using it

### People

Click a name in **People** for the person panel: last seen, recent activity, and (with editing on) name, access, up to three groups, an individual time slot and an optional validity period. **Add a person** creates one.

To add a badge without typing its number, press **Read from a reader** in *Add a person* and swipe the new badge on any door reader: the controller logs it as an unknown credential and the number is filled in. Every "Unknown credential" line in **Activity** also has an **Add as person** link.

A badge number and type can't be changed after creation (as in Vauban's interface): delete the person and add them again.

### Access hours

Each person belongs to up to three **groups**. For each door, a group allows **no access**, access **any time**, or access during a **time slot**. A **time slot** is a weekly schedule with up to 10 ranges per day (for example `08:00-12:30, 13:30-18:00`) and a row for public holidays.

The controller has fixed tables (1,024 groups, 128 time slots), so **Add a group / Add a time slot** sets up an unused entry: switched off, no access, nobody in it, and not referenced anywhere. Changes apply immediately, and changing a time slot affects every group that uses it (the panel lists them).

Times must end after they start. A night shift is split over two days (`22:00-23:59`, then `00:00-06:00`), because the controller doesn't take ranges that cross midnight.

### Doors

Click a door for its panel. With `DASH_ALLOW_DOOR_OPEN=1`, **Open door** unlocks it briefly, like Vauban's own "Open" button. You confirm first, the same door can't be reopened within 5 seconds, and the dashboard then waits for the controller's "remote opening" event to confirm the door actually opened. If the controller accepts the command but logs nothing, remote opening is probably turned off in its own settings.

Door settings (unlock duration, free-access hours, held-open mode, automations) aren't editable: on controllers with mismatched web pages, the controller doesn't return their current values, so saving them would overwrite everything with defaults.

### History

On first start, visor-dash imports everything the controller still holds (about a minute), then adds events as they happen. After a restart it fills the gap, as long as the controller hasn't overwritten those events yet. To lose nothing, keep it running.

Each event is stored with the person's name **at that time**, and only if the event's badge matches that person's badge: the controller reuses user slots, and this keeps a new person from inheriting someone else's history.

The default 90-day retention follows the CNIL's guidance for access-control logs (three months). Check what applies to you before keeping them longer.

### Backups

`visor-dash backup` (nightly with the service) saves the controller's own backup file, which its restore function accepts, and a snapshot of every person's full settings. Automatic backups are named `auto-…`; the newest 30 are kept, other files are never deleted. The page warns you if the last backup failed or is more than 36 hours old.

## Safety

- Reading uses an allowlist of controller pages. Writes are limited to the user, group and time-slot forms, single-user deletion and the door-open command, each behind its setting; tests check that everything else is refused (no firmware upload, restore, controller settings or "delete all").
- Before saving, the controller's edit page is parsed and only known content is accepted. If anything is unfamiliar, the change is refused rather than risking a field being reset.
- After saving, the record is read back and must match what was sent. For people, every other user record must also be byte-for-byte unchanged.
- Every change and door opening is appended to `backups/audit.jsonl` with who, when, what was sent, the previous settings and the controller's reply.
- Write requests need a JSON body with a custom header and a matching `Origin`, and are refused from other websites. On a local-only setup the `Host` header must be `localhost`, which blocks DNS rebinding.
- One request at a time to the controller; changes are serialised.

## Development

```sh
python3 -m unittest discover -s tests -t . -v      # synthetic data only; never contacts a controller
python3 -m pip install pyinstaller==6.11.1
python3 packaging/build.py --version 0.0.0 --target macos-arm64 --pyz
python3 packaging/smoke_test.py 0.0.0 dist/visor-dash-0.0.0-macos-arm64/visor-dash dist/release/visor-dash-0.0.0.pyz
```

Every push to `main` runs the tests on Linux, macOS and Windows, builds the apps, smoke-tests them and publishes a GitHub release `v0.1.<run number>` ([workflow](.github/workflows/release.yml)).

| Path | |
|---|---|
| `visor_dash/client.py` | controller access, file formats, read allowlist and write guards |
| `visor_dash/users.py` | user form: parsing, validation, verified create / edit / delete |
| `visor_dash/schedules.py` | groups and time slots: parsing, validation, verified saves |
| `visor_dash/history.py` | long-term event archive (SQLite), ring-log handling |
| `visor_dash/backup.py` | backups, rotation, status |
| `visor_dash/server.py` | local web server and API |
| `visor_dash/static/` | the page (HTML, CSS, JS; no external resources) |
| `visor_dash/cli.py`, `settings.py`, `service.py` | commands, data folder and settings, background jobs |
| `packaging/` | release build and smoke test |
| `docs/PROTOCOL.md` | what's known about the controller's web API |

## Ideas

- Restore a person's previous settings from the audit log in one click.
- Public holiday dates.
- Fault alerts by email or phone.
- CSV import of people.

## License

[MIT](LICENSE).
