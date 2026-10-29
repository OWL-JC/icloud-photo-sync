# iCloud Photo Sync

**English** | [简体中文](README.zh-CN.md)

A small, menu-driven tool to sync your iCloud Photos library to a local folder,
using the third-party downloader [`icloudpd`](https://github.com/boredazfcuk/icloud_photos_downloader).

Supports **multiple Apple IDs** (each into its own subfolder), a **configurable
backup location**, and **scheduled sync** (Windows Task Scheduler, or `cron`
on Linux/macOS).

> Note: the interactive menu is in Chinese. The tool itself works on any OS
> where `icloudpd` runs.

## Features

- Sync iPhone / iCloud Photos to local disk, organized by `year/month`.
- Multiple accounts, each in its own subfolder under the backup root.
- Configurable backup root and `icloudpd` location via `config.ini`.
- Two non-interactive modes for schedulers:
  - `python sync_all.py --sync` — incremental sync (stops after 50 already-downloaded photos).
  - `python sync_all.py --full` — full download (optionally `--accounts a,b,c`).
- Passwords are **never stored**: you type the password + 2FA code once at login;
  `icloudpd` saves a session cookie, so later syncs need no password.

## Why not the official iCloud for Windows client?

The official client authenticates through `gsa.apple.com`, which may be blocked
in some networks (TLS handshake drops while other Apple domains work fine).
`icloudpd` uses a different web API auth path and works where the official client
does not. This tool simply wraps `icloudpd`.

## Requirements

- Python 3.9+
- [`icloudpd`](https://github.com/boredazfcuk/icloud_photos_downloader) installed
  and available on `PATH` (or set its full path in `config.ini`).
- An Apple ID with iCloud Photos enabled, and the ability to complete 2FA
  (password + phone/device verification code) at login time.

## Setup

1. Install `icloudpd` (see its docs). Verify with `icloudpd --help`.
2. Clone this repo and enter it:
   ```bash
   git clone https://github.com/OWL-JC/icloud-photo-sync.git
   cd icloud-photo-sync
   ```
3. (Optional) Copy the example config and adjust:
   ```bash
   cp config.ini.example config.ini
   ```
   On first run `sync_all.py` also creates a default `config.ini` automatically.

## Usage

Run the interactive menu:

```bash
python sync_all.py
```

```
1. 登录 / 添加账号        # enter Apple ID, then password + 2FA code when prompted
2. 管理账号（查看 / 删除）
3. 设置备份地址          # change where photos are stored
4. 全量下载（可选时间）  # full download now, or schedule a start time
5. 立即同步一次（增量）  # one incremental sync
6. 设置每日自动同步      # create a daily scheduled task at a chosen time
7. 查看 / 取消定时任务
8. 退出
```

## Background (scheduled) modes

These are what the scheduled task runs — no human input needed:

```bash
python sync_all.py --sync          # incremental sync of all accounts
python sync_all.py --full          # full download of all accounts
python sync_all.py --full --accounts you@example.com   # only this account
python sync_all.py --redact-log    # strip emails/paths from logs/sync.log before sharing
```

Exit codes (for schedulers and CI):

| Code | Meaning |
|------|---------|
| `0`  | Success — new photos downloaded, or already up to date |
| `1`  | Sync failed — no accounts, unknown account name, `icloudpd` missing/timeout/error, or directory statistics failing (folder missing / no permission) |
| `2`  | Usage error — e.g. `--accounts` with no value |
> Nothing is ever deleted from disk by this tool; a non-zero exit means the sync
did not complete, not that photos were removed.

## Configuration (`config.ini`)

| Key           | Meaning |
|---------------|---------|
| `backup_root` | Root folder for photos; each account gets its own subfolder. |
| `cookie_dir`  | Where `icloudpd` stores login session cookies. Do not edit manually — keep it on a local, non-cloud-synced path. |
| `tool`        | Path to `icloudpd` (or just `icloudpd` if on `PATH`). |
| `domain`      | iCloud region. **Mainland China → `cn`**; elsewhere leave empty. |
| `auto_delete` | Mirror deletions — if `true`, photos deleted in iCloud are also deleted locally (destructive, handled by `icloudpd --auto-delete`; needs `icloudpd>=1.12.0`). Default `false`. |
| `[account_x]` | One section per account: `apple_id` and `folder` (subfolder name; empty = backup root). |
| `[notify]`    | **Optional.** Failure alert email. Set `enabled=true` and fill `smtp_host` / `smtp_user` / `smtp_pass` / `to` (see example in `config.ini.example`). Sent when a sync fails (or disk space is low). Default off. |
| `[log]`       | **Optional.** `max_size_mb` + `keep`: rotate `sync.log` once it exceeds the size, keeping up to `keep` old copies. |
| `[disk]`      | **Optional.** `warn_free_gb`: warn (log + email) when the backup drive has less than this many GB free. |

### Optional enhancements

- **Failure email alert** — add a `[notify]` section and set `enabled=true`. On any
  sync failure (and on low disk space) the tool sends an email via SMTP, so you know
  even when running unattended. Uses SSL (port 465) or STARTTLS (port 587).
- **Log rotation** — add a `[log]` section. `sync.log` is rotated once it passes
  `max_size_mb`, keeping up to `keep` historical copies (`.1`, `.2`, …).
- **Disk space warning** — add a `[disk]` section with `warn_free_gb`. When free space
  on the backup drive drops below that threshold, a warning is logged (and emailed if
  `[notify]` is enabled).

## Scheduling

This is fully **local** — it does **not** require GitHub Actions or a self-hosted
runner. The `scheduled-sync.yml` workflow in this repo is only an *example* for
people who run their own runner; the recommended way to schedule syncs is below.

**Windows (recommended — non-interactive):**

```bash
python install_scheduler.py --daily 21:00
# or a one-time full download at a chosen time:
python install_scheduler.py --once 23:30 --accounts you@example.com
# inspect / remove what it created:
python install_scheduler.py --show
python install_scheduler.py --remove
```

This creates a Windows Task Scheduler entry that runs `python sync_all.py --sync`
daily (or `python sync_all.py --full --accounts ...` once). No GitHub involved.

**Windows (interactive menu):** use menu option `6` to create a daily task at a
time you choose (or option `4` to schedule a one-time full download).

**Linux / macOS:** use `cron`. Example — incremental sync every day at 09:30:
```cron
30 9 * * * /usr/bin/python3 /path/to/icloud-photo-sync/sync_all.py --sync
```

## Continuous Integration / GitHub Actions

Two example workflows ship in `.github/workflows/`:

- `ci.yml` — on every push / pull request: compile check, a `ruff` lint gate
  (any violation fails the build), plus smoke tests including assertions that
  failure cases exit non-zero (no accounts, unknown account, missing `icloudpd`,
  usage error),   across a Python 3.9 / 3.10 / 3.12 × Ubuntu / Windows matrix.
- `scheduled-sync.yml` — an example daily sync for a **self-hosted runner**
  (manual-only by default). GitHub-hosted runners cannot reach your photos or
  credentials, so enable it only on a runner you control; see the comments in
  the file for setup steps.

## Security

- **Never share your Apple ID password, 2FA codes, or the contents of your
  `cookie_dir`.** Maintainers will never ask for them.
- `config.ini` and `logs/` are gitignored — keep personal paths and Apple IDs out
  of commits and out of issue reports.
- `logs/sync.log` contains Apple IDs and local absolute paths. Run
  `python sync_all.py --redact-log` before pasting it into an issue or uploading
  it as a workflow artifact.
- `cookie_dir` holds the credential that grants full access to your iCloud photo
  library: keep it on a local, non-cloud-synced path (not OneDrive/Dropbox/
  iCloud Drive, not inside a repository).

## Caveats

- **`icloudpd`'s own exit code is not reliable**: it may return `0` even on bad
  arguments. This tool therefore judges success by the file count plus the
  process run status, and reports its own exit codes (`0`/`1`/`2`) — see
  [Background modes](#background-scheduled-modes).
- **Credentials expire** (usually weeks to months). When sync reports "no new
  files", re-run menu option `1` to log in again.
- **`--folder-structure` must be a single brace block**: `"{:%Y/%m}"`, not
  `"{:%Y}/{:%m}"`.
- **`--auto-delete` (mirror phone deletions to disk) is opt-in** — set
  `auto_delete = true` in `[settings]`. When enabled, photos deleted in iCloud are
  also deleted locally (destructive, passed through to `icloudpd --auto-delete`;
  needs `icloudpd>=1.12.0`). When disabled (default), this tool **never deletes any
  photos** — file disappearances are only flagged via `logs/NEEDS_ATTENTION.flag`.
- Login takes 1–3 minutes with no output; that is normal, do not close the window.

## License

[MIT](LICENSE)
