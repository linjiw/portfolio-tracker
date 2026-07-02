import datetime as dt
import importlib.util
import math
import pathlib
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "market_mass_credit_spread_backtest.py"
SPEC = importlib.util.spec_from_file_location("market_mass_credit_spread_backtest", SCRIPT)
backtest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backtest)


def rows(n=180, start_price=100.0):
    out = []
    day = dt.date(2024, 1, 1)
    added = 0
    while added < n:
        if day.weekday() < 5:
            price = start_price + 1.2 * math.sin(added / 4.0) + 0.04 * added
            out.append({
                "date": day.isoformat(),
                "open": price,
                "high": price * 1.006,
                "low": price * 0.994,
                "close": price,
                "volume": 1_000_000,
                "dollar_volume": price * 1_000_000,
            })
            added += 1
        day += dt.timedelta(days=1)
    return out


class MarketMassCreditSpreadBacktestTests(unittest.TestCase):
    def test_call_spread_mark_increases_when_spot_rises(self):
        low = backtest.spread_mark(100, 105, 110, "CCS", 7 / 365, 0.30)
        high = backtest.spread_mark(108, 105, 110, "CCS", 7 / 365, 0.30)

        self.assertGreater(low, 0)
        self.assertLess(low, 5)
        self.assertGreater(high, low)

    def test_put_and_call_strikes_are_otm_and_width_apart(self):
        c_short, c_long = backtest.choose_short_strike("CCS", 100, 103.2, 5, 1, 0.01)
        p_short, p_long = backtest.choose_short_strike("PCS", 100, 96.8, 5, 1, 0.01)

        self.assertEqual((c_short, c_long), (104, 109))
        self.assertEqual((p_short, p_long), (96, 91))

    def test_boundary_buffer_pushes_short_strike_farther_out(self):
        c_short, c_long = backtest.choose_short_strike("CCS", 100, 103.2, 5, 1, 0.01, 0.02)
        p_short, p_long = backtest.choose_short_strike("PCS", 100, 96.8, 5, 1, 0.01, 0.02)

        self.assertEqual((c_short, c_long), (106, 111))
        self.assertEqual((p_short, p_long), (94, 89))

    def test_mass_band_maps_percent_distance_to_trade_price(self):
        state = {"center": {"current_price": 20000.0}}
        band = {"lower_boundary": 19000.0, "upper_boundary": 21000.0}

        mapped = backtest.map_mass_band_to_trade(band, state, 500.0)

        self.assertAlmostEqual(mapped["lower_boundary"], 475.0)
        self.assertAlmostEqual(mapped["upper_boundary"], 525.0)
        self.assertAlmostEqual(mapped["mass_to_trade_spot_ratio"], 0.025)

    def test_mass_adaptive_skips_detached_center(self):
        center = {"regime": "detached_from_mass", "quality_score": 80, "distance_z": 3.0}
        build = {"score": 20}

        self.assertEqual(backtest.side_candidates("mass_adaptive", "overheat", center, build), [])

    def test_simulate_trade_returns_defined_risk_fields(self):
        sample = rows(20)
        state = {
            "center": {
                "regime": "active_center",
                "quality_score": 80,
                "distance_z": 0.0,
                "center_price": 100,
            },
            "volatility": {"annual_vol_used": 0.45},
            "volatility_build_up": {"score": 20},
        }
        band = {"lower_boundary": 98.0, "upper_boundary": 103.0}
        cfg = backtest.config_with(
            side_policy="pcs_only",
            min_credit_risk=0.0,
            max_short_delta=1.0,
            width=5.0,
            hold_bars=4,
            risk_per_trade_pct=0.01,
            max_contracts=3,
        )

        trade = backtest.simulate_trade(
            sample,
            0,
            "PCS",
            band,
            state,
            cfg,
            capital=100000,
            commission_per_option_contract=0.0,
        )

        self.assertIsNotNone(trade)
        self.assertEqual(trade["side"], "PCS")
        self.assertGreater(trade["contracts"], 0)
        self.assertGreater(trade["max_loss_dollars"], 0)
        self.assertIn("exit_reason", trade)
        self.assertIn("gravity_score", trade)
        self.assertIn("levitation_score", trade)
        self.assertIn("short_hvn_distance_em", trade)

    def test_credit_haircut_reduces_modeled_credit(self):
        sample = rows(20)
        state = {
            "center": {"regime": "active_center", "quality_score": 80, "distance_z": 0.0, "center_price": 100},
            "volatility": {"annual_vol_used": 0.45},
            "volatility_build_up": {"score": 20},
        }
        band = {"lower_boundary": 98.0, "upper_boundary": 103.0}
        base_cfg = backtest.config_with(
            side_policy="pcs_only",
            min_credit_risk=0.0,
            max_short_delta=1.0,
            width=5.0,
            hold_bars=4,
            risk_per_trade_pct=0.01,
            max_contracts=3,
        )
        haircut_cfg = dict(base_cfg)
        haircut_cfg["credit_haircut_pct"] = 25.0

        base = backtest.simulate_trade(sample, 0, "PCS", band, state, base_cfg, capital=100000, commission_per_option_contract=0.0)
        haircut = backtest.simulate_trade(sample, 0, "PCS", band, state, haircut_cfg, capital=100000, commission_per_option_contract=0.0)

        self.assertIsNotNone(base)
        self.assertIsNotNone(haircut)
        self.assertLess(haircut["credit_per_share"], base["credit_per_share"])

    def test_snapshot_spread_credit_uses_conservative_mid(self):
        store = backtest.OptionSnapshotStore([
            {
                "snapshot_ts": "2024-01-01T15:30:00-05:00",
                "expiry": "2024-01-05",
                "option_type": "put",
                "strike": "98",
                "bid": "1.20",
                "ask": "1.40",
                "mid": "1.30",
            },
            {
                "snapshot_ts": "2024-01-01T15:30:00-05:00",
                "expiry": "2024-01-05",
                "option_type": "put",
                "strike": "93",
                "bid": "0.20",
                "ask": "0.40",
                "mid": "0.30",
            },
        ])

        credit = backtest.snapshot_spread_credit(
            store,
            "2024-01-01",
            "2024-01-05",
            "PCS",
            98,
            93,
            fill_model="conservative_mid",
        )

        self.assertIsNotNone(credit)
        self.assertAlmostEqual(credit["snapshot_mid_credit"], 1.0)
        self.assertAlmostEqual(credit["snapshot_natural_credit"], 0.8)
        self.assertAlmostEqual(credit["credit"], 0.95)

    def test_snapshot_store_rejects_stale_snapshot_by_minutes(self):
        store = backtest.OptionSnapshotStore([
            {
                "snapshot_ts": "2024-01-01T14:00:00-05:00",
                "expiry": "2024-01-05",
                "option_type": "put",
                "strike": "98",
                "bid": "1.20",
                "ask": "1.40",
                "mid": "1.30",
            },
        ], max_age_minutes=30)

        contract, reason = store.find_contract(
            "2024-01-01",
            "2024-01-05",
            "put",
            98,
            timestamp_policy="same_day_close",
        )

        self.assertIsNone(contract)
        self.assertEqual(reason, "stale_snapshot")

    def test_simulate_trade_can_replay_snapshot_entry_credit(self):
        sample = rows(20)
        state = {
            "center": {"regime": "active_center", "quality_score": 80, "distance_z": 0.0, "center_price": 100},
            "volatility": {"annual_vol_used": 0.45},
            "volatility_build_up": {"score": 20},
        }
        band = {"lower_boundary": 98.0, "upper_boundary": 103.0}
        cfg = backtest.config_with(
            side_policy="pcs_only",
            entry_fill_model="natural",
            min_credit_risk=0.0,
            max_short_delta=1.0,
            width=5.0,
            hold_bars=4,
            risk_per_trade_pct=0.01,
            max_contracts=3,
        )
        store = backtest.OptionSnapshotStore([
            {
                "snapshot_ts": f"{sample[0]['date']}T15:30:00-05:00",
                "expiry": sample[4]["date"],
                "option_type": "put",
                "strike": "97",
                "bid": "1.20",
                "ask": "1.40",
                "mid": "1.30",
            },
            {
                "snapshot_ts": f"{sample[0]['date']}T15:30:00-05:00",
                "expiry": sample[4]["date"],
                "option_type": "put",
                "strike": "92",
                "bid": "0.20",
                "ask": "0.40",
                "mid": "0.30",
            },
        ])

        trade = backtest.simulate_trade(
            sample,
            0,
            "PCS",
            band,
            state,
            cfg,
            capital=100000,
            commission_per_option_contract=0.0,
            option_snapshots=store,
        )

        self.assertIsNotNone(trade)
        self.assertEqual(trade["entry_credit_source"], "snapshot:natural")
        self.assertAlmostEqual(trade["credit_per_share"], 0.8)
        self.assertIn("credit_error_pct", trade)

    def test_snapshot_missing_leg_returns_structured_rejection(self):
        sample = rows(20)
        state = {
            "center": {"regime": "active_center", "quality_score": 80, "distance_z": 0.0, "center_price": 100},
            "volatility": {"annual_vol_used": 0.45},
            "volatility_build_up": {"score": 20},
        }
        band = {"lower_boundary": 98.0, "upper_boundary": 103.0}
        cfg = backtest.config_with(
            side_policy="pcs_only",
            entry_fill_model="natural",
            min_credit_risk=0.0,
            max_short_delta=1.0,
            width=5.0,
            hold_bars=4,
            risk_per_trade_pct=0.01,
            max_contracts=3,
        )
        store = backtest.OptionSnapshotStore([
            {
                "snapshot_ts": f"{sample[0]['date']}T15:30:00-05:00",
                "expiry": sample[4]["date"],
                "option_type": "put",
                "strike": "97",
                "bid": "1.20",
                "ask": "1.40",
                "mid": "1.30",
            },
        ])

        rejected = backtest.simulate_trade(
            sample,
            0,
            "PCS",
            band,
            state,
            cfg,
            capital=100000,
            commission_per_option_contract=0.0,
            option_snapshots=store,
            explain_rejections=True,
        )

        self.assertTrue(rejected["_rejected"])
        self.assertEqual(rejected["reason"], "missing_long_put")
        self.assertFalse(rejected["snapshot_fill_available"])

    def test_hvn_distance_metrics_map_mass_nodes_to_trade_price(self):
        state = {
            "center": {
                "current_price": 200.0,
                "profile_nodes": {
                    "support": [{"price": 190.0, "mass_share": 0.20, "strength": 0.05}],
                    "resistance": [{"price": 210.0, "mass_share": 0.15, "strength": 0.04}],
                },
            }
        }

        metrics = backtest.strike_hvn_metrics(state, trade_spot=100.0, strike=95.0, horizon_days=4, annual_vol=0.30)

        self.assertAlmostEqual(metrics["nearest_hvn_price"], 95.0)
        self.assertEqual(metrics["nearest_hvn_kind"], "support")
        self.assertAlmostEqual(metrics["nearest_hvn_distance_em"], 0.0)
        self.assertGreater(metrics["hvn_penalty"], 0.0)

    def test_short_delta_filter_blocks_too_close_short_strike(self):
        sample = rows(20)
        state = {
            "center": {"regime": "active_center", "quality_score": 80, "distance_z": 0.0, "center_price": 100},
            "volatility": {"annual_vol_used": 0.45},
            "volatility_build_up": {"score": 20},
        }
        band = {"lower_boundary": 99.0, "upper_boundary": 101.0}
        cfg = backtest.config_with(
            side_policy="ccs_only",
            min_credit=0.0,
            min_credit_risk=0.0,
            min_otm_pct=0.0,
            max_short_delta=0.05,
            width=5.0,
            hold_bars=4,
            risk_per_trade_pct=0.01,
            max_contracts=3,
        )

        trade = backtest.simulate_trade(sample, 0, "CCS", band, state, cfg, capital=100000)

        self.assertIsNone(trade)

    def test_breach_stop_exits_when_short_strike_touched(self):
        sample = rows(20)
        sample[1]["high"] = 106.0
        state = {
            "center": {"regime": "active_center", "quality_score": 80, "distance_z": 0.0, "center_price": 100},
            "volatility": {"annual_vol_used": 0.45},
            "volatility_build_up": {"score": 20},
        }
        band = {"lower_boundary": 98.0, "upper_boundary": 103.0}
        cfg = backtest.config_with(
            side_policy="ccs_only",
            min_credit=0.0,
            min_credit_risk=0.0,
            max_short_delta=1.0,
            width=5.0,
            hold_bars=4,
            breach_stop=True,
            risk_per_trade_pct=0.01,
            max_contracts=3,
        )

        trade = backtest.simulate_trade(
            sample,
            0,
            "CCS",
            band,
            state,
            cfg,
            capital=100000,
            commission_per_option_contract=0.0,
        )

        self.assertIsNotNone(trade)
        self.assertEqual(trade["exit_reason"], "short_strike_breach_stop")
        self.assertTrue(trade["touched_short"])

    def test_iron_condor_is_combined_defined_risk_structure(self):
        sample = rows(20)
        state = {
            "center": {"regime": "active_center", "quality_score": 85, "distance_z": 0.0, "center_price": 100},
            "volatility": {"annual_vol_used": 0.35},
            "volatility_build_up": {"score": 15},
        }
        band = {"lower_boundary": 96.0, "upper_boundary": 104.0}
        cfg = backtest.config_with(
            side_policy="iron_condor",
            min_credit=0.0,
            min_credit_risk=0.0,
            max_short_delta=1.0,
            width=5.0,
            hold_bars=4,
            risk_per_trade_pct=0.01,
            max_contracts=2,
        )

        trade = backtest.simulate_iron_condor_trade(
            sample,
            0,
            band,
            state,
            cfg,
            capital=100000,
            commission_per_option_contract=0.0,
        )

        self.assertIsNotNone(trade)
        self.assertEqual(trade["side"], "IC")
        self.assertLess(trade["max_loss_dollars"], trade["width"] * 100 * trade["contracts"])
        self.assertLess(trade["put_short_strike"], trade["call_short_strike"])
        self.assertIn("put_short_delta", trade)
        self.assertIn("call_short_delta", trade)
        self.assertIn("ic_body_pct", trade)
        self.assertIn("ic_credit_balance", trade)
        self.assertIn("gravity_score", trade)
        self.assertIn("levitation_score", trade)

    def test_iron_condor_rejects_too_narrow_body(self):
        sample = rows(20)
        state = {
            "center": {"regime": "active_center", "quality_score": 85, "distance_z": 0.0, "center_price": 100},
            "volatility": {"annual_vol_used": 0.35},
            "volatility_build_up": {"score": 15},
        }
        band = {"lower_boundary": 99.0, "upper_boundary": 101.0}
        cfg = backtest.config_with(
            side_policy="iron_condor",
            min_credit=0.0,
            min_credit_risk=0.0,
            max_short_delta=1.0,
            min_ic_body_pct=0.05,
            width=5.0,
            hold_bars=4,
            risk_per_trade_pct=0.01,
            max_contracts=2,
        )

        trade = backtest.simulate_iron_condor_trade(
            sample,
            0,
            band,
            state,
            cfg,
            capital=100000,
            commission_per_option_contract=0.0,
        )

        self.assertIsNone(trade)

    def test_iron_condor_rejects_lopsided_wing_credit(self):
        sample = rows(20)
        state = {
            "center": {"regime": "active_center", "quality_score": 85, "distance_z": 0.0, "center_price": 100},
            "volatility": {"annual_vol_used": 0.35},
            "volatility_build_up": {"score": 15},
        }
        band = {"lower_boundary": 80.0, "upper_boundary": 101.0}
        cfg = backtest.config_with(
            side_policy="iron_condor",
            min_credit=0.0,
            min_credit_risk=0.0,
            max_short_delta=1.0,
            min_ic_credit_balance=0.20,
            width=5.0,
            hold_bars=4,
            risk_per_trade_pct=0.01,
            max_contracts=2,
        )

        trade = backtest.simulate_iron_condor_trade(
            sample,
            0,
            band,
            state,
            cfg,
            capital=100000,
            commission_per_option_contract=0.0,
        )

        self.assertIsNone(trade)

    def test_iron_condor_rejects_tiny_individual_wing_credit(self):
        sample = rows(20)
        state = {
            "center": {"regime": "active_center", "quality_score": 85, "distance_z": 0.0, "center_price": 100},
            "volatility": {"annual_vol_used": 0.35},
            "volatility_build_up": {"score": 15},
        }
        band = {"lower_boundary": 80.0, "upper_boundary": 101.0}
        cfg = backtest.config_with(
            side_policy="iron_condor",
            min_credit=0.0,
            min_credit_risk=0.0,
            max_short_delta=1.0,
            min_ic_wing_credit=0.03,
            width=5.0,
            hold_bars=4,
            risk_per_trade_pct=0.01,
            max_contracts=2,
        )

        trade = backtest.simulate_iron_condor_trade(
            sample,
            0,
            band,
            state,
            cfg,
            capital=100000,
            commission_per_option_contract=0.0,
        )

        self.assertIsNone(trade)

    def test_align_trade_and_mass_rows_uses_intersection_dates(self):
        trade = rows(5)
        mass = rows(5, start_price=200)
        mass = mass[1:]

        aligned_trade, aligned_mass = backtest.align_trade_and_mass_rows(trade, mass)

        self.assertEqual(len(aligned_trade), 4)
        self.assertEqual(aligned_trade[0]["date"], aligned_mass[0]["date"])
        self.assertEqual(aligned_mass[0]["close"], mass[0]["close"])

    def test_iron_condor_side_candidate_returns_combined_ic(self):
        center = {"regime": "active_center", "quality_score": 85, "distance_z": 0.2}
        build = {"score": 20}

        self.assertEqual(backtest.side_candidates("iron_condor", "mixed", center, build), ["IC"])

    def test_ic_weather_block_parser(self):
        cfg = backtest.config_with(blocked_ic_weather="break, overheat")

        self.assertTrue(backtest.is_weather_blocked_for_ic("break", cfg))
        self.assertTrue(backtest.is_weather_blocked_for_ic("overheat", cfg))
        self.assertFalse(backtest.is_weather_blocked_for_ic("mixed", cfg))

    def test_gravity_profile_sets_backtest_lookback_and_half_life(self):
        args = backtest.parse_args([
            "--gravity-profile", "swing",
            "--boundary-model", "ou_hybrid",
            "--credit-haircut-pct", "20",
            "--signal-lag-bars", "1",
            "--min-short-hvn-distance-em", "0.75",
            "--use-option-snapshots", "data/option_chain_snapshots/QQQ",
            "--entry-fill-model", "conservative_mid",
            "--snapshot-max-age-days", "1",
            "--max-snapshot-age-minutes", "30",
            "--require-snapshot-fills",
            "--min-snapshot-fill-coverage", "80",
            "--entry-timestamp-policy", "same_day_close",
        ])

        self.assertEqual(args.lookback, 84)
        self.assertEqual(args.half_life, 21)
        self.assertEqual(args.boundary_model, "ou_hybrid")
        self.assertEqual(args.credit_haircut_pct, 20)
        self.assertEqual(args.signal_lag_bars, 1)
        self.assertEqual(args.min_short_hvn_distance_em, 0.75)
        self.assertEqual(args.use_option_snapshots, "data/option_chain_snapshots/QQQ")
        self.assertEqual(args.entry_fill_model, "conservative_mid")
        self.assertEqual(args.snapshot_max_age_days, 1)
        self.assertEqual(args.snapshot_max_age_minutes, 30)
        self.assertTrue(args.require_snapshot_fills)
        self.assertEqual(args.min_snapshot_fill_coverage, 80)
        self.assertEqual(args.entry_timestamp_policy, "same_day_close")

    def test_synthetic_weekly_backtest_produces_summary(self):
        sample = rows(220)
        result = backtest.run_backtest_on_rows(
            sample,
            vol_series={},
            fallback_vol_series={},
            config={
                "side_policy": "pcs_only",
                "confidence": 0.68,
                "quality_min": 40,
                "max_build_up": 100,
                "max_abs_distance_z": 99,
                "min_credit": 0.0,
                "min_credit_risk": 0.0,
                "max_short_delta": 1.0,
                "width": 5,
                "hold_bars": 4,
                "entry_weekday": 0,
                "max_contracts": 2,
            },
            capital=100000,
            lookback=126,
            half_life=42,
            commission_per_option_contract=0.0,
        )

        self.assertIn("summary", result)
        self.assertGreater(result["summary"]["trades"], 0)
        self.assertIn("pcs", result["summary"])
        self.assertIn("ccs", result["summary"])
        self.assertIn("avg_gravity_score", result["summary"])
        self.assertIn("avg_levitation_score", result["summary"])
        self.assertIn("loss_analysis", result)

    def test_backtest_outputs_snapshot_rejections_and_coverage(self):
        sample = rows(220)
        result = backtest.run_backtest_on_rows(
            sample,
            vol_series={},
            fallback_vol_series={},
            config={
                "side_policy": "pcs_only",
                "entry_fill_model": "conservative_mid",
                "confidence": 0.68,
                "quality_min": 40,
                "max_build_up": 100,
                "max_abs_distance_z": 99,
                "min_credit": 0.0,
                "min_credit_risk": 0.0,
                "max_short_delta": 1.0,
                "width": 5,
                "hold_bars": 4,
                "entry_weekday": 0,
                "max_contracts": 2,
                "min_snapshot_fill_coverage": 100,
            },
            capital=100000,
            lookback=126,
            half_life=42,
            commission_per_option_contract=0.0,
            option_snapshots=backtest.OptionSnapshotStore([]),
        )

        self.assertEqual(result["summary"]["trades"], 0)
        self.assertGreater(len(result["snapshot_rejections"]), 0)
        self.assertEqual(result["summary"]["snapshot_fill_coverage_pct"], 0.0)
        self.assertFalse(result["summary"]["snapshot_coverage_gate_pass"])

    def test_gravity_filter_can_block_entries(self):
        sample = rows(220)
        result = backtest.run_backtest_on_rows(
            sample,
            vol_series={},
            fallback_vol_series={},
            config={
                "side_policy": "pcs_only",
                "confidence": 0.68,
                "quality_min": 0,
                "max_build_up": 100,
                "max_abs_distance_z": 99,
                "min_gravity_score": 101,
                "max_levitation_score": 100,
                "min_credit": 0.0,
                "min_credit_risk": 0.0,
                "max_short_delta": 1.0,
                "width": 5,
                "hold_bars": 4,
                "entry_weekday": 0,
                "max_contracts": 2,
            },
            capital=100000,
            lookback=126,
            half_life=42,
            commission_per_option_contract=0.0,
        )

        self.assertEqual(result["summary"]["trades"], 0)
        self.assertTrue(any(s["reason"] == "gravity_below_min" for s in result["skipped"]))

    def test_loss_analysis_classifies_losing_trade(self):
        analysis = backtest.loss_analysis([{
            "win": False,
            "entry_date": "2024-01-01",
            "expiry_date": "2024-01-05",
            "side": "IC",
            "touched_short": True,
            "touched_side": "call",
            "exit_reason": "stop_2.2x_credit",
            "pnl_dollars": -100.0,
            "credit_risk": 0.10,
            "gravity_score": 45.0,
            "levitation_score": 70.0,
            "build_up_score": 50.0,
            "put_short_hvn_distance_em": 0.50,
            "entry_close": 100.0,
            "expiry_close": 104.0,
        }])

        causes = {row["cause"] for row in analysis["buckets"]}
        self.assertIn("short_strike_touched", causes)
        self.assertIn("hvn_magnet_risk", causes)
        self.assertIn("high_levitation", causes)

    def test_hvn_outcome_analysis_separates_winners_and_losers(self):
        rows_ = [
            {"win": True, "touched_short": False, "short_hvn_distance_em": 1.2},
            {"win": True, "touched_short": True, "short_hvn_distance_em": 0.4},
            {"win": False, "touched_short": True, "short_hvn_distance_em": 0.1},
        ]

        analysis = backtest.hvn_outcome_analysis(rows_)
        by_group = {row["group"]: row for row in analysis}

        self.assertEqual(by_group["winners"]["trades"], 2)
        self.assertEqual(by_group["losers"]["near_hvn_lt_0p20_rate_pct"], 100.0)
        self.assertEqual(by_group["touched_losers"]["magnet_hvn_lt_0p75_rate_pct"], 100.0)

    def test_sweep_configs_expands_ic_body_and_balance_filters(self):
        configs = backtest.sweep_configs({
            "grid_confidences": "0.80",
            "grid_widths": "3",
            "grid_min_qualities": "75",
            "grid_max_builds": "72",
            "grid_credit_to_risks": "0.08",
            "grid_min_gravity_scores": "50,60",
            "grid_max_levitation_scores": "55,65",
            "grid_min_ic_body_pcts": "0,0.04",
            "grid_min_ic_wing_credits": "0,0.03",
            "grid_min_ic_credit_balances": "0,0.05",
        })
        ic_configs = [c for c in configs if c["side_policy"] == "iron_condor"]
        ccs_configs = [c for c in configs if c["side_policy"] == "ccs_only"]

        self.assertEqual(len(ic_configs), 32)
        self.assertEqual(len(ccs_configs), 4)
        self.assertTrue(any(
            c["min_ic_body_pct"] == 0.04
            and c["min_ic_wing_credit"] == 0.03
            and c["min_ic_credit_balance"] == 0.05
            and c["min_gravity_score"] == 60
            and c["max_levitation_score"] == 65
            for c in ic_configs
        ))


if __name__ == "__main__":
    unittest.main()
