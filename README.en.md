# Server Monitor Bot

[Русский](README.md) · **English**

A self-hosted Telegram bot that watches a Linux or Windows server: CPU, RAM, swap, disks, temperature, services, processes and Docker containers. It answers commands, sends an alert when a threshold is crossed or something goes down, and a second message when everything has recovered. On top of monitoring it does SSH security alerts, IP blocking, backup checks, package updates and one-tap admin actions, all from the chat.

Python 3, python-telegram-bot 22, psutil, SQLite, matplotlib. Interface in Russian, Ukrainian or English.

## Controls

A persistent keyboard at the bottom of the chat: **Status**, **Processes**, **Services**, **Alerts**, **Settings**. Inline buttons under messages: "Refresh" (updates the message in place), sort processes by CPU/RAM, "Mute for 1 h" and "Until morning" under an alert.

The **Settings** menu (saved to `settings.json`, previous version in `settings.json.bak`):
- CPU / RAM / disk thresholds (±5%) and how long a breach must last (1 / 5 / 15 min);
- check interval (30 s / 1 min / 5 min);
- what to watch: toggle services and containers, "➕" lists the ones running on the server;
- quiet hours: at night only critical alerts (a service, container or process went down);
- reminders about an unresolved problem (30 / 60 / 180 min);
- daily summary at 09:00 or 20:00;
- reset to the values from `config.yaml`.

**🌐 Sites and ports** in settings and `/probe`: external checks. `/probe https://example.com` adds a website, `/probe https://example.com/health | OK` also looks for text on the page, `/probe tcp://host:5432` checks a port, `ping://host` and `dns://name` do ping and DNS. Results show up under "Services".

**🐳 Docker** in settings: watch all containers automatically, alert on errors in container logs.

Commands: `/status`, `/procs`, `/mem`, `/services`, `/alerts`, `/incidents`, `/settings`, `/logs name [lines]`, `/chart 1h|24h|7d`, `/probe address`, `/backup`, `/net`, `/updates`, `/ban IP`, `/mute 2h`, `/unmute`, `/maint 45m`, `/actions`, `/lang`.

## Language

Russian (default), Ukrainian or English: **Settings → 🌐 Язык · Мова · Language** or `/lang uk|en|ru`. Buttons, replies, alerts, summaries, the weekly report and chart labels follow the chosen language. The default is `lang:` in `config.yaml`.

For developers: strings in the code are written in Russian and wrapped in `_()` (`_("Диск {path}", path=p)`); translations live in `locales.py`. `tests/test_i18n.py` fails if a string is missing `_()` or a translation.

## History and charts

Once a minute the bot writes CPU, RAM, disk usage and network counters to `metrics.db` (SQLite, 30 days, about 10 MB). The "📈 Chart" button sends an image for 1 h / 24 h / 7 days with peaks and traffic. The daily summary has the day's peaks, disk growth, traffic and a forecast like "at the current rate the disk fills up in N days".

## Alerts

