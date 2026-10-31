import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import sync  # noqa: E402

HTML = """New Device Logged In From {{{device_name}}}
<!---------------->
{{> email/email_header }}
<style type="text/css">body { margin: 0; font-family: Arial; }</style>
<table><tr><td style="color: #333;">
   Your account was just logged into from a new device.
</td></tr><tr><td>
   <b>Date:</b> {{datetime}}
</td></tr><tr><td>
   Open the <a href="{{url}}/">web vault</a> &amp; sign in. See https://example.com/help for details.
   {{#if jwt}}Only shown when set.{{/if}}
</td></tr></table>
{{> email/email_footer }}"""

TEXT = """New Device Logged In From {{{device_name}}}
<!---------------->
Your account was just logged into from a new device.

* Date: {{datetime}}
* IP Address: {{ip}}
{{> email/email_footer_text }}"""


def fake(core: str) -> str:
    return "译:" + core


class TokenizerTests(unittest.TestCase):
    def test_lossless(self):
        for src, html in ((HTML, True), (TEXT, False)):
            parts = sync.tokenize(src, html)
            self.assertEqual("".join(t for _, t in parts), src)

    def test_css_and_comments_are_not_translated(self):
        cores = [s.core for s in sync.find_segments(sync.tokenize(HTML, True), True)]
        self.assertFalse(any("margin" in c or "font-family" in c for c in cores))
        self.assertFalse(any("----" in c for c in cores))

    def test_sentences(self):
        cores = [s.core for s in sync.find_segments(sync.tokenize(HTML, True), True)]
        self.assertIn("New Device Logged In From ⟦1⟧", cores)
        self.assertIn("Your account was just logged into from a new device.", cores)
        self.assertIn("⟦1⟧Date:⟦2⟧ ⟦3⟧", cores)
        # link, entity and bare URL are protected inside the sentence
        self.assertIn(
            "Open the ⟦1⟧web vault⟦2⟧ ⟦3⟧ sign in. See ⟦4⟧ for details.", cores)
        # sentences inside {{#if}} are separate, structural tokens stay out
        self.assertIn("Only shown when set.", cores)

    def test_plain_text_bullets_and_lines(self):
        segs = sync.find_segments(sync.tokenize(TEXT, False), False)
        self.assertEqual([s.lead for s in segs if s.core.startswith("Date")], ["* "])
        self.assertTrue(all("\n" not in s.core for s in segs))

    def test_glued_pieces_become_one_token(self):
        html = sync.find_segments(sync.tokenize("<td>Join the <b>{{org}}</b> team.</td>", True), True)
        self.assertEqual(html[0].core, "Join the \u27e61\u27e7 team.")
        self.assertEqual(html[0].toks, ["<b>{{org}}</b>"])
        text = "Verify: {{url}}/#/verify-email/?userId={{id}}&token={{tok}}\n"
        plain = sync.find_segments(sync.tokenize(text, False), False)
        self.assertEqual(plain[0].core, "Verify: \u27e61\u27e7")
        self.assertEqual(plain[0].toks, ["{{url}}/#/verify-email/?userId={{id}}&token={{tok}}"])

    def test_pairs(self):
        segs = sync.find_segments(sync.tokenize(HTML, True), True)
        self.assertTrue(any(s.pairs == [(1, 2)] for s in segs))


class ValidateTests(unittest.TestCase):
    core = "⟦1⟧Date:⟦2⟧ ⟦3⟧"
    pairs = [(1, 2)]

    def ok(self, out):
        return sync.validate(self.core, self.pairs, out)

    def test_accepts_reordering_and_chinese(self):
        self.assertIsNone(self.ok("⟦1⟧日期：⟦2⟧ ⟦3⟧"))
        self.assertIsNone(self.ok("⟦3⟧ ⟦1⟧日期⟦2⟧"))

    def test_rejects_missing_duplicate_or_extra_tokens(self):
        self.assertIsNotNone(self.ok("⟦1⟧日期：⟦2⟧"))
        self.assertIsNotNone(self.ok("⟦1⟧日期⟦2⟧ ⟦3⟧ ⟦3⟧"))
        self.assertIsNotNone(self.ok("⟦1⟧日期⟦2⟧ ⟦3⟧ ⟦4⟧"))

    def test_rejects_swapped_pair(self):
        self.assertIsNotNone(self.ok("⟦2⟧日期⟦1⟧ ⟦3⟧"))

    def test_rejects_injection(self):
        for bad in ("⟦1⟧<b>日期⟦2⟧ ⟦3⟧", "⟦1⟧{{x}}⟦2⟧ ⟦3⟧",
                    "⟦1⟧https://evil.example⟦2⟧ ⟦3⟧", "⟦1⟧日期\n⟦2⟧ ⟦3⟧"):
            self.assertIsNotNone(self.ok(bad), bad)


class RenderTests(unittest.TestCase):
    def test_markup_is_untouched(self):
        for src, html in ((HTML, True), (TEXT, False)):
            out, left = sync.render(src, html, fake)
            self.assertEqual(left, [])
            self.assertIn("译:", out)
            self.assertTrue(sync.final_check(src, out, html))

    def test_subject_marker_and_css_survive(self):
        out, _ = sync.render(HTML, True, fake)
        self.assertEqual(out.split("\n")[1], "<!---------------->")
        self.assertIn("body { margin: 0; font-family: Arial; }", out)
        self.assertEqual(out.split("\n")[0], "译:New Device Logged In From {{{device_name}}}")

    def test_bad_translation_stays_english(self):
        broken = lambda core: core.replace("⟦2⟧", "") if "Date" in core else "译:" + core  # noqa: E731
        out, left = sync.render(HTML, True, broken)
        self.assertEqual(len(left), 1)
        self.assertIn("<b>Date:</b> {{datetime}}", out)
        self.assertTrue(sync.final_check(HTML, out, True))

    def test_missing_translation_stays_english(self):
        out, left = sync.render(TEXT, False, lambda core: None)
        self.assertEqual(out, TEXT)
        self.assertTrue(left)

    def test_final_check_catches_damage(self):
        self.assertFalse(sync.final_check(HTML, HTML.replace("{{datetime}}", "{{date}}"), True))
        self.assertFalse(sync.final_check(HTML, HTML.replace("https://example.com/help", "https://evil.example"), True))


class VersionTests(unittest.TestCase):
    def test_numeric_compare(self):
        self.assertLess(sync.parse_ver("1.9.0"), sync.parse_ver("1.10.0"))
        self.assertEqual(sync.parse_ver("v1.37.4"), (1, 37, 4))
        with self.assertRaises(SystemExit):
            sync.parse_ver("main")


@unittest.skipUnless(os.environ.get("UPSTREAM_DIR"), "set UPSTREAM_DIR to the official templates/email directory")
class RealTemplates(unittest.TestCase):
    def test_every_official_file_survives(self):
        for p in sorted(Path(os.environ["UPSTREAM_DIR"]).glob("*.hbs")):
            src = p.read_bytes().decode("utf-8")
            html = sync.is_html_file(p.name)
            out, left = sync.render(src, html, fake)
            self.assertEqual(left, [], p.name)
            self.assertTrue(sync.final_check(src, out, html), p.name)


if __name__ == "__main__":
    unittest.main()
