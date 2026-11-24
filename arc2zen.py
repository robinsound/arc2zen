#!/usr/bin/env python3
"""arc2zen - migrate Arc Browser (macOS) to Zen Browser.

Subcommands
-----------
  profiles    Show how Arc profiles map to Zen containers (read-only).
  bookmarks   Export Arc's pinned tabs/folders as a Netscape bookmarks HTML
              file that Zen/Firefox can import (safe, no profile edits).
  spaces      Recreate Arc spaces as Zen Spaces (workspaces), with pinned
              tabs, folders, Essentials and one container per Arc profile.
              Edits Zen's session file directly: Zen must be closed.

Only the Python 3 standard library is used.
"""
import argparse
import collections
import configparser
import datetime
import html
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import uuid

COCOA_EPOCH = 978307200  # Arc timestamps: seconds since 2001-01-01
ARC_DIR = os.path.expanduser("~/Library/Application Support/Arc")
ZEN_DIR = os.path.expanduser("~/Library/Application Support/zen")

CONTAINER_ICONS = ["briefcase", "fence", "fruit", "chill", "gift", "tree", "pet", "circle"]
CONTAINER_COLORS = ["blue", "turquoise", "green", "yellow", "orange", "red", "pink", "purple"]
ARC_ICON_EMOJI = {"flash": "⚡", "planet": "\U0001FA90"}
DEFAULT_SPACE_ICON = "\U0001F4C1"


class Arc2ZenError(Exception):
    pass


# --------------------------------------------------------------------------
# mozlz4 (Firefox/Zen LZ4 container) - pure python
# --------------------------------------------------------------------------
MOZLZ4_MAGIC = b"mozLz40\0"


def lz4_block_decompress(src, size):
    dst = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        dst += src[i:i + lit]
        i += lit
        if i >= n:
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        mlen = token & 15
        if mlen == 15:
            while True:
                b = src[i]
                i += 1
                mlen += b
                if b != 255:
                    break
        mlen += 4
        start = len(dst) - offset
        for k in range(mlen):
            dst.append(dst[start + k])
    if len(dst) != size:
        raise Arc2ZenError("corrupt mozlz4 data (size mismatch)")
    return bytes(dst)


def lz4_block_literals(data):
    """Valid LZ4 block made of a single literal run (no compression)."""
    n = len(data)
    out = bytearray()
    if n < 15:
        out.append(n << 4)
    else:
        out.append(15 << 4)
        rest = n - 15
        while rest >= 255:
            out.append(255)
            rest -= 255
        out.append(rest)
    out += data
    return bytes(out)


def mozlz4_read(path):
    raw = open(path, "rb").read()
    if raw[:8] != MOZLZ4_MAGIC:
        raise Arc2ZenError(f"{path} is not a mozlz4 file")
    size = struct.unpack("<I", raw[8:12])[0]
    return lz4_block_decompress(raw[12:], size)


def mozlz4_write(path, data):
    blob = MOZLZ4_MAGIC + struct.pack("<I", len(data)) + lz4_block_literals(data)
    with open(path, "wb") as f:
        f.write(blob)


