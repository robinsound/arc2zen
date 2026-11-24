import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import arc2zen  # noqa: E402


def tab(i, url, title):
    return {"id": i, "parentID": None, "childrenIds": [], "title": None,
            "createdAt": 700000000.0,
            "data": {"tab": {"savedURL": url, "savedTitle": title, "timeLastActiveAt": 700000000.0}}}


def container(i, children):
    return {"id": i, "parentID": None, "childrenIds": children, "title": None, "data": {"itemContainer": {}}}


def folder(i, title, children):
    return {"id": i, "parentID": None, "childrenIds": children, "title": title,
            "createdAt": 700000000.0, "data": {"list": {}}}


def make_arc(tmp):
    items = [
        container("pin-a", ["t1", "f1"]), container("unpin-a", ["t9"]),
        container("pin-b", ["t4"]), container("unpin-b", []),
        container("fav-default", ["fa1", "fa2", "fa3"]), container("fav-p1", ["fb1"]),
        tab("t1", "https://example.com/1", "One"),
        folder("f1", "Docs", ["t2", "f2"]),
        tab("t2", "https://example.com/2", "Two"),
        folder("f2", "Nested", ["t3"]),
        tab("t3", "https://example.com/3", "Three <&>"),
        tab("t4", "https://example.com/4", "Four"),
        tab("t9", "https://example.com/unpinned", "Unpinned"),
        tab("fa1", "https://fav.example/1", "Fav 1"), tab("fa2", "https://fav.example/2", "Fav 2"),
        tab("fa3", "https://fav.example/3", "Fav 3"), tab("fb1", "https://fav.example/b", "Fav B"),
    ]
    spaces = [
        {"id": "sa", "title": "Work", "containerIDs": ["pinned", "pin-a", "unpinned", "unpin-a"],
         "profile": {"default": True}, "customInfo": {"iconType": {"emoji_v2": "\U0001F4BC"}}},
        {"id": "sb", "title": "Fun", "containerIDs": ["pinned", "pin-b", "unpinned", "unpin-b"],
         "profile": {"custom": {"_0": {"directoryBasename": "Profile 1"}}}},
    ]
    top = [{"default": True}, "fav-default", {"custom": {"_0": {"directoryBasename": "Profile 1"}}}, "fav-p1"]
    sidebar = {"sidebar": {"containers": [{"global": {}}, {"items": items, "spaces": spaces,
                                                           "topAppsContainerIDs": top}]}}
    arc_dir = os.path.join(tmp, "Arc")
    os.makedirs(os.path.join(arc_dir, "User Data"))
    json.dump(sidebar, open(os.path.join(arc_dir, "StorableSidebar.json"), "w"))
    json.dump({"profile": {"info_cache": {"Default": {"name": "Me"}, "Profile 1": {"name": "Fun Stuff"}}}},
              open(os.path.join(arc_dir, "User Data", "Local State"), "w"))
    return arc_dir


def make_zen(tmp, with_existing_space=False):
    zen_dir = os.path.join(tmp, "zen")
    prof = os.path.join(zen_dir, "Profiles", "abc.Default")
    os.makedirs(prof)
    open(os.path.join(zen_dir, "profiles.ini"), "w").write(
        "[Profile0]\nName=x\nIsRelative=1\nPath=Profiles/abc.Default\nDefault=1\n")
    session = {"lastCollected": 1, "tabs": [], "folders": [], "splitViewData": [], "groups": [],
               "spaces": ([{"uuid": "{old}", "name": "Work", "containerTabId": 0}] if with_existing_space else [])}
    arc2zen.mozlz4_write(os.path.join(prof, "zen-sessions.jsonlz4"), json.dumps(session).encode())
    json.dump({"version": 8, "lastUserContextId": 2, "identities": [
        {"userContextId": 2, "public": True, "icon": "briefcase", "color": "orange", "name": "Other"},
        {"userContextId": 4294967295, "public": False, "name": "userContextIdInternal.webextStorageLocal"}]},
        open(os.path.join(prof, "containers.json"), "w"))
    return zen_dir, prof


