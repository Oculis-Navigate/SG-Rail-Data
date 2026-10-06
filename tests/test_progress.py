import io
import unittest

from mrt_wayfinding.progress import TerminalProgress


class TerminalProgressTests(unittest.TestCase):
    def test_known_total_renders_bar_percentage_and_completion_newline(self):
        output = io.StringIO()
        with TerminalProgress(stream=output, enabled=True, width=10) as progress:
            progress.update("SgWiki stations", 1, 4, "Example")
            progress.update("SgWiki stations", 4, 4, "Last station")
        rendered = output.getvalue()
        self.assertIn("[##--------] 1/4  25.0%", rendered)
        self.assertIn("[##########] 4/4 100.0%", rendered)
        self.assertIn("Last station", rendered)
        self.assertTrue(rendered.endswith("\n"))

    def test_unknown_total_shows_completed_count(self):
        output = io.StringIO()
        with TerminalProgress(stream=output, enabled=True) as progress:
            progress.update("LTA BusStops", 3, None, "1,000 bus stops")
        self.assertIn("3 complete", output.getvalue())
        self.assertIn("1,000 bus stops", output.getvalue())

    def test_non_terminal_stream_is_quiet_by_default(self):
        output = io.StringIO()
        with TerminalProgress(stream=output) as progress:
            progress.update("Stage 1", 1, 1, "complete")
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