# --------------------------------------------------------------------------
# Arc model
# --------------------------------------------------------------------------
class ArcData:
    def __init__(self, arc_dir):
        self.arc_dir = arc_dir
        path = os.path.join(arc_dir, "StorableSidebar.json")
        if not os.path.exists(path):
            raise Arc2ZenError(f"Arc sidebar file not found: {path}")
        with open(path) as f:
            data = json.load(f)
        containers = [c for c in data["sidebar"]["containers"]
                      if isinstance(c, dict) and "items" in c]
        if not containers:
            raise Arc2ZenError("unexpected StorableSidebar.json layout")
        c = containers[0]
        self.items = {x["id"]: x for x in c["items"] if isinstance(x, dict)}
        self.spaces = [s for s in c["spaces"] if isinstance(s, dict)]
        top = c.get("topAppsContainerIDs", [])
        # list alternates: profile descriptor, favorites container id
        self.favorites = []
        for i in range(0, len(top) - 1, 2):
            if isinstance(top[i], dict) and isinstance(top[i + 1], str):
                self.favorites.append((self.profile_dir(top[i]), top[i + 1]))
        self.profile_names = self._read_profile_names()

    @staticmethod
    def profile_dir(descriptor):
        if "default" in descriptor:
            return "Default"
        return descriptor["custom"]["_0"]["directoryBasename"]

    def space_profile(self, space):
        return self.profile_dir(space.get("profile", {"default": True}))

    def _read_profile_names(self):
        names = {}
        path = os.path.join(self.arc_dir, "User Data", "Local State")
        try:
            with open(path) as f:
                cache = json.load(f)["profile"]["info_cache"]
            for k, v in cache.items():
                names[k] = v.get("name") or k
        except (OSError, KeyError, ValueError):
            pass
        return names

    def profile_label(self, profile_dir):
        return self.profile_names.get(profile_dir, profile_dir)

    def used_profiles(self):
        """Profile dirs used by at least one space or favorites container."""
        seen = []
        for p in [self.space_profile(s) for s in self.spaces] + [p for p, _ in self.favorites]:
            if p not in seen:
                seen.append(p)
        return seen

    def container(self, cid):
        return self.items.get(cid, {})

    @staticmethod
    def space_containers(space):
        """Return (pinned_id, unpinned_id) for a space.

        Arc stores these as a flat [label, id, label, id] list and does NOT keep a
        stable order (it can be [pinned, ..., unpinned, ...] or the reverse), so
        always look the labels up. Newer files also carry `newContainerIDs`, where
        labels are objects such as {"pinned": {}}.
        """
        found = {}
        for key in ("containerIDs", "newContainerIDs"):
            ids = space.get(key) or []
            for i in range(0, len(ids) - 1, 2):
                label, cid = ids[i], ids[i + 1]
                if isinstance(label, dict):
                    label = next(iter(label), None)
                if label in ("pinned", "unpinned") and isinstance(cid, str):
                    found.setdefault(label, cid)
        if "pinned" not in found:
            raise Arc2ZenError(f"cannot find the pinned container of space {space.get('title')!r}")
        return found["pinned"], found.get("unpinned")

    @staticmethod
    def created_ms(item):
        try:
            return int((item["data"]["tab"]["timeLastActiveAt"] + COCOA_EPOCH) * 1000)
        except (KeyError, TypeError):
            return int(time.time() * 1000)


# --------------------------------------------------------------------------
# bookmarks export
# --------------------------------------------------------------------------
def export_bookmarks(arc, out_path, include_unpinned=False):
    lines = []
    stats = {"links": 0, "folders": 0}
    seen = set()

    def w(depth, text):
        lines.append("    " * depth + text)

    def ts(item):
        try:
            return int(item["createdAt"] + COCOA_EPOCH)
        except (KeyError, TypeError):
            return int(time.time())

    def render(item_id, depth):
        item = arc.items.get(item_id)
        if not item or item_id in seen:
            return
        seen.add(item_id)
        data = item.get("data", {})
        if "tab" in data:
            url = data["tab"].get("savedURL")
            if not url:
                return
            title = data["tab"].get("savedTitle") or item.get("title") or url
            w(depth, f'<DT><A HREF="{html.escape(url, quote=True)}" ADD_DATE="{ts(item)}">'
                     f'{html.escape(title)}</A>')
            stats["links"] += 1
        elif "list" in data:
            w(depth, f'<DT><H3 ADD_DATE="{ts(item)}">{html.escape(item.get("title") or "Folder")}</H3>')
            w(depth, "<DL><p>")
            stats["folders"] += 1
            for child in item.get("childrenIds", []):
                render(child, depth + 1)
            w(depth, "</DL><p>")
        else:
            for child in item.get("childrenIds", []):
                render(child, depth)

    def render_container(cid, depth):
        for child in arc.container(cid).get("childrenIds", []):
            render(child, depth)

    w(0, "<!DOCTYPE NETSCAPE-Bookmark-file-1>")
    w(0, '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">')
    w(0, "<TITLE>Bookmarks</TITLE>")
    w(0, "<H1>Bookmarks</H1>")
    w(0, "<DL><p>")
    w(1, "<DT><H3>Arc</H3>")
    w(1, "<DL><p>")
    if arc.favorites:
        w(2, "<DT><H3>Favorites</H3>")
        w(2, "<DL><p>")
        for _, cid in arc.favorites:
            render_container(cid, 3)
        w(2, "</DL><p>")
    for space in arc.spaces:
        pinned, unpinned = arc.space_containers(space)
        w(2, f'<DT><H3>{html.escape(space.get("title") or "Space")}</H3>')
        w(2, "<DL><p>")
        render_container(pinned, 3)
        if include_unpinned:
            w(3, "<DT><H3>Unpinned</H3>")
            w(3, "<DL><p>")
            render_container(unpinned, 4)
            w(3, "</DL><p>")
        w(2, "</DL><p>")
    w(1, "</DL><p>")
    w(0, "</DL><p>")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return stats


