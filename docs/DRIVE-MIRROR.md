# Google Drive as Your Memory Mirror

This guide shows you how to set up Google Drive as a one-to-one backup
of your local memory files. Your local folder stays the working copy.
Drive holds an identical mirror. If your computer dies, your memories
survive.

## The idea

```
Local:  ~/memory/
            ├── work/
            ├── personal/
            └── journal.md

Drive:  My Drive/Memory/
            ├── work/          (identical copy)
            ├── personal/      (identical copy)
            └── journal.md     (identical copy)
```

Every file in your local memory folder has exactly one matching file
in Drive. Same names, same folder structure. When you update a local
file, you push the change to Drive. When you need to restore, you pull
from Drive.

## Setup

### 1. Create the Drive folder

1. Go to [drive.google.com](https://drive.google.com)
2. Click **New** → **New folder**
3. Name it `Memory`
4. Note the folder ID from the URL:
   `https://drive.google.com/drive/folders/1ABC123xyz...`
   The part after `/folders/` is your folder ID. Save it.

### 2. Install rclone (free, open source)

rclone syncs local folders to cloud storage from the command line.

**Mac:**
```bash
brew install rclone
```

**Linux:**
```bash
sudo apt install rclone
# or
curl https://rclone.org/install.sh | sudo bash
```

**Windows:** Download from [rclone.org](https://rclone.org/downloads/)

### 3. Connect rclone to Google Drive

```bash
rclone config
```

Follow the prompts:
1. Type `n` for new remote
2. Name it `gdrive`
3. Choose `drive` (Google Drive)
4. Leave client ID and secret blank (press Enter)
5. Choose `1` for full access
6. Leave root folder ID blank
7. When asked "Use auto config?", type `y`
8. Your browser opens. Sign in to Google and click **Allow**.
9. Type `y` to confirm, then `q` to quit.

Test it:
```bash
rclone lsd gdrive:
```
You should see your Drive folders listed.

### 4. Do the first sync

```bash
# Replace FOLDER_ID with your Memory folder ID from step 1
rclone sync ~/memory gdrive:Memory --drive-root-folder-id FOLDER_ID -v
```

This copies everything up. The first run takes a while depending on
how many files you have.

## Daily use

### Push changes to Drive

After you update your memory files, mirror the changes:

```bash
rclone sync ~/memory gdrive:Memory --drive-root-folder-id FOLDER_ID
```

**Important:** `sync` makes Drive match local exactly. Files you
deleted locally get deleted on Drive too. This is what you want for
a true mirror, but be careful.

If you want to be safe and never delete from Drive automatically,
use `copy` instead:

```bash
rclone copy ~/memory gdrive:Memory --drive-root-folder-id FOLDER_ID
```

### Pull from Drive (restore)

If you lose your local files:

```bash
rclone sync gdrive:Memory ~/memory --drive-root-folder-id FOLDER_ID
```

### Automate it

**Mac/Linux with cron** — sync every hour:

```bash
crontab -e
```

Add this line (replace FOLDER_ID):

```
0 * * * * /usr/bin/rclone sync ~/memory gdrive:Memory --drive-root-folder-id FOLDER_ID --log-file ~/.rclone-memory.log
```

**Manual habit** — if you prefer control, just run the sync command
at the end of each day.

## Tips

- **Exclude the index cache.** The Helix Memory Corridor builds its
  search indexes locally in `~/.helix-memory/`. Don't sync that folder.
  It's rebuilt automatically. Only sync your source markdown files.

- **Drive file limits.** Google Drive handles tens of thousands of
  files fine. If you have more than ~100k files, consider splitting
  into multiple Drive folders.

- **Version history is free backup.** Google Drive keeps version
  history on every file. Even if a bad sync overwrites something,
  you can restore the previous version from the Drive web UI:
  right-click the file → **Manage versions**.

- **Sharing.** If you want someone else to read your memories
  (not edit), share the Drive folder with "Viewer" permission.
  They get a read-only copy without touching your local files.

## Troubleshooting

**"Failed to get folder ID"** — Double-check the folder ID from the
Drive URL. It must be the Memory folder, not a parent.

**"Token expired"** — Run `rclone config`, choose your `gdrive`
remote, and re-authenticate.

**Sync is slow** — Add `--transfers 8` to run 8 uploads in parallel:
```bash
rclone sync ~/memory gdrive:Memory --drive-root-folder-id FOLDER_ID --transfers 8
```
