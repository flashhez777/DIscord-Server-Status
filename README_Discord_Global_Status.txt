DISCORD GLOBAL STATUS MONITOR v2.1.0
====================================

Cross-platform worldwide Discord status dashboard for Windows, Kali Linux,
and other systems with Python 3.9 or newer.

This is a GLOBAL Discord infrastructure monitor. It does not monitor a
specific guild, bot, user account, private server or message.

WHAT IT MONITORS
----------------
1. Discord's official public Statuspage summary:
   - Overall worldwide status
   - Core service components
   - Published client status (Desktop, iOS, Android, Web)
   - Published Voice components by named region
   - Active/unresolved incidents and their latest updates
   - Upcoming and active scheduled maintenance

2. Optional connectivity checks performed from this computer:
   - Discord REST API endpoint
   - Real unauthenticated WebSocket handshake with Discord Gateway
   - Discord CDN image endpoint

IMPORTANT DISTINCTION
---------------------
The official status feed is the source for worldwide and published regional
status. REST/Gateway/CDN probes are measurements from THIS computer's network
path. They are displayed separately and are not represented as global
telemetry. Public regional status is limited to the regions/components
Discord publishes.

AUTOMATIC MONITORING
--------------------
Run the script or launcher normally. Continuous monitoring starts by default:
- Fetches Discord's official global status feed.
- Runs local REST/Gateway/CDN checks (unless --no-probes is specified).
- Detects global status, component, incident and maintenance changes.
- Recovers automatically from temporary HTTP, DNS and connection errors.
- Saves the last known state and an event history.
- Sends desktop notifications for global changes/feed outages where the OS
  desktop notification service is available.
- Continues until Ctrl+C is pressed.

The default polling interval is 30 seconds. The minimum accepted interval is
10 seconds to avoid excessive requests to the public status service.

AUTOMATIC DEPENDENCY INSTALLATION
---------------------------------
The monitor's core requires only Python's standard library. On startup it
checks for optional quality-of-life packages:
- plyer: desktop notifications
- colorama: improved ANSI color handling on Windows consoles

Missing optional packages are installed automatically with the active Python
interpreter's pip into a private vendor directory under the app's per-user
state folder. It does not use sudo, modify system Python packages, or require
administrator rights. If pip, PyPI or notifications are unavailable, the
monitor continues with its standard-library/native fallback.

Windows state/vendor folder:
  %LOCALAPPDATA%\DiscordGlobalStatusMonitor\

Linux state/vendor folder:
  ~/.local/state/DiscordGlobalStatusMonitor/

REQUIREMENTS
------------
- Python 3.9 or newer
- Internet connection
- No Discord account, bot token or API key
- No root/admin permissions for the monitor

WINDOWS
-------
1. Install Python 3.9+ from https://www.python.org/downloads/windows/
   Enable "Add python.exe to PATH" during setup.
2. Extract all package files into one folder.
3. Double-click RUN_DISCORD_STATUS.bat.

PowerShell alternative:
  py -3 discord_server_global_status.py

KALI LINUX
----------
Run:
  chmod +x RUN_DISCORD_STATUS.sh
  ./RUN_DISCORD_STATUS.sh

Or directly:
  python3 discord_server_global_status.py

If Python 3 is missing:
  sudo apt update && sudo apt install -y python3

COMMON OPTIONS
--------------
Continuous monitoring, refresh every 30 seconds (default):
  python3 discord_server_global_status.py

Refresh every 15 seconds:
  python3 discord_server_global_status.py --interval 15

One snapshot then exit:
  python3 discord_server_global_status.py --once

Only the official global status feed; disable local probes:
  python3 discord_server_global_status.py --no-probes

Machine-readable JSON snapshot:
  python3 discord_server_global_status.py --json

Show saved status-change history:
  python3 discord_server_global_status.py --history

Enable terminal beep when status changes:
  python3 discord_server_global_status.py --beep

Disable desktop notifications:
  python3 discord_server_global_status.py --no-notifications

Disable automatic installation of optional packages:
  python3 discord_server_global_status.py --no-auto-install

Disable local state/event logging:
  python3 discord_server_global_status.py --no-save

Show all options:
  python3 discord_server_global_status.py --help

PERSISTENT DATA
----------------
Windows:
  %LOCALAPPDATA%\DiscordGlobalStatusMonitor\

Linux:
  ~/.local/state/DiscordGlobalStatusMonitor/

The folder stores last_state.json, events.jsonl and the optional vendor
packages. Delete this folder to reset saved state, history and local packages.

DATA SOURCE
-----------
Official Discord Statuspage:
  https://discordstatus.com

Official summary API:
  https://discordstatus.com/api/v2/summary.json

The monitor polls Discord's public status feed. It cannot see unreported
incidents, private backend telemetry, or outages Discord has not published.