# --------------------------------------------------------------------------
# Zen profile helpers
# --------------------------------------------------------------------------
def find_zen_profile(zen_dir):
    ini = os.path.join(zen_dir, "profiles.ini")
    if not os.path.exists(ini):
        raise Arc2ZenError(f"{ini} not found - is Zen installed and launched at least once?")
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    cp.read(ini)
    rel = None
    for section in cp.sections():  # the profile Zen actually uses for this install
        if section.startswith("Install") and cp.has_option(section, "Default"):
            rel = cp.get(section, "Default")
            break
    if rel is None:
        for section in cp.sections():
            if section.startswith("Profile") and cp.get(section, "Default", fallback="0") == "1":
                rel = cp.get(section, "Path")
                break
    if rel is None:
        raise Arc2ZenError("could not determine the default Zen profile; pass --zen-profile")
    return rel if os.path.isabs(rel) else os.path.join(zen_dir, rel)


def zen_is_running():
    try:
        out = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True).stdout
    except OSError:
        return False
    return any("Zen.app/Contents/MacOS/zen" in line for line in out.splitlines())


def load_map_file(path):
    if not path:
        return {}
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise Arc2ZenError("--map must be a JSON object, e.g. {\"Profile 1\": \"Work\", \"Default\": null}")
    return data


class ContainerPlan:
    """Decides which Zen container (userContextId) each Arc profile uses."""

    def __init__(self, arc, containers_json, overrides):
        self.cj = containers_json
        self.identities = containers_json["identities"]
        self.new = []  # identities to create
        self.mapping = {}  # arc profile dir -> userContextId
        self.how = {}  # arc profile dir -> human description
        public = {i["name"].lower(): i for i in self.identities
                  if i.get("public") and i.get("name") and not i["name"].startswith("userContextIdInternal")}
        by_id = {i["userContextId"]: i for i in self.identities if i.get("public")}
        next_id = max([containers_json.get("lastUserContextId", 0)] +
                      [i["userContextId"] for i in self.identities
                       if i["userContextId"] < 4294967295]) + 1
        for n, pdir in enumerate(arc.used_profiles()):
            label = arc.profile_label(pdir)
            if pdir in overrides:
                target = overrides[pdir]
                if target is None or target == 0:
                    self._set(pdir, 0, "no container (default)")
                    continue
                if isinstance(target, int):
                    if target not in by_id:
                        raise Arc2ZenError(f"--map: container id {target} does not exist in Zen")
                    self._set(pdir, target, f"existing container '{by_id[target]['name']}' (mapped)")
                    continue
                label = str(target)
            elif pdir == "Default":
                self._set(pdir, 0, "no container (Arc default profile)")
                continue
            found = public.get(label.lower())
            if found:
                self._set(pdir, found["userContextId"], f"existing container '{found['name']}'")
            else:
                ident = {"userContextId": next_id, "public": True,
                         "icon": CONTAINER_ICONS[n % len(CONTAINER_ICONS)],
                         "color": CONTAINER_COLORS[n % len(CONTAINER_COLORS)], "name": label}
                self.new.append(ident)
                public[label.lower()] = ident
                self._set(pdir, next_id, f"NEW container '{label}'")
                next_id += 1

    def _set(self, pdir, ctx, how):
        self.mapping[pdir] = ctx
        self.how[pdir] = how

    def apply(self):
        for ident in self.new:
            self.identities.append(ident)
            self.cj["lastUserContextId"] = max(self.cj.get("lastUserContextId", 0), ident["userContextId"])
        return self.cj