- Resources (CPU, RAM, disk, temperature): when the breach lasts longer than the configured time.
- Services, Docker containers, processes: immediately.
- Server reboot, OOM killer, "reboot required after updates".
- Docker: container stopped or restarted (including Docker's own auto-restart), healthcheck `unhealthy`, errors in logs (`Traceback`, `ERROR`, `CRITICAL`, `Exception`, `FATAL`, at most once per 15 min). Per-container CPU and RAM in "Status", a "📜 Logs" button under the alert.
- External checks: a site, port, ping or DNS fails 2 checks in a row (also sent during quiet hours). TLS certificates of https sites: warnings 14, 7, 3 and 1 day before expiry and on the day.
- Unusual load: CPU or RAM far above what is normal for this hour for 15 minutes (baseline from 14 days of history, at least 3 days needed), even when far below the threshold.
- Memory growth: RAM grows steadily for 6 hours and would run out in under 12 hours at that rate (looks like a leak). The alert names the process using the most memory.
- Under an alert: "✔️ Acknowledge" (no reminders until the problem clears), "💤 This one for 1 h", "🔕 All for 1 h". More than 3 alerts in one check arrive as a single message with "Acknowledge all".
- When a problem clears, a "Recovered" message; until then, reminders.
- **Quiet hours** (23:00–06:00, 23:00–08:00, 00:00–07:00, 22:00–09:00): only critical alerts at night.
- **Maintenance** (30 min, 1 h, 2 h, or `/maint 45m`, `/maint off`): no alerts at all while you work on the server. Problems are still logged, marked "maintenance", and not counted as downtime in the weekly report.
- Alert state is kept in `state.json`, so a restart does not repeat old alerts.

## Incident log and weekly report

- `/incidents`: the last 15 incidents with start time and duration, and downtime over 7 days. Incidents are always logged, even during quiet hours or when notifications are muted. Stored in `metrics.db` for 180 days.
- **Weekly report** every Monday at 09:00: incidents and downtime by problem compared with last week, uptime of each external check, average and peak load, disk, traffic, events by type, backups, number of IPs blocked by the bot, and a 7-day chart.

## Other servers

One bot can watch other servers too: on every check it logs in over SSH and asks the agent `deploy/monitor-agent`
for CPU, memory, disks, services, containers and pending reboot. Thresholds are shared with the main server.
Alerts carry the other server's name in the header; `/servers` shows its last state. Two failed polls in a row
raise a critical "📡 Server is not responding" alert. The agent is read-only and is reached with a key restricted
in `/root/.ssh/authorized_keys` by `command="/usr/local/sbin/monitor-agent",restrict`. See `remote_servers` in
`config.example.yaml` and the Russian README for step-by-step setup.

## Security

- SSH login from a new IP: immediately. Password brute force (more than the threshold of failed attempts in 10 minutes): at most once an hour. The bot needs to read the sshd journal: `sudo usermod -aG systemd-journal monitor`.
- A new port open to the outside (not localhost). Ports open on first start are remembered as normal.
- Ubuntu security updates: checked every 6 hours, reminder at most once a day.
- **If the whole server goes down, the bot can't tell you**, so there is `heartbeat_url`: the bot pings an external service (for example healthchecks.io) on every check, and that service notifies you when the pings stop.

All of this is toggled in **Settings → 🛡 Security**.

## Network and disk

"🌐 Network and disk" or `/net`: receive/send rate per interface, traffic per day and week, top remote IPs by connection count, processes doing the most disk I/O, inode usage (alert above `thresholds.inode`, 90% by default).

## IP blocking

The "🚫 Block IP" button under an SSH brute-force alert, or `/ban IP`, adds a `ufw deny from IP` rule tagged `monitor-bot`. Local and reserved addresses and IPs with a currently open SSH session are never blocked; only the bot's own blocks can be removed from the bot.

**Auto-ban**: 10 or more failed SSH logins from one IP within 10 minutes blocks it for 24 hours. Threshold, duration and the switch are in "🛡 Security". IPs with a successful SSH login in the last 30 days, IPs in `autoban_whitelist` and open sessions are never auto-banned.

Note: Docker bypasses ufw for published container ports (`-p 8080:80`). Close those with rules in the `DOCKER-USER` chain or publish them on 127.0.0.1 only.

## Backups

`/backup` or **Settings → 💾 Backups**.
- **Bot backup** daily at 03:45: `config.yaml`, `settings.json`, `state.json`, `actions.log` and a live copy of `metrics.db`, kept 14 copies in `/var/lib/monitor_bot/backups`.
- **Other backups**: folders listed in `/etc/monitor-bot/backup-watch` with a maximum age. The bot warns if the newest backup is too old, missing, or much smaller than the previous one.
- **📤 Send**: the bot backup or the newest backup from any watched folder arrives as a Telegram file (up to 45 MB), optionally daily.
- **Copies on a PC**: a Windows PC pulls the latest backups once a day over SSH with a key restricted to a single command (`pull-stream`), checks SHA256 sums and keeps 30 copies. Setup scripts are in `deploy/pc/`. The bot warns if the PC hasn't pulled for more than 3 days.
- **Restore test** every Sunday at 04:30: the bot unpacks its own backup, reads `config.yaml` and runs `PRAGMA integrity_check` on `metrics.db`; the helper reads other archives in full to catch truncated or corrupt ones.

## Actions from Telegram

`/actions`: restart a container or service, stop a process, free disk space (unused Docker images, build cache, journal down to 200 MB, apt cache), reboot the server. Every action needs a confirmation (a reboot needs two) and is written to `actions.log`.

Everything that needs root goes through the helper `deploy/monitor-bot-action`, which validates its arguments: services only from `/etc/monitor-bot/allowed-services`; no kernel, system or apt/dpkg processes; Docker limited to reading state and logs and restarting a container. The bot's user **must not be in the `docker` group**, since that group is equivalent to root.

```
sudo install -o root -g root -m 755 deploy/monitor-bot-action /usr/local/sbin/monitor-bot-action
sudo install -o root -g root -m 440 deploy/sudoers-monitor-bot /etc/sudoers.d/monitor-bot
sudo visudo -cf /etc/sudoers.d/monitor-bot
sudo install -D -o root -g root -m 644 deploy/allowed-services /etc/monitor-bot/allowed-services
```
Turn everything off with `actions_enabled: false` in `config.yaml`.

## Package updates

`/updates`: available Ubuntu updates (security ones first) with old → new versions and whether a reboot is needed. Buttons to re-check (`apt-get update`), install security updates only, or install all (`apt-get upgrade`). Installation runs in a separate systemd unit, so restarting services (including the bot) does not interrupt it, and the bot reports what was upgraded and what still needs a reboot. The server never reboots on its own.

## Self-update

**Settings → ⬆️ Update bot** shows the installed version and what's new on `main`. "⬆️ Update" downloads the new code, runs its tests as the bot user, keeps a backup of the current code, installs, restarts, checks after 30 seconds that the service is healthy, and rolls back automatically if not. The helper, sudoers and service file are never updated automatically, so code from GitHub cannot widen the bot's privileges.

## Installation

1. Create a bot with @BotFather and get the token.
2. On the server:
   ```
   python -m venv venv
   venv/bin/pip install -r requirements.txt      # Windows: venv\Scripts\pip install -r requirements.txt
   cp config.example.yaml config.yaml            # Windows: copy config.example.yaml config.yaml
   ```
3. Put the `token` into `config.yaml`, run `python bot.py` and send `/start` to the bot. It replies with your chat_id; add it to `allowed_chats` and restart.
4. Fill in `services`, `processes`, and adjust thresholds if needed.

## Autostart

- **Linux:** `deploy/monitor-bot.service` (instructions inside the file). Settings and state go to `/var/lib/monitor_bot`.
- **Windows:** Task Scheduler, trigger "At startup", action `venv\Scripts\pythonw.exe bot.py config.yaml`, working directory = the bot folder, "Run for all users". Or a service via NSSM.

## Tests

```
python -m unittest discover tests
```

Tests run on GitHub Actions for every PR and push to `main` (`.github/workflows/tests.yml`).
