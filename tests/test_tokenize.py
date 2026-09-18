"""分词与查询归一化测试（零依赖；jieba 缺失时自动验证回退分词）。"""
from __future__ import annotations

import unittest

from app.tokenize import _cut_fallback, backend_name, tokenize, tokenize_query


class TestTokenize(unittest.TestCase):
    def test_chinese_text_produces_tokens(self):
        tokens = tokenize("表示开心的笑脸")
        self.assertTrue(tokens)
        self.assertTrue(any("心" in token for token in tokens))

    def test_punctuation_and_whitespace_are_dropped(self):
        self.assertEqual(tokenize("，。！？ "), [])

    def test_english_is_lowercased(self):
        self.assertEqual(tokenize("Rocket"), ["rocket"])

    def test_codepoint_becomes_single_token(self):
        self.assertEqual(tokenize("U+1F680"), ["u+1f680"])
        self.assertEqual(tokenize("u+1f600"), ["u+1f600"])

    def test_query_codepoint_is_single_token(self):
        self.assertEqual(tokenize_query("U+1F680"), ["u+1f680"])

    def test_query_of_single_emoji_keeps_whole_char(self):
        tokens = tokenize_query("🚀")
        self.assertEqual(tokens[0], "🚀")

    def test_query_of_zwj_sequence_is_normalized(self):
        tokens = tokenize_query("❤️")
        self.assertIn("❤", tokens)
        self.assertNotIn("\ufe0f", tokens)

    def test_fallback_cut_adds_bigrams_for_cjk(self):
        tokens = _cut_fallback("按键")
        self.assertIn("按", tokens)
        self.assertIn("键", tokens)
        self.assertIn("按键", tokens)

    def test_fallback_cut_keeps_symbols(self):
        self.assertIn("🚀", _cut_fallback("🚀 火箭"))

    def test_backend_name_is_reported(self):
        name = backend_name()
        self.assertTrue(name.startswith("jieba") or name == "char-bigram-fallback")


if __name__ == "__main__":
    unittest.main()