# --------------------------------------------------------------------------
# spaces migration
# --------------------------------------------------------------------------
def build_spaces(arc, zen, plan, max_essentials=12, force=False):
    """Mutates `zen` (decoded zen-sessions.json) and returns stats."""
    now = int(time.time() * 1000)
    counter = [0]
    stats = collections.Counter()
    seen = set()

    def folder_id():
        counter[0] += 1
        return f"{now}-{counter[0]}"

    def sync_id():
        return f"{now + counter[0]}-{uuid.uuid4()}"

    def make_tab(url, title, ws, ctx, group=None, essential=False, last=now, empty=False):
        tab = {
            "entries": [{"url": "about:blank", "title": None} if empty
                        else {"url": url, "title": title, "cacheKey": 0, "ID": 0}],
            "lastAccessed": last, "pinned": True, "hidden": False,
            "zenWorkspace": None if essential else ws,
            "zenSyncId": sync_id(), "zenEssential": essential,
            "zenDefaultUserContextId": "true" if ctx else None,
            "zenPinnedIcon": None, "zenIsEmpty": empty, "zenHasStaticIcon": False,
            "zenGlanceId": None, "zenIsGlance": False, "zenLiveFolderItemId": None,
            "searchMode": None, "userContextId": ctx, "attributes": {}, "index": 1,
            "userTypedValue": "", "userTypedClear": 0,
        }
        if group:
            tab["groupId"] = group
        if not empty:
            tab["_zenPinnedInitialState"] = {"entry": {"url": url, "title": title}, "image": None}
        return tab

    new_tabs, new_folders, new_groups, new_spaces = [], [], [], []

    def add_folder(name, ws, ctx, parent):
        fid = folder_id()
        placeholder = make_tab(None, None, ws, ctx, group=fid, empty=True)
        new_folders.append({
            "pinned": True, "splitViewGroup": False, "id": fid, "name": name,
            "collapsed": True, "saveOnWindowClose": True, "parentId": parent,
            "prevSiblingInfo": None, "emptyTabIds": [placeholder["zenSyncId"]],
            "userIcon": "", "workspaceId": ws})
        new_groups.append({
            "pinned": True, "splitView": False, "id": fid, "name": name,
            "color": "zen-workspace-color", "collapsed": True, "saveOnWindowClose": True})
        new_tabs.append(placeholder)
        stats["folders"] += 1
        return fid

    def walk(item_id, ws, ctx, parent):
        item = arc.items.get(item_id)
        if not item or item_id in seen:
            return
        seen.add(item_id)
        data = item.get("data", {})
        if "tab" in data:
            url = data["tab"].get("savedURL")
            if not url:
                return
            title = data["tab"].get("savedTitle") or item.get("title") or url
            new_tabs.append(make_tab(url, title, ws, ctx, group=parent, last=arc.created_ms(item)))
            stats["tabs"] += 1
        elif "list" in data:
            fid = add_folder(item.get("title") or "Folder", ws, ctx, parent)
            for child in item.get("childrenIds", []):
                walk(child, ws, ctx, fid)
        else:
            for child in item.get("childrenIds", []):
                walk(child, ws, ctx, parent)

    existing_names = {s["name"] for s in zen.get("spaces", [])}

    # --- spaces first (so overflow favorites can find a home)
    created_spaces = []
    for space in arc.spaces:
        name = space.get("title") or "Space"
        if name in existing_names and not force:
            stats["spaces_skipped"] += 1
            continue
        ctx = plan.mapping[arc.space_profile(space)]
        ws = "{" + str(uuid.uuid4()) + "}"
        icon_info = (space.get("customInfo") or {}).get("iconType") or {}
        icon = icon_info.get("emoji_v2") or ARC_ICON_EMOJI.get(icon_info.get("icon")) or DEFAULT_SPACE_ICON
        new_spaces.append({
            "uuid": ws, "icon": icon, "name": name,
            "theme": {"type": "gradient", "gradientColors": [], "opacity": 0.5, "texture": 0},
            "containerTabId": ctx, "hasCollapsedPinnedTabs": False})
        created_spaces.append((space, ws, ctx))
        stats["spaces"] += 1

    for space, ws, ctx in created_spaces:
        for child in arc.container(arc.space_containers(space)[0]).get("childrenIds", []):
            walk(child, ws, ctx, None)

    # --- favorites -> Essentials (max N per container, rest into a folder)
    ess_count = collections.Counter(t["userContextId"] for t in zen.get("tabs", []) if t.get("zenEssential"))
    known = {(t["userContextId"], t["entries"][-1]["url"]) for t in zen.get("tabs", [])
             if t.get("zenEssential") and t.get("entries")}
    overflow = collections.defaultdict(list)
    for pdir, cid in arc.favorites:
        ctx = plan.mapping.get(pdir)
        if ctx is None:
            continue
        for child in arc.container(cid).get("childrenIds", []):
            item = arc.items.get(child)
            tab = (item or {}).get("data", {}).get("tab")
            if not tab or not tab.get("savedURL"):
                continue
            url, title = tab["savedURL"], tab.get("savedTitle") or tab["savedURL"]
            if (ctx, url) in known and not force:
                stats["essentials_skipped"] += 1
                continue
            if ess_count[ctx] < max_essentials:
                ess_count[ctx] += 1
                known.add((ctx, url))
                new_tabs.append(make_tab(url, title, None, ctx, essential=True, last=arc.created_ms(item)))
                stats["essentials"] += 1
            else:
                overflow[ctx].append((url, title, arc.created_ms(item)))
    for ctx, entries in overflow.items():
        home = next((ws for _, ws, c in created_spaces if c == ctx), None)
        if home is None:  # target space already existed: no safe place, report only
            stats["favorites_dropped"] += len(entries)
            continue
        fid = add_folder("Arc favorites", home, ctx, None)
        for url, title, last in entries:
            new_tabs.append(make_tab(url, title, home, ctx, group=fid, last=last))
            stats["favorites_in_folder"] += 1

    zen["spaces"] = zen.get("spaces", []) + new_spaces
    zen["folders"] = zen.get("folders", []) + new_folders
    zen["groups"] = zen.get("groups", []) + new_groups
    zen["tabs"] = zen.get("tabs", []) + new_tabs
    return stats


