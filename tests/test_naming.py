"""여러 영상 글의 파일 네이밍/덮어쓰기 방지 테스트 (네트워크·브라우저 불필요)."""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import download as dl  # noqa: E402
import main  # noqa: E402


class NamingTests(unittest.TestCase):
    def setUp(self):
        main._names_claimed.clear()
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.out = Path(td.name)

    def reserve(self, name, url, force=False, prev=None, create=True):
        n = main._reserve_name(name, url, self.out, force, prev)
        if create:
            (self.out / f"{n}.mp4").write_text("x")
        return n

    def test_index_suffix_survives_long_title_even_with_force(self):
        title = "T" * 118
        names = [self.reserve(f"{title} ({i:02d})", f"https://s/p#v{i:02d}", force=True) for i in (1, 2, 3)]
        self.assertEqual(len({n.lower() for n in names}), 3)
        self.assertTrue(all(n.endswith(f"({i:02d})") for i, n in zip((1, 2, 3), names)))
        self.assertTrue(all(len(n) <= main.MAX_NAME_LEN for n in names))

    def test_many_videos_get_distinct_ordered_names(self):
        names = [self.reserve(f"Series ({i:02d})", f"https://s/p#v{i:02d}") for i in range(1, 31)]
        self.assertEqual(len(set(names)), 30)
        self.assertEqual(names[0], "Series (01)")
        self.assertEqual(names[29], "Series (30)")

    def test_same_title_different_posts_never_overwrite_even_with_force(self):
        a = self.reserve("Same Title", "https://s/a/", force=True)
        b = self.reserve("Same Title", "https://s/b/", force=True)
        self.assertNotEqual(a.lower(), b.lower())
        self.assertTrue(b.startswith("Same Title ["))

    def test_case_only_difference_is_a_collision(self):
        a = self.reserve("Movie", "https://s/a/")
        b = self.reserve("movie", "https://s/b/")
        self.assertNotEqual(a.lower(), b.lower())

    def test_sanitized_collision_gets_hash(self):
        a = self.reserve("A: B", "https://s/a/")
        b = self.reserve("A_ B", "https://s/b/")
        self.assertNotEqual(a.lower(), b.lower())

    def test_force_overwrites_only_its_own_previous_file(self):
        first = self.reserve("Post", "https://s/a/")
        main._names_claimed.clear()             # 다음 실행
        again = self.reserve("Post", "https://s/a/", force=True, prev=self.out / f"{first}.mp4", create=False)
        self.assertEqual(again, first)          # 자기 파일은 덮어쓰기
        main._names_claimed.clear()
        other = self.reserve("Post", "https://s/other/", force=True, prev=None, create=False)
        self.assertNotEqual(other.lower(), first.lower())

    def test_without_force_existing_file_is_kept(self):
        first = self.reserve("Post", "https://s/a/")
        main._names_claimed.clear()
        second = self.reserve("Post", "https://s/a/", force=False, create=False)
        self.assertNotEqual(second.lower(), first.lower())

    def test_same_item_may_reuse_its_name_on_retry(self):
        a = self.reserve("Post", "https://s/a/", create=False)
        self.assertEqual(self.reserve("Post", "https://s/a/", create=False), a)

    def test_long_output_dir_shrinks_name_limit(self):
        deep = self.out
        for i in range(5):
            deep = deep / ("d" * 20)
        deep.mkdir(parents=True)
        n = main._reserve_name("N" * 118 + " (02)", "https://s/p#v02", deep, False)
        self.assertLessEqual(len(str(deep)) + 1 + len(n) + 30, 260)
        self.assertTrue(n.endswith("(02)"))

    def test_hash_name_keeps_index_suffix(self):
        self.reserve("Dup (02)", "https://s/a#v02")
        n = self.reserve("Dup (02)", "https://s/b#v02")
        self.assertRegex(n, r"^Dup \(02\) \[[0-9a-f]{6}\]$")


class RelatedFilesTests(unittest.TestCase):
    def test_cleanup_does_not_touch_other_items_with_dotted_names(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "Vol.1.mp4").write_text("finished other video")
            (out / "Vol.mp4.part").write_text("partial")
            (out / "Vol.f137.mp4").write_text("fragment")
            self.assertEqual(sorted(p.name for p in dl._related_files(out, "Vol")), ["Vol.f137.mp4", "Vol.mp4.part"])
            dl._cleanup_partial(out, "Vol", time.time() - 5)
            self.assertEqual([p.name for p in out.iterdir()], ["Vol.1.mp4"])


if __name__ == "__main__":
    unittest.main()
