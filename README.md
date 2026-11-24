# arc2zen

Migrate [Arc Browser](https://arc.net) to [Zen Browser](https://zen-browser.app) on macOS.

Arc has no bookmarks export, and Zen has no Arc importer. `arc2zen` reads Arc's local
data and gives you two ways to move over:

| Command     | What it does                                                                 | Risk |
|-------------|------------------------------------------------------------------------------|------|
| `bookmarks` | Exports pinned tabs and folders as a standard bookmarks HTML file             | None - imports through Zen's own UI |
| `spaces`    | Recreates your Arc **spaces** as Zen **Spaces**, with folders, pinned tabs, Essentials and containers | Edits Zen's session file; Zen must be closed; a backup is made |

Python 3 standard library only, no install step.

## Quick start

```sh
git clone https://github.com/robinsound/arc2zen && cd arc2zen

# 1. See how Arc profiles will map to Zen containers (read-only)
python3 arc2zen.py profiles

# 2a. Safe route: export bookmarks, then import the HTML in Zen
python3 arc2zen.py bookmarks -o ~/Downloads/arc-bookmarks.html
#     Zen: Cmd+Shift+O > Import and Backup > Import Bookmarks from HTML...

# 2b. Full route: recreate spaces (quit Zen first with Cmd+Q)
python3 arc2zen.py spaces --dry-run     # preview, writes nothing
python3 arc2zen.py spaces               # do it
```

Then reopen Zen. First launch can be slow if you have thousands of pinned tabs.

## Let an AI agent do it

Using Claude Code or a similar coding agent on your Mac? Paste this prompt:

```text
Move everything from Arc Browser to Zen Browser using https://github.com/robinsound/arc2zen.
Clone it, read its README, and run `python3 arc2zen.py profiles` to show me how Arc
profiles map to Zen containers; ask me to confirm or adjust that mapping. Then run
`spaces --dry-run` and show me the result. Ask me to quit Zen (Cmd+Q) before running
`spaces` for real, and don't touch Zen's files while it is open. Afterwards, also
export bookmarks as a fallback. Report what was migrated and where the backup is.
```

## How Arc concepts map to Zen

| Arc                               | Zen                                                       |
|-----------------------------------|-----------------------------------------------------------|
| Space                             | Space (same name and emoji)                               |
| Pinned tabs and folders (nested)  | Pinned tabs and folders (nested, collapsed)               |
| Favorites (top apps)              | Essentials, per container                                 |
| Profile (the one a space uses)    | Firefox container (isolated cookies/logins)               |
| Unpinned tabs                     | Not migrated (`bookmarks --include-unpinned` exports them)|
| Split views, boosts, themes       | Not migrated                                              |

### Profiles and containers

Every Arc space belongs to an Arc profile. Profiles become Zen containers:

- Arc's `Default` profile uses no container.
- Other profiles reuse a Zen container with the **same name** (case-insensitive),
  otherwise a new container with the Arc profile's name is created.
- Override any of this with a JSON file passed as `--map`. Keys are Arc profile
  directory names (see `arc2zen.py profiles`); values are a container name, a numeric
  container id, or `null` for no container:

```json
{ "Profile 3": "Work", "Profile 5": 7, "Default": null }
```

### Limits worth knowing

- Zen allows at most 12 Essentials per container (`zen.tabs.essentials.max`). Extra Arc
  favorites go to an "Arc favorites" folder in the first migrated space of that container.
- Spaces whose name already exists in Zen are skipped, and Essentials already present are
  not duplicated, so re-running is safe. `--force` disables both checks.
- The order of folders relative to loose pinned tabs may differ from Arc.

## Safety

`spaces` edits `zen-sessions.jsonlz4` and `containers.json` in your Zen profile. It:

1. Refuses to run while Zen is running (Zen rewrites the file when it quits).
2. Validates the result (folder/space references, unique ids) before writing.
3. Copies both original files to `arc2zen-backup-<timestamp>/` inside the Zen profile.

To undo, quit Zen and copy the two files from the backup folder back over the originals.

This relies on Zen's internal session format (tested with Zen 1.24), which can change in
future releases. If a Zen update breaks it, please open an issue.

## Options

```
arc2zen.py [--arc-dir DIR] [--zen-dir DIR] {profiles,bookmarks,spaces} ...

spaces:
  --zen-profile DIR      Zen profile to edit (default: the one Zen uses, from profiles.ini)
  --map FILE             JSON overriding Arc profile -> Zen container
  --max-essentials N     Essentials per container (default 12)
  --dry-run              Show what would happen, write nothing
  --force                Create spaces/essentials even if they already exist

bookmarks:
  -o, --output FILE      Output HTML file (default arc-bookmarks.html)
  --include-unpinned     Also export unpinned tabs
```

## Development

```sh
python3 -m unittest discover -s tests
```

## Disclaimer

Not affiliated with The Browser Company or Zen Browser. Use at your own risk and keep
your own backups.
