"""前端统计脚本注入测试：站点 ID 合法时启用、留空或非法时禁用。"""
from __future__ import annotations

import unittest

from app.server import analytics_script


class TestAnalyticsScript(unittest.TestCase):
    SITE_ID = "f03589ceff5aac7111bd60cfc7d497ca"

    def test_enabled_with_valid_site_id(self):
        script = analytics_script(self.SITE_ID)
        self.assertIn("enabled: true", script)
        self.assertIn(f"hm.baidu.com/hm.js?{self.SITE_ID}", script)

    def test_uppercase_hex_id_accepted(self):
        self.assertIn("enabled: true", analytics_script(self.SITE_ID.upper()))

    def test_disabled_when_empty(self):
        self.assertIn("enabled: false", analytics_script(""))

    def test_disabled_when_invalid(self):
        bad_ids = ("short", "hello world", "abc'def", "<script>alert(1)</script>", self.SITE_ID + "-x")
        for bad in bad_ids:
            script = analytics_script(bad)
            self.assertIn("enabled: false", script)
            self.assertNotIn("hm.baidu.com", script)  # 非法值绝不产出统计引用，防注入


if __name__ == "__main__":
    unittest.main()
