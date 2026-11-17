"""Static guards for static/js/core/charts.js + static/css/charts.css.

The charts are exercised visually on the app's #/styleguide page (chartsDemo) and by a headless
Chrome self-test page kept outside the project; these tests only pin what can silently regress
without a browser: no HTML-string sinks, the public API the views import, and the validated colour
values (dark chart surface #1a1a19).
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHARTS_JS = ROOT / "static" / "js" / "core" / "charts.js"
CHARTS_CSS = ROOT / "static" / "css" / "charts.css"

SURFACE = "#1a1a19"
CATEGORICAL = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
SEQ_RAMP = ["#184f95", "#256abf", "#3987e5", "#6da7ec", "#9ec5f4"]   # low -> high on dark
EXPORTS = ["columns", "barList", "clock", "clockRadial", "weekday", "weekdayBars", "sparkline",
           "calendarHeat", "yearBars", "setLoading", "showTooltip", "hideTooltip", "formatNumber",
           "chartsDemo"]


def _lin(c):
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _rgb(hex_color):
    h = hex_color.lstrip("#")
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)]


def _luminance(hex_color):
    r, g, b = (_lin(v) for v in _rgb(hex_color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _oklab_l(hex_color):
    r, g, b = (_lin(v) for v in _rgb(hex_color))
    l_ = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m_ = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s_ = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_


class ChartsModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = CHARTS_JS.read_text(encoding="utf-8")
        cls.css = CHARTS_CSS.read_text(encoding="utf-8")

    def test_no_html_string_sinks(self):
        # Labels are untrusted (artist/track names): the module must build DOM with textContent only.
        for pattern in (r"\.innerHTML\b", r"\.outerHTML\b", r"insertAdjacentHTML", r"document\.write",
                        r"\beval\(", r"new Function\(", r"setAttribute\(\s*['\"]style"):
            self.assertIsNone(re.search(pattern, self.js), pattern)

    def test_public_api(self):
        for name in EXPORTS:
            self.assertRegex(self.js, rf"export (function|const) {name}\b", name)
        self.assertIn("window.__chartsDemo = chartsDemo", self.js)
        self.assertIn("export default charts", self.js)

    def test_hrefs_are_filtered(self):
        self.assertIn("SAFE_HREF", self.js)
        self.assertRegex(self.js, r"SAFE_HREF\.test\(")

    def test_no_dashed_rules(self):
        self.assertNotIn("stroke-dasharray", self.css)
        self.assertNotIn("stroke-dasharray", self.js)

    def test_categorical_fallbacks_match_spec(self):
        for i, hex_color in enumerate(CATEGORICAL, start=1):
            self.assertRegex(self.css, rf"--series-{i}:\s*{hex_color};", f"--series-{i}")
            self.assertGreaterEqual(_contrast(hex_color, SURFACE), 3.0, hex_color)

    def test_sequential_ramp_is_validated(self):
        for i, hex_color in enumerate(SEQ_RAMP, start=1):
            self.assertRegex(self.css, rf"--seq-{i}:\s*{hex_color};", f"--seq-{i}")
        ls = [_oklab_l(c) for c in SEQ_RAMP]
        for a, b in zip(ls, ls[1:]):
            self.assertGreater(b, a, "ramp must get lighter step by step on the dark surface")
            self.assertGreaterEqual(b - a, 0.06, "adjacent steps must stay visibly distinct")
        # The step nearest the surface must still read as a mark (ordinal floor 2:1).
        self.assertGreaterEqual(_contrast(SEQ_RAMP[0], SURFACE), 2.0)

    def test_tokens_yield_to_tokens_css(self):
        # Zero-specificity defaults so tokens.css (:root) wins regardless of load order.
        self.assertIn(":where(:root)", self.css)
        self.assertNotRegex(self.css, r"(?m)^:root\s*\{")

    def test_reduced_motion_respected(self):
        self.assertIn("prefers-reduced-motion: reduce", self.css)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_module_imports_without_a_dom(self):
        script = (
            "import(process.argv[1]).then(m => { console.log(JSON.stringify(Object.keys(m))); })"
            ".catch(e => { console.error(e); process.exit(1); });"
        )
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", script, CHARTS_JS.as_uri()],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        names = set(json.loads(proc.stdout.strip().splitlines()[-1]))
        self.assertTrue(set(EXPORTS) <= names, set(EXPORTS) - names)


if __name__ == "__main__":
    unittest.main()