class Mozlz4Test(unittest.TestCase):
    def test_roundtrip_small_and_large(self):
        with tempfile.TemporaryDirectory() as tmp:
            for size in (0, 1, 14, 15, 16, 270, 100000):
                data = os.urandom(size)
                p = os.path.join(tmp, "f.jsonlz4")
                arc2zen.mozlz4_write(p, data)
                self.assertEqual(arc2zen.mozlz4_read(p), data)

    def test_decompress_with_match(self):
        # "abcabcabc" encoded by a real LZ4 encoder: literals 'abc', match offset 3 len 6
        block = bytes([0x32]) + b"abc" + bytes([3, 0])
        self.assertEqual(arc2zen.lz4_block_decompress(block, 9), b"abcabcabc")


@mock.patch.object(arc2zen, "zen_is_running", return_value=False)
class MigrationTest(unittest.TestCase):
    def run_cli(self, tmp, *extra, existing=False):
        arc_dir = make_arc(tmp)
        zen_dir, prof = make_zen(tmp, existing)
        code = arc2zen.main(["--arc-dir", arc_dir, "--zen-dir", zen_dir, "spaces", *extra])
        session = json.loads(arc2zen.mozlz4_read(os.path.join(prof, "zen-sessions.jsonlz4")))
        containers = json.load(open(os.path.join(prof, "containers.json")))
        return code, session, containers, prof

    def test_creates_spaces_folders_tabs_and_container(self, _running):
        with tempfile.TemporaryDirectory() as tmp:
            code, s, c, prof = self.run_cli(tmp)
            self.assertEqual(code, 0)
            self.assertEqual([x["name"] for x in s["spaces"]], ["Work", "Fun"])
            self.assertEqual(s["spaces"][0]["icon"], "\U0001F4BC")
            self.assertEqual(s["spaces"][0]["containerTabId"], 0)  # Arc Default -> no container
            new = [i for i in c["identities"] if i["name"] == "Fun Stuff"]
            self.assertEqual(len(new), 1)
            self.assertEqual(s["spaces"][1]["containerTabId"], new[0]["userContextId"])
            self.assertEqual(c["lastUserContextId"], new[0]["userContextId"])
            self.assertEqual({f["name"] for f in s["folders"]}, {"Docs", "Nested"})
            nested = next(f for f in s["folders"] if f["name"] == "Nested")
            docs = next(f for f in s["folders"] if f["name"] == "Docs")
            self.assertEqual(nested["parentId"], docs["id"])
            urls = [t["entries"][0]["url"] for t in s["tabs"] if not t["zenIsEmpty"] and not t["zenEssential"]]
            self.assertEqual(urls, ["https://example.com/1", "https://example.com/2",
                                    "https://example.com/3", "https://example.com/4"])
            self.assertNotIn("https://example.com/unpinned", urls)
            self.assertEqual(sum(t["zenEssential"] for t in s["tabs"]), 4)
            self.assertTrue(any(n.startswith("arc2zen-backup-") for n in os.listdir(prof)))

    def test_essentials_limit_overflows_into_folder(self, _running):
        with tempfile.TemporaryDirectory() as tmp:
            _, s, _, _ = self.run_cli(tmp, "--max-essentials", "2")
            self.assertEqual(sum(t["zenEssential"] and t["userContextId"] == 0 for t in s["tabs"]), 2)
            self.assertIn("Arc favorites", [f["name"] for f in s["folders"]])

    def test_existing_space_is_skipped_and_run_is_idempotent(self, _running):
        with tempfile.TemporaryDirectory() as tmp:
            _, s, _, _ = self.run_cli(tmp, existing=True)
            self.assertEqual([x["name"] for x in s["spaces"]], ["Work", "Fun"])  # old Work + new Fun only
            before = len(s["tabs"])
            arc_dir = os.path.join(tmp, "Arc")
            zen_dir = os.path.join(tmp, "zen")
            arc2zen.main(["--arc-dir", arc_dir, "--zen-dir", zen_dir, "spaces"])
            prof = os.path.join(zen_dir, "Profiles", "abc.Default")
            s2 = json.loads(arc2zen.mozlz4_read(os.path.join(prof, "zen-sessions.jsonlz4")))
            self.assertEqual(len(s2["tabs"]), before)

    def test_dry_run_writes_nothing(self, _running):
        with tempfile.TemporaryDirectory() as tmp:
            _, s, c, prof = self.run_cli(tmp, "--dry-run")
            self.assertEqual(s["tabs"], [])
            self.assertEqual(len(c["identities"]), 2)
            self.assertFalse(any(n.startswith("arc2zen-backup-") for n in os.listdir(prof)))

    def test_map_file_overrides_container(self, _running):
        with tempfile.TemporaryDirectory() as tmp:
            mp = os.path.join(tmp, "map.json")
            json.dump({"Profile 1": "Other"}, open(mp, "w"))
            _, s, c, _ = self.run_cli(tmp, "--map", mp)
            self.assertEqual(s["spaces"][1]["containerTabId"], 2)
            self.assertEqual(len(c["identities"]), 2)  # nothing created


