import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from backtests import prospective_validation as pv
from backtests.prospective_validation import (
    RESULT_COLUMNS,
    _candidate_rows,
    _coverage_value,
    _required_inputs,
    baseline_snapshot,
    create_registry,
    evaluate_registry,
    greedy_replay,
    verify_registry,
)


class ProspectiveValidationTests(unittest.TestCase):
    def _fixture(self, root: Path, primary=False):
        results = root / "results"; robust = results / "strategy_robustness"; (results / "liq_reversion").mkdir(parents=True); robust.mkdir()
        data = pd.DataFrame({"detect_ts": [1, 10, 20, 30], "entry_ts": [1, 10, 20, 30], "exit_ts": [5, 15, 25, 35], "net_ret_bp": [1., 2., 3., 4.]})
        data.to_csv(results / "liq_reversion/events_btc_long_x.csv", index=False)
        pd.DataFrame({"strategy": ["btc_long_x"], "family": ["liq_reversion"], "symbol": ["BTCUSDT"], "source_file": ["events_btc_long_x.csv"], "horizon_ms": [4], "meta_direction": ["long"], "meta_ret_threshold": [-.02], "meta_oi_threshold": [-.01], "meta_entry_delay_min": [0], "meta_exit_hold_h": [1]}).to_csv(robust / "strategy_inventory.csv", index=False)
        pd.DataFrame({"strategy": ["btc_long_x"], "family": ["liq_reversion"], "symbol": ["BTCUSDT"], "fold_year": [2026], "train_eligible": [True], "train_mean_bp": [1.], "train_bh_q": [.05], "train_by_q": [.05 if primary else .5], "primary_selection": [primary], "test_mean_bp": [999.]}).to_csv(robust / "walk_forward_folds.csv", index=False)
        (robust / "analysis_summary.json").write_text("{}")
        regimes = results / "market_regimes"; regimes.mkdir(); path = regimes / "daily_regimes.csv"; pd.DataFrame({"date": ["1970-01-01", "1970-01-01"], "symbol": ["BTCUSDT", "ETHUSDT"], "regime": ["uptrend_low_vol"] * 2}).to_csv(path, index=False)
        return results, robust, path

    def test_train_only_candidates_and_primary_zero(self):
        d = pd.DataFrame({"strategy":["a","b"],"family":["f","f"],"symbol":["X","X"],"fold_year":[2026,2026],"train_eligible":[True,True],"train_mean_bp":[1,1],"train_bh_q":[.05,.05],"train_by_q":[.2,.05],"primary_selection":[False,True],"test_mean_bp":[-999,999]})
        rows = _candidate_rows(d)
        self.assertEqual(rows.tier.tolist(), ["primary", "shadow"])

    def test_candidate_selection_strictly_recomputes_primary_rule(self):
        d = pd.DataFrame({"strategy":["a"],"family":["f"],"symbol":["X"],"fold_year":[2026],"train_eligible":["True"],"train_mean_bp":[1],"train_bh_q":[.05],"train_by_q":[.2],"primary_selection":["False"]})
        self.assertEqual(_candidate_rows(d).tier.tolist(), ["shadow"])
        d.loc[0, "train_by_q"] = .05
        with self.assertRaises(ValueError):
            _candidate_rows(d)

    def test_create_rejects_overwrite_and_hash_changes_are_detected(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); results, robust, regimes=self._fixture(root); path=root/"registry.json"
            create_registry(path, results, regimes, robust, "1970-02-01", pd.Timestamp("1970-01-02", tz="UTC")); self.assertTrue(verify_registry(path))
            anchor = verify_registry(path)["external_anchor"]
            self.assertEqual(anchor["kind"], "git_annotated_tag")
            self.assertEqual(anchor["tag"], "prospective-validation-1970-02-01-v1")
            with self.assertRaises(FileExistsError): create_registry(path, results, regimes, robust, "1970-02-01", pd.Timestamp("1970-01-02", tz="UTC"))
            obj=json.loads(path.read_text()); obj["rules"]["window"]="bad"; path.write_text(json.dumps(obj))
            with self.assertRaises(ValueError): verify_registry(path)

    def test_create_rejects_non_month_boundary(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); results, robust, regimes=self._fixture(root)
            with self.assertRaises(ValueError):
                create_registry(root/"registry.json", results, regimes, robust, "1970-02-02", pd.Timestamp("1970-01-02", tz="UTC"))

    def test_external_anchor_strips_git_output_and_matches_all_declared_files(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t).resolve()
            for relative in pv.ANCHOR_REQUIRED_PATHS:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((relative + "\n").encode())
            registry_path = root / pv.ANCHOR_REQUIRED_PATHS[0]
            tag = "prospective-validation-1970-02-01-v1"
            tag_ref = f"refs/tags/{tag}"
            commit, tag_object = "c" * 40, "a" * 40
            anchor = {
                "kind": "git_annotated_tag",
                "repository": pv.DEFAULT_ANCHOR_REPOSITORY,
                "tag": tag,
                "required_paths": list(pv.ANCHOR_REQUIRED_PATHS),
            }

            def fake_git(args, cwd, *, text=True):
                if args == ["rev-parse", "--show-toplevel"]:
                    return f"{root}\n"
                if args == ["remote", "get-url", "origin"]:
                    return pv.DEFAULT_ANCHOR_REPOSITORY + "\n"
                if args == ["cat-file", "-t", tag_ref]:
                    return "tag\n"
                if args == ["rev-parse", f"{tag_ref}^{{commit}}"]:
                    return commit + "\n"
                if args == ["rev-parse", tag_ref]:
                    return tag_object + "\n"
                if args[:3] == ["ls-remote", "--tags", "origin"]:
                    return f"{tag_object}\t{tag_ref}\n{commit}\t{tag_ref}^{{}}\n"
                if args[0] == "show":
                    relative = args[1].split(":", 1)[1]
                    return (root / relative).read_bytes()
                raise AssertionError(args)

            with patch.object(pv, "ROOT", root), \
                 patch.object(pv, "verify_registry", return_value={"external_anchor": anchor}), \
                 patch.object(pv, "_git", side_effect=fake_git):
                verified = pv.verify_external_anchor(registry_path)
            self.assertEqual(verified["commit"], commit)
            self.assertEqual(verified["tag_object"], tag_object)

    def test_coverage_reader_accepts_both_source_manifest_schemas(self):
        liq = {"oi_coverage": {"BTCUSDT": {"coverage_end_exclusive_ts": 123}}}
        crowding = {"sources": {"BTCUSDT": {"funding": {"coverage_end_exclusive_ts": 456}}}}
        self.assertEqual(_coverage_value(liq, "BTCUSDT", "oi"), 123)
        self.assertEqual(_coverage_value(crowding, "BTCUSDT", "funding"), 456)

    def test_candidate_dependent_inputs_are_explicit(self):
        self.assertEqual(_required_inputs({"family":"liq_reversion","liq_params":{"oi_threshold":None}}),{"kline"})
        self.assertEqual(_required_inputs({"family":"liq_reversion","liq_params":{"oi_threshold":"-0x1p-7"}}),{"kline","oi"})
        self.assertEqual(_required_inputs({"family":"crowding_A2"}),{"kline","funding"})
        self.assertEqual(_required_inputs({"family":"crowding_B2"}),{"kline","oi"})
        self.assertEqual(_required_inputs({"family":"crowding_B3"}),{"kline","funding","oi"})

    def test_boundary_and_quarantine_carry(self):
        e=pd.DataFrame({"detect_ts":[5,10,20],"entry_ts":[5,10,20],"exit_ts":[25,15,30],"net_bp":[1.,2.,3.]})
        kept,state=greedy_replay(e[e.detect_ts<10]); self.assertEqual(state["last_exit_ts"],25)
        # Event detected exactly at start is in score, but is rejected by quarantine overlap.
        all_kept,_=greedy_replay(e); self.assertEqual(all_kept.detect_ts.tolist(),[5])
        self.assertEqual(baseline_snapshot(e,10)["raw_count"],1)

    def test_evaluation_withholds_interim_and_missing_manifest_is_normal(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); results, robust, regimes=self._fixture(root); reg=root/"registry.json"; out=root/"out"
            create_registry(reg, results, regimes, robust, "1970-02-01", pd.Timestamp("1970-01-02", tz="UTC"))
            manifest=evaluate_registry(reg,out,results,pd.Timestamp("1970-02-02",tz="UTC"))
            self.assertEqual(manifest["status"],"collecting")
            self.assertEqual(manifest["coverage"],"awaiting_source_refresh")
            df=pd.read_csv(out/"shadow_results.csv"); self.assertTrue(df.mean_bp.isna().all())

    def test_baseline_mutation_fails_closed_and_extra_file_does_not_matter(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); results, robust, regimes=self._fixture(root); reg=root/"registry.json"; out=root/"out"
            create_registry(reg, results, regimes, robust, "1970-02-01", pd.Timestamp("1970-01-02", tz="UTC"))
            (results/"liq_reversion/extra.csv").write_text("ignored\n")
            evaluate_registry(reg,out,results,pd.Timestamp("1970-02-02",tz="UTC"))
            out2=root/"out2"; d=pd.read_csv(results/"liq_reversion/events_btc_long_x.csv"); d.loc[0,"net_ret_bp"]=99; d.to_csv(results/"liq_reversion/events_btc_long_x.csv",index=False)
            with self.assertRaises(ValueError): evaluate_registry(reg,out2,results,pd.Timestamp("1970-02-02",tz="UTC"))

    def test_ready_no_primary_has_fixed_schema_and_shadow_has_no_pass(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); results, robust, regimes=self._fixture(root); reg=root/"registry.json"
            registry=create_registry(reg, results, regimes, robust, "1970-02-01", pd.Timestamp("1970-01-02", tz="UTC"))
            source=results/"liq_reversion/events_btc_long_x.csv"
            original=pd.read_csv(source)
            cutoff=int(registry["baseline_cutoff_exclusive_ts"])
            future=[{"detect_ts":cutoff+1,"entry_ts":cutoff+1,
                     "exit_ts":cutoff+1+4+60_000,"net_ret_bp":99.0}]
            for month in range(2, 8):
                base=int(pd.Timestamp(f"1970-{month:02d}-01", tz="UTC").value//1_000_000)
                for i in range(6):
                    detect=base+i*3_600_000
                    future.append({"detect_ts":detect,"entry_ts":detect,"exit_ts":detect+4,"net_ret_bp":5.0})
            pd.concat([original,pd.DataFrame(future)],ignore_index=True).to_csv(source,index=False)
            end=int(pd.Timestamp(registry["evaluation_end"]).value//1_000_000)
            followup=int(pd.Timestamp(registry["followup_end"]).value//1_000_000)
            manifest={
                "script_sha256": registry["provenance"]["backtests/liq_reversion.py"],
                "kline_coverage":{"BTCUSDT":{"coverage_end_exclusive_ts":followup,"tail_contiguous_start_ts":0}},
                "oi_coverage":{"BTCUSDT":{"coverage_end_exclusive_ts":end,"tail_contiguous_start_ts":0}},
                "event_file_sha256":{source.name:hashlib.sha256(source.read_bytes()).hexdigest()},
            }
            (results/"liq_reversion/run_manifest.json").write_text(json.dumps(manifest),encoding="utf-8")
            out=root/"out"
            report=evaluate_registry(reg,out,results,pd.Timestamp(registry["followup_end"])+pd.Timedelta(days=1))
            self.assertEqual(report["status"],"no_confirmatory_hypotheses")
            self.assertTrue(report["stats_disclosed"])
            self.assertFalse(report["confirmatory_test_performed"])
            primary=pd.read_csv(out/"primary_results.csv")
            self.assertEqual(primary.columns.tolist(),RESULT_COLUMNS)
            self.assertTrue(primary.empty)
            shadow=pd.read_csv(out/"shadow_results.csv")
            self.assertTrue(shadow["by_q"].isna().all())
            self.assertTrue(shadow["pass"].isna().all())
            self.assertTrue((shadow["confirmatory"] == False).all())
            self.assertTrue((shadow["status"] == "nonconfirmatory_complete").all())
            audit=pd.read_csv(out/"event_audit.csv")
            self.assertGreaterEqual(int(audit.loc[0,"quarantine_raw"]),1)


if __name__ == "__main__": unittest.main()
