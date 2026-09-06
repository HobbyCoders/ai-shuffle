import runpy
import unittest
from pathlib import Path

validate = runpy.run_path(str(Path(__file__).with_name("check-instrumentation.py")))["validate"]


def block(code, name="testOne", total=1):
    return (f"INSTRUMENTATION_STATUS: class=example.SmokeTest\n"
            f"INSTRUMENTATION_STATUS: test={name}\n"
            f"INSTRUMENTATION_STATUS: numtests={total}\n"
            f"INSTRUMENTATION_STATUS_CODE: {code}\n")


class InstrumentationParserTest(unittest.TestCase):
    def setUp(self):
        self.valid = block(1) + block(0) + "OK (1 test)\nINSTRUMENTATION_CODE: -1\n"

    def test_success(self):
        self.assertEqual(1, validate(self.valid))

    def test_whole_suite(self):
        text = "".join(block(1, f"test{i}", 14) + block(0, f"test{i}", 14) for i in range(14))
        self.assertEqual(14, validate(text + "OK (14 tests)\nINSTRUMENTATION_CODE: -1\n"))

    def test_rejects_failed_skipped_assumed_and_unknown_statuses(self):
        for status in (-1, -2, -3, -4, 2):
            with self.subTest(status=status), self.assertRaises(ValueError):
                validate(self.valid.replace("STATUS_CODE: 0", f"STATUS_CODE: {status}"))

    def test_rejects_incomplete_duplicate_or_crashed_runs(self):
        invalid = ["", "OK (0 tests)\nINSTRUMENTATION_CODE: -1\n",
                   self.valid.replace(block(0), ""), self.valid.replace(block(1), ""),
                   self.valid.replace("numtests=1", "numtests=2"),
                   self.valid.replace("OK (1 test)", "OK (2 tests)"),
                   self.valid.replace("INSTRUMENTATION_CODE: -1", "INSTRUMENTATION_CODE: 0"),
                   self.valid + "INSTRUMENTATION_FAILED: Process crashed\n",
                   self.valid + "FAILURES!!!\n", self.valid + block(1),
                   self.valid.replace("INSTRUMENTATION_CODE: -1", "")]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate(text)


if __name__ == "__main__":
    unittest.main()
