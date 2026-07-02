import importlib.util
import pathlib
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "archive_option_chain_snapshots.py"
SPEC = importlib.util.spec_from_file_location("archive_option_chain_snapshots", SCRIPT)
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)


class ArchiveOptionChainSnapshotsTests(unittest.TestCase):
    def test_output_path_partitions_by_ticker_year_and_month(self):
        path = archive.output_path(
            "/tmp/snapshots",
            "QQQ",
            "2026-06-26T15:30:00-04:00",
            "2026-07-03",
        )

        self.assertEqual(pathlib.Path("/tmp/snapshots/QQQ/2026/06"), path.parent)
        self.assertEqual("2026-06-26_153000_ET_QQQ_2026-07-03.csv", path.name)

    def test_raw_and_metadata_paths_are_partitioned_by_snapshot_date(self):
        raw = archive.raw_output_path(
            "/tmp/snapshots",
            "QQQ",
            "2026-06-26T15:30:00-04:00",
            "2026-07-03",
        )
        metadata = archive.metadata_output_path(
            "/tmp/snapshots",
            "QQQ",
            "2026-06-26T15:30:00-04:00",
            "2026-07-03",
        )

        self.assertEqual(pathlib.Path("/tmp/snapshots/QQQ/raw/2026-06-26"), raw.parent)
        self.assertEqual("2026-06-26_153000_ET_QQQ_2026-07-03_raw.json", raw.name)
        self.assertEqual(pathlib.Path("/tmp/snapshots/QQQ/metadata/2026-06-26"), metadata.parent)
        self.assertEqual("2026-06-26_153000_ET_QQQ_2026-07-03_metadata.json", metadata.name)

    def test_bs_greeks_return_option_type_specific_delta(self):
        call = archive.bs_greeks(100, 105, 7, 0.30, "call")
        put = archive.bs_greeks(100, 95, 7, 0.30, "put")

        self.assertGreater(call["model_delta"], 0.0)
        self.assertLess(call["model_delta"], 1.0)
        self.assertLess(put["model_delta"], 0.0)
        self.assertGreater(put["model_delta"], -1.0)

    def test_normalize_option_row_adds_snapshot_and_model_fields(self):
        args = archive.parse_args(["--skip-gravity-state"])
        row = archive.normalize_option_row(
            {
                "contractSymbol": "QQQ260703C00720000",
                "strike": 720,
                "bid": 1.2,
                "ask": 1.4,
                "lastPrice": 1.25,
                "volume": 10,
                "openInterest": 100,
                "impliedVolatility": 0.25,
                "inTheMoney": False,
            },
            "call",
            args,
            "2026-06-26T15:30:00-04:00",
            "QQQ",
            706.52,
            "2026-07-03",
            None,
            None,
        )

        self.assertEqual(row["snapshot_date"], "2026-06-26")
        self.assertEqual(row["expiry"], "2026-07-03")
        self.assertEqual(row["option_type"], "call")
        self.assertAlmostEqual(row["mid"], 1.3)
        self.assertIn("model_delta", row)
        self.assertEqual(row["source"], "yfinance")

    def test_snapshot_metadata_records_rebuild_inputs(self):
        args = archive.parse_args(["--ticker", "QQQ"])
        metadata = archive.snapshot_metadata(
            args,
            "2026-06-26T15:30:00-04:00",
            706.52,
            "2026-07-03",
            [{"strike": 720}],
            2,
        )

        self.assertEqual(metadata["source"], "yfinance")
        self.assertEqual(metadata["underlying"], "QQQ")
        self.assertEqual(metadata["normalized_rows"], 1)
        self.assertEqual(metadata["raw_rows"], 2)
        self.assertEqual(metadata["timezone"], "America/New_York")


if __name__ == "__main__":
    unittest.main()