def check_integrity(zen):
    folder_ids = {f["id"] for f in zen["folders"]}
    for f in zen["folders"]:
        assert f["parentId"] is None or f["parentId"] in folder_ids, "dangling folder parent"
    for t in zen["tabs"]:
        assert not t.get("groupId") or t["groupId"] in folder_ids, "tab in missing folder"
    ids = [t["zenSyncId"] for t in zen["tabs"]]
    assert len(ids) == len(set(ids)), "duplicate zenSyncId"
    spaces = {s["uuid"] for s in zen["spaces"]}
    for t in zen["tabs"]:
        assert t["zenEssential"] or t["zenWorkspace"] in spaces or t["zenWorkspace"] is None, "tab in missing space"


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_profiles(args):
    arc = ArcData(args.arc_dir)
    zen_profile = args.zen_profile or find_zen_profile(args.zen_dir)
    with open(os.path.join(zen_profile, "containers.json")) as f:
        cj = json.load(f)
    plan = ContainerPlan(arc, cj, load_map_file(args.map))
    print(f"Zen profile: {zen_profile}\n")
    print(f"{'Arc profile dir':<16} {'Arc name':<28} {'Spaces':<30} Zen container")
    for pdir in arc.used_profiles():
        spaces = ", ".join(s.get("title", "?") for s in arc.spaces if arc.space_profile(s) == pdir) or "-"
        print(f"{pdir:<16} {arc.profile_label(pdir):<28} {spaces[:29]:<30} {plan.how[pdir]}")
    print("\nOverride with --map file.json, e.g. {\"Profile 3\": \"Work\", \"Default\": null}")


