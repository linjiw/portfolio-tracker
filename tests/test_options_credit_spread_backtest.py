import copy
import json
import pathlib
import py_compile
import tempfile
import unittest

from scripts import options_credit_spread_backtest as wrapper


ROOT = pathlib.Path(__file__).resolve().parents[1]


def valid_vertical_trade(**overrides):
    trade = {
        "entry_date": "2026-01-05",
        "exit_date": "2026-01-09",
        "expiry_date": "2026-01-09",
        "side": "PCS",
        "width": 5.0,
        "credit_per_share": 1.0,
        "contracts": 2,
        "risk_per_contract": 400.0,
        "max_loss_dollars": 805.2,
        "max_gain_dollars": 194.8,
        "pnl_dollars": 100.0,
        "return_on_risk_pct": 12.42,
        "win": True,
        "entry_credit_source": "synthetic",
    }
    trade.update(overrides)
    return trade


def sample_payload(trades=None, *, sweep=None):
    trades = list(trades if trades is not None else [valid_vertical_trade()])
    total_pnl = sum(row["pnl_dollars"] for row in trades)
    total_risk = sum(row["max_loss_dollars"] for row in trades)
    wins = sum(1 for row in trades if row["win"])
    return {
        "schemaVersion": 1,
        "baseline": {
            "summary": {
                "trades": len(trades),
                "wins": wins,
                "losses": len(trades) - wins,
                "total_pnl_dollars": total_pnl,
                "final_equity": 100_000.0 + total_pnl,
                "total_risk_dollars": total_risk,
                "total_return_pct": total_pnl / 100_000.0 * 100.0,
                "return_on_total_risk_pct": total_pnl / total_risk * 100.0,
                "pricing_source": "synthetic_black_scholes",
            },
            "trades": trades,
            "skipped": [
                {"date": "2026-01-12", "reason": "context_error:ValueError"},
                {"date": "2026-01-19", "reason": "PCS:missing_expiry"},
                {"date": "2026-01-26", "reason": "quality_below_min"},
            ],
            "snapshot_rejections": [{"reason": "missing_snapshot"}],
        },
        "sweep": sweep,
        "validation": {"decisionGrade": False},
    }


class OptionsCreditSpreadWrapperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = wrapper.load_core_module()

    def args(self):
        return copy.deepcopy(self.core.parse_args([]))

    def test_wrapper_script_compiles(self):
        py_compile.compile(
            str(ROOT / "scripts" / "options_credit_spread_backtest.py"),
            doraise=True,
        )

    def test_default_arguments_pass_preflight(self):
        self.assertEqual(wrapper.preflight_issues(self.args()), [])

    def test_wrapper_flags_are_stripped_and_recorded(self):
        delegated, waivers = wrapper.split_wrapper_flags(
            [
                "--signal-lag-bars",
                "0",
                "--allow-same-close-sensitivity",
            ]
        )
        self.assertEqual(delegated, ["--signal-lag-bars", "0"])
        self.assertTrue(waivers["allow_same_close_sensitivity"])
        self.assertFalse(waivers["allow_session_expiry_sensitivity"])

    def test_same_bar_signal_requires_explicit_sensitivity_waiver(self):
        args = self.args()
        args.signal_lag_bars = 0
        issues = wrapper.preflight_issues(args)
        same_close = next(
            row for row in issues if row["code"] == "same_close_signal_sensitivity"
        )
        self.assertEqual(same_close["severity"], "block")
        self.assertFalse(same_close["waivedForSensitivity"])

        issues = wrapper.preflight_issues(args, {"allow_same_close_sensitivity": True})
        same_close = next(
            row for row in issues if row["code"] == "same_close_signal_sensitivity"
        )
        self.assertEqual(same_close["severity"], "warning")
        self.assertTrue(same_close["waivedForSensitivity"])

    def test_snapshot_fill_model_cannot_silently_run_synthetic(self):
        args = self.args()
        args.entry_fill_model = "conservative_mid"
        issues = wrapper.preflight_issues(args)
        self.assertIn(
            "snapshot_fill_without_snapshots", {row["code"] for row in issues}
        )

    def test_snapshot_requirements_need_archived_chains(self):
        args = self.args()
        args.require_snapshot_fills = True
        args.min_snapshot_fill_coverage = 80.0
        codes = {row["code"] for row in wrapper.preflight_issues(args)}
        self.assertIn("required_snapshots_missing", codes)
        self.assertIn("snapshot_coverage_without_snapshots", codes)

    def test_same_day_open_is_blocked_because_strikes_use_close_spot(self):
        args = self.args()
        args.entry_timestamp_policy = "same_day_open"
        issues = wrapper.preflight_issues(args)
        mismatch = next(
            row for row in issues if row["code"] == "same_day_open_uses_close_spot"
        )
        self.assertEqual(mismatch["severity"], "block")

    def test_next_open_policy_is_not_claimed_for_synthetic_marks(self):
        args = self.args()
        args.entry_timestamp_policy = "next_open"
        issues = wrapper.preflight_issues(args)
        ignored = next(
            row
            for row in issues
            if row["code"] == "timestamp_policy_ignored_by_synthetic_pricing"
        )
        self.assertEqual(ignored["severity"], "block")

    def test_next_open_snapshot_still_requires_timestamp_mismatch_waiver(self):
        args = self.args()
        args.entry_timestamp_policy = "next_open"
        args.entry_fill_model = "conservative_mid"
        args.use_option_snapshots = "."

        mismatch = next(
            row for row in wrapper.preflight_issues(args)
            if row["code"] == "next_open_uses_prior_close_spot"
        )
        self.assertEqual(mismatch["severity"], "block")

    def test_session_count_expiry_and_explicit_overlap_need_waivers(self):
        args = self.args()
        args.hold_bars = 5
        args.expiry_mode = "session_count"
        args.allow_overlapping_positions = True
        issues = wrapper.preflight_issues(args)
        by_code = {row["code"]: row for row in issues}
        self.assertEqual(
            by_code["session_expiry_not_weekly_friday"]["severity"], "block"
        )
        self.assertEqual(by_code["potential_overlapping_capital"]["severity"], "block")

        issues = wrapper.preflight_issues(
            args,
            {
                "allow_session_expiry_sensitivity": True,
                "allow_overlapping_capital_sensitivity": True,
            },
        )
        by_code = {row["code"]: row for row in issues}
        self.assertEqual(
            by_code["session_expiry_not_weekly_friday"]["severity"], "warning"
        )
        self.assertEqual(
            by_code["potential_overlapping_capital"]["severity"], "warning"
        )

    def test_invalid_quantitative_ranges_fail_closed(self):
        cases = (
            ("credit_haircut_pct", 100.0, "invalid_credit_haircut_pct"),
            ("snapshot_fill_edge", 1.1, "invalid_snapshot_fill_edge"),
            ("min_snapshot_fill_coverage", 101.0, "invalid_min_snapshot_fill_coverage"),
            ("max_short_delta", 0.0, "invalid_max_short_delta"),
            ("risk_free_rate", float("nan"), "invalid_risk_free_rate"),
        )
        for field, value, expected_code in cases:
            with self.subTest(field=field):
                args = self.args()
                setattr(args, field, value)
                codes = {row["code"] for row in wrapper.preflight_issues(args)}
                self.assertIn(expected_code, codes)

    def test_missing_local_inputs_are_blocked_before_execution(self):
        args = self.args()
        args.input_csv = "/definitely/not/a/portfolio-tracker-input.csv"
        codes = {row["code"] for row in wrapper.preflight_issues(args)}
        self.assertIn("missing_input_csv", codes)

    def test_economics_audit_checks_risk_gain_loss_cost_and_ror(self):
        metrics = wrapper._economics_metrics(
            [valid_vertical_trade()],
            commission_per_option_contract=0.65,
        )
        self.assertEqual(metrics["riskIdentityMismatchCount"], 0)
        self.assertEqual(metrics["maxLossIdentityMismatchCount"], 0)
        self.assertEqual(metrics["maxGainIdentityMismatchCount"], 0)
        self.assertEqual(metrics["pnlOutsideDefinedRiskBoundsCount"], 0)
        self.assertEqual(metrics["returnOnRiskDenominatorMismatchCount"], 0)
        self.assertEqual(metrics["embeddedRoundTripCommissionDollars"], 5.2)
        self.assertEqual(metrics["expectedRoundTripCommissionDollars"], 5.2)

        broken = valid_vertical_trade(max_loss_dollars=800.0)
        metrics = wrapper._economics_metrics(
            [broken],
            commission_per_option_contract=0.65,
        )
        self.assertEqual(metrics["maxLossIdentityMismatchCount"], 1)
        self.assertEqual(metrics["returnOnRiskDenominatorMismatchCount"], 1)

        broken = valid_vertical_trade(pnl_dollars=250.0, return_on_risk_pct=31.05)
        metrics = wrapper._economics_metrics(
            [broken], commission_per_option_contract=0.65
        )
        self.assertEqual(metrics["pnlOutsideDefinedRiskBoundsCount"], 1)

    def test_validity_audit_labels_synthetic_clean_run_watch_not_allow(self):
        audit = wrapper.build_validity_audit(sample_payload(), self.args())
        self.assertFalse(audit["decisionGrade"])
        self.assertEqual(audit["liveDecisionLabel"], "BLOCK")
        self.assertEqual(audit["researchUseLabel"], "WATCH")
        self.assertEqual(
            audit["signalAndFill"]["entryPricingClassification"],
            "historical_underlying_synthetic_option_entry_and_exit",
        )
        self.assertEqual(audit["signalAndFill"]["historicalOptionExitCount"], 0)
        self.assertFalse(
            audit["signalAndFill"]["timestampPolicyAppliesToSyntheticPricing"]
        )
        self.assertEqual(audit["calendar"]["nonFridayExpiryCount"], 0)
        self.assertEqual(audit["capital"]["strictOverlapPairs"], 0)
        self.assertEqual(audit["missingData"]["contextErrorCount"], 1)
        self.assertEqual(audit["missingData"]["missingExpiryCount"], 1)
        self.assertEqual(audit["summaryConsistency"]["fieldsChecked"], 8)
        self.assertEqual(audit["summaryConsistency"]["mismatchCount"], 0)

    def test_headline_aggregate_mismatch_blocks_research_result(self):
        payload = sample_payload()
        payload["baseline"]["summary"]["total_pnl_dollars"] = 999.0
        audit = wrapper.build_validity_audit(payload, self.args())
        self.assertEqual(audit["researchUseLabel"], "BLOCK")
        self.assertEqual(audit["summaryConsistency"]["mismatchCount"], 1)
        self.assertEqual(
            audit["summaryConsistency"]["mismatches"][0]["field"],
            "total_pnl_dollars",
        )

    def test_failed_snapshot_coverage_gate_blocks_research_result(self):
        payload = sample_payload(
            [
                valid_vertical_trade(entry_credit_source="snapshot:conservative_mid"),
            ]
        )
        payload["baseline"]["summary"]["snapshot_coverage_gate_pass"] = False
        payload["baseline"]["summary"]["trades_falling_back_to_synthetic"] = 0
        args = self.args()
        args.entry_fill_model = "conservative_mid"
        args.use_option_snapshots = "."
        audit = wrapper.build_validity_audit(payload, args)
        self.assertEqual(audit["researchUseLabel"], "BLOCK")
        self.assertTrue(audit["missingData"]["snapshotCoverageGateFailure"])

    def test_overlap_nonfriday_and_mixed_pricing_block_research_result(self):
        snapshot_trade = valid_vertical_trade(
            entry_date="2026-01-08",
            exit_date="2026-01-12",
            expiry_date="2026-01-12",
            entry_credit_source="snapshot:conservative_mid",
        )
        payload = sample_payload(
            [valid_vertical_trade(), snapshot_trade],
            sweep={"top_test_from_train": [{"name": "candidate"}]},
        )
        audit = wrapper.build_validity_audit(payload, self.args())
        self.assertEqual(audit["researchUseLabel"], "BLOCK")
        self.assertEqual(audit["capital"]["strictOverlapPairs"], 1)
        self.assertEqual(audit["calendar"]["nonFridayExpiryCount"], 1)
        self.assertEqual(
            audit["signalAndFill"]["entryPricingClassification"],
            "mixed_snapshot_and_synthetic_entry_modeled_exit",
        )
        self.assertTrue(audit["calibration"]["chronologicalHoldoutReported"])
        self.assertFalse(audit["calibration"]["nestedWalkForward"])

    def test_same_day_exit_entry_capital_reuse_is_detected(self):
        second = valid_vertical_trade(
            entry_date="2026-01-09",
            exit_date="2026-01-16",
            expiry_date="2026-01-16",
        )
        metrics = wrapper._capital_overlap_metrics([valid_vertical_trade(), second])
        self.assertEqual(metrics["strictOverlapPairs"], 0)
        self.assertEqual(metrics["sameDayExitEntryReusePairs"], 1)
        self.assertEqual(metrics["peakConcurrentPositionsInclusive"], 2)

    def test_prior_session_weekly_expiry_adjustment_is_not_calendar_failure(self):
        adjusted = valid_vertical_trade(
            entry_date="2026-04-01",
            exit_date="2026-04-02",
            expiry_date="2026-04-02",
            expiry_target_date="2026-04-03",
            expiry_mode="weekly_friday",
            expiry_calendar_adjusted=True,
        )

        audit = wrapper.build_validity_audit(sample_payload([adjusted]), self.args())

        self.assertEqual(audit["calendar"]["calendarAdjustedExpiryCount"], 1)
        self.assertEqual(audit["calendar"]["unexpectedNonFridayExpiryCount"], 0)
        self.assertEqual(audit["researchUseLabel"], "WATCH")

    def test_output_annotation_is_machine_readable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = pathlib.Path(tmp)
            (out_dir / "summary.json").write_text(
                json.dumps(sample_payload()),
                encoding="utf-8",
            )
            (out_dir / "report.md").write_text("# Backtest\n", encoding="utf-8")
            args = self.args()
            args.out_dir = str(out_dir)

            first = wrapper.annotate_outputs(out_dir, args)
            second = wrapper.annotate_outputs(out_dir, args)
            payload = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
            report = (out_dir / "report.md").read_text(encoding="utf-8")

            self.assertEqual(first, second)
            self.assertFalse(payload["validation"]["decisionGrade"])
            self.assertTrue(payload["validation"]["researchOnly"])
            self.assertEqual(
                payload["validation"]["wrapperAudit"]["liveDecisionLabel"],
                "BLOCK",
            )
            self.assertEqual(report.count("## Guarded Wrapper Validity Audit"), 1)


if __name__ == "__main__":
    unittest.main()