class ContainerOrderTest(unittest.TestCase):
    def test_pinned_found_by_label_whatever_the_order(self):
        sc = arc2zen.ArcData.space_containers
        self.assertEqual(sc({"containerIDs": ["pinned", "P", "unpinned", "U"]}), ("P", "U"))
        self.assertEqual(sc({"containerIDs": ["unpinned", "U", "pinned", "P"]}), ("P", "U"))
        self.assertEqual(sc({"newContainerIDs": [{"unpinned": {"_0": {}}}, "U", {"pinned": {}}, "P"]}), ("P", "U"))
        with self.assertRaises(arc2zen.Arc2ZenError):
            sc({"containerIDs": []})

    def test_export_with_flipped_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            arc_dir = make_arc(tmp)
            path = os.path.join(arc_dir, "StorableSidebar.json")
            data = json.load(open(path))
            for sp in data["sidebar"]["containers"][1]["spaces"]:
                a, b, c, d = sp["containerIDs"]
                sp["containerIDs"] = [c, d, a, b]
            json.dump(data, open(path, "w"))
            out = os.path.join(tmp, "b.html")
            arc2zen.main(["--arc-dir", arc_dir, "bookmarks", "-o", out])
            text = open(out).read()
            self.assertIn("example.com/3", text)
            self.assertNotIn("example.com/unpinned", text)


class BookmarksTest(unittest.TestCase):
    def test_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            arc_dir = make_arc(tmp)
            out = os.path.join(tmp, "b.html")
            self.assertEqual(arc2zen.main(["--arc-dir", arc_dir, "bookmarks", "-o", out]), 0)
            text = open(out).read()
            self.assertIn("Three &lt;&amp;&gt;", text)
            self.assertNotIn("unpinned", text)
            arc2zen.main(["--arc-dir", arc_dir, "bookmarks", "-o", out, "--include-unpinned"])
            self.assertIn("example.com/unpinned", open(out).read())


class RunningGuardTest(unittest.TestCase):
    def test_refuses_when_zen_is_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            arc_dir = make_arc(tmp)
            zen_dir, prof = make_zen(tmp)
            with mock.patch.object(arc2zen, "zen_is_running", return_value=True):
                self.assertEqual(arc2zen.main(["--arc-dir", arc_dir, "--zen-dir", zen_dir, "spaces"]), 1)
            s = json.loads(arc2zen.mozlz4_read(os.path.join(prof, "zen-sessions.jsonlz4")))
            self.assertEqual(s["tabs"], [])


if __name__ == "__main__":
    unittest.main()
