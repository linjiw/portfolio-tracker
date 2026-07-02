import pathlib
import py_compile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class OptionsCreditSpreadWrapperTests(unittest.TestCase):
    def test_wrapper_script_compiles(self):
        py_compile.compile(str(ROOT / "scripts" / "options_credit_spread_backtest.py"), doraise=True)


if __name__ == "__main__":
    unittest.main()