def cmd_bookmarks(args):
    arc = ArcData(args.arc_dir)
    stats = export_bookmarks(arc, args.output, args.include_unpinned)
    print(f"Wrote {args.output}: {stats['links']} links, {stats['folders']} folders.")
    print("Import in Zen: Cmd+Shift+O > Import and Backup > Import Bookmarks from HTML...")


def cmd_spaces(args):
    arc = ArcData(args.arc_dir)
    zen_profile = args.zen_profile or find_zen_profile(args.zen_dir)
    sessions = os.path.join(zen_profile, "zen-sessions.jsonlz4")
    containers = os.path.join(zen_profile, "containers.json")
    for p in (sessions, containers):
        if not os.path.exists(p):
            raise Arc2ZenError(f"{p} not found. Launch Zen once (and quit it) first.")
    if not args.dry_run and zen_is_running() and not args.allow_running:
        raise Arc2ZenError("Zen is running. Quit it (Cmd+Q) first - it rewrites this file on exit.")

    zen = json.loads(mozlz4_read(sessions))
    with open(containers) as f:
        cj = json.load(f)
    plan = ContainerPlan(arc, cj, load_map_file(args.map))

    print(f"Zen profile: {zen_profile}")
    for pdir in arc.used_profiles():
        print(f"  Arc profile {pdir!r} ({arc.profile_label(pdir)}) -> {plan.how[pdir]}")
    stats = build_spaces(arc, zen, plan, max_essentials=args.max_essentials, force=args.force)
    check_integrity(zen)
    print("\nResult" + (" (dry run, nothing written)" if args.dry_run else ""))
    for key in ("spaces", "spaces_skipped", "folders", "tabs", "essentials", "essentials_skipped",
                "favorites_in_folder", "favorites_dropped"):
        if stats.get(key):
            print(f"  {key.replace('_', ' '):<22} {stats[key]}")
    if stats.get("spaces_skipped"):
        print("  (spaces whose name already exists in Zen are skipped; use --force to duplicate)")
    if args.dry_run:
        return

    cj = plan.apply()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup, n = os.path.join(zen_profile, "arc2zen-backup-" + stamp), 1
    while os.path.exists(backup):
        n += 1
        backup = os.path.join(zen_profile, f"arc2zen-backup-{stamp}-{n}")
    os.makedirs(backup)
    shutil.copy2(sessions, backup)
    shutil.copy2(containers, backup)
    mozlz4_write(sessions, json.dumps(zen, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    with open(containers, "w") as f:
        json.dump(cj, f, separators=(",", ":"))
    print(f"\nDone. Backup of the original files: {backup}")
    print(f"To undo: quit Zen and copy the two files from that folder back into {zen_profile}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="arc2zen", description=__doc__.split("\n\n")[0])
    parser.add_argument("--arc-dir", default=ARC_DIR, help="Arc data directory")
    parser.add_argument("--zen-dir", default=ZEN_DIR, help="Zen data directory")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("profiles", help="show the Arc profile -> Zen container mapping")
    p.add_argument("--zen-profile", help="Zen profile directory (default: auto-detect)")
    p.add_argument("--map", help="JSON file overriding the profile -> container mapping")
    p.set_defaults(func=cmd_profiles)

    p = sub.add_parser("bookmarks", help="export Arc pinned tabs as a bookmarks HTML file")
    p.add_argument("-o", "--output", default="arc-bookmarks.html")
    p.add_argument("--include-unpinned", action="store_true", help="also export unpinned tabs")
    p.set_defaults(func=cmd_bookmarks)

    p = sub.add_parser("spaces", help="create Zen Spaces from Arc spaces (Zen must be closed)")
    p.add_argument("--zen-profile", help="Zen profile directory (default: auto-detect)")
    p.add_argument("--map", help="JSON file overriding the profile -> container mapping")
    p.add_argument("--max-essentials", type=int, default=12,
                   help="Essentials per container (Zen's default limit is 12)")
    p.add_argument("--dry-run", action="store_true", help="show what would happen, write nothing")
    p.add_argument("--force", action="store_true", help="create spaces/essentials even if they already exist")
    p.add_argument("--allow-running", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_spaces)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except Arc2ZenError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
