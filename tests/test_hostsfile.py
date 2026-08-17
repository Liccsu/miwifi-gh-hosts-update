"""hostsfile 模块单元测试。"""

import unittest

from app.hostsfile import MAX_HOSTS_LEN, merge, normalize_entries, parse_hosts, total_length


class ParseHostsTest(unittest.TestCase):
    def test_skips_comments_and_blank_lines(self):
        content = (
            "# GitHub IP hosts Start\n"
            "\n"
            "140.82.113.25   alive.github.com   # 行内注释\n"
            "  185.199.108.133  avatars.githubusercontent.com\n"
        )
        self.assertEqual(
            parse_hosts(content),
            [
                "140.82.113.25 alive.github.com",
                "185.199.108.133 avatars.githubusercontent.com",
            ],
        )

    def test_drops_lines_without_two_columns(self):
        self.assertEqual(parse_hosts("just-a-word\n"), [])

    def test_keeps_multiple_domains_on_one_line(self):
        self.assertEqual(parse_hosts("1.2.3.4 a.com b.com\n"), ["1.2.3.4 a.com b.com"])


class NormalizeTest(unittest.TestCase):
    def test_normalizes_whitespace_and_comments(self):
        self.assertEqual(
            normalize_entries(["1.2.3.4    foo.com", "# comment", "5.6.7.8 bar.com #x"]),
            ["1.2.3.4 foo.com", "5.6.7.8 bar.com"],
        )

    def test_drops_invalid_lines(self):
        self.assertEqual(normalize_entries(["nonsense", "", 42]), [])


class MergeTest(unittest.TestCase):
    def test_managed_domains_replaced_others_kept(self):
        existing = [
            "1.1.1.1 github.com",  # 托管域名 -> 替换
            "9.9.9.9 my-router.local",  # 手动条目 -> 保留
        ]
        managed = ["2.2.2.2 github.com", "3.3.3.3 api.github.com"]
        self.assertEqual(
            merge(existing, managed),
            ["9.9.9.9 my-router.local", "2.2.2.2 github.com", "3.3.3.3 api.github.com"],
        )

    def test_duplicate_managed_domains_all_replaced(self):
        existing = ["1.1.1.1 github.com", "1.1.1.2 github.com"]
        managed = ["2.2.2.2 github.com"]
        self.assertEqual(merge(existing, managed), ["2.2.2.2 github.com"])

    def test_managed_domain_substring_not_matched(self):
        existing = ["1.1.1.1 notgithub.com"]
        managed = ["2.2.2.2 github.com"]
        self.assertEqual(merge(existing, managed), ["1.1.1.1 notgithub.com", "2.2.2.2 github.com"])

    def test_no_existing(self):
        self.assertEqual(merge([], ["2.2.2.2 github.com"]), ["2.2.2.2 github.com"])


class LengthTest(unittest.TestCase):
    def test_total_length_counts_join_with_newline(self):
        self.assertEqual(total_length(["a", "b"]), 3)

    def test_realistic_payload_under_limit(self):
        managed = [f"1.2.3.{i % 255} example{i}.com" for i in range(200)]
        merged = merge([], managed)
        self.assertLessEqual(total_length(merged), MAX_HOSTS_LEN)


if __name__ == "__main__":
    unittest.main()
