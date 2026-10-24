import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "simatic_s7_mcp_server.py"
SPEC = importlib.util.spec_from_file_location("simatic_s7_mcp_server_under_test", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


SAMPLE_NETWORKS = [
    {
        "title": "Motor start/stop seal-in",
        "comment": "Rung 1: seal-in contactor",
        "instructions": ["A I 0.0", "O M 0.0", "AN I 0.1", "= M 0.0"],
    },
    {
        "title": "Drive main contactor",
        "instructions": ["A M 0.0", "= Q 0.0"],
    },
]


def awl_for(networks, block_type="OB", block_number=1):
    report = MODULE.validate_and_analyze_program(networks)
    if not report.valid:
        raise AssertionError(report.errors)
    return MODULE.generate_s7_awl_source(
        report.normalized_networks,
        block_type=block_type,
        block_number=block_number,
    )


class MockS7Controller:
    def __init__(
        self,
        original_awl,
        compile_failure_once=False,
        readback_mismatch=False,
        import_failure=False,
        rollback_failure=False,
        backup_failure=False,
    ):
        self.current_awl = original_awl
        self.compile_failure_once = compile_failure_once
        self.readback_mismatch = readback_mismatch
        self.import_failure = import_failure
        self.rollback_failure = rollback_failure
        self.backup_failure = backup_failure
        self.import_count = 0
        self.compile_count = 0
        self.import_started = False

    def connect(self):
        return True, "SIMATIC Manager - [TestProject -- C:\\Siemens\\Step7\\s7proj\\TestProj]"

    def is_block_editor_active(self, expected_block="OB1"):
        return True, f"LAD/FBD/STL - [{expected_block} -- TestProj]"

    def export_current_awl(self, path, block_name="OB1"):
        if self.backup_failure and "before" in path.name:
            return False, "Simulated backup export failure"
        content = self.current_awl
        if self.readback_mismatch and "verify" in path.name:
            # Different instructions to cause mismatch
            content = awl_for([{"title": "Mismatch", "instructions": ["A I 0.0", "= Q 1.0"]}])
        MODULE.save_s7_artifact(content, path)
        return True, str(path)

    def import_awl_into_active_editor(self, path, block_name="OB1"):
        if self.import_failure and self.import_count == 0:
            self.import_count += 1
            self.import_started = True
            return False, "Simulated import failure"
        if self.rollback_failure and self.import_count > 0:
            self.import_count += 1
            self.import_started = True
            return False, "Simulated rollback import failure"
        self.current_awl = path.read_text(encoding="utf-8")
        self.import_count += 1
        self.import_started = True
        return True, "Candidate AWL accepted by SIMATIC Manager"

    def compile_check_block(self, block_name="OB1"):
        self.compile_count += 1
        if self.compile_failure_once and self.compile_count == 1:
            return False, ["Compile error: Block contains syntax errors"]
        return True, ["Save and compile completed with 0 errors."]


class ValidatorTests(unittest.TestCase):
    def test_supported_s7_plc_families(self):
        for plc in ("S7-300", "S7-400", "WinAC", "CPU315", "CPU416"):
            with self.subTest(plc=plc):
                report = MODULE.validate_and_analyze_program(SAMPLE_NETWORKS, plc_type=plc)
                self.assertTrue(report.valid, report.errors)

    def test_unknown_plc_type_rejected(self):
        report = MODULE.validate_and_analyze_program(SAMPLE_NETWORKS, plc_type="FX3U")
        self.assertFalse(report.valid)
        self.assertIn("Unsupported plc_type", report.errors[0])

    def test_representative_ladder_passes(self):
        report = MODULE.validate_and_analyze_program(SAMPLE_NETWORKS)
        self.assertTrue(report.valid, report.errors)
        self.assertEqual(len(report.normalized_networks), 2)
        self.assertGreater(report.estimated_mc7_bytes, 0)
        self.assertIn("I 0.0", report.devices_used)
        self.assertIn("Q 0.0", report.devices_used)

    def test_bit_address_boundary_enforces_octal_bit_index(self):
        # 0.0 to 0.7 valid
        self.assertTrue(MODULE.validate_and_analyze_program([{"instructions": ["A I 0.7", "= Q 0.7"]}]).valid)
        # .8 is invalid in S7
        report_bad_bit = MODULE.validate_and_analyze_program([{"instructions": ["A I 0.8", "= Q 0.0"]}])
        self.assertFalse(report_bad_bit.valid)

    def test_target_family_address_boundaries(self):
        # S7-300 max input byte 1023
        self.assertTrue(MODULE.validate_and_analyze_program([{"instructions": ["A I 1023.7", "= Q 1023.7"]}], plc_type="S7-300").valid)
        self.assertFalse(MODULE.validate_and_analyze_program([{"instructions": ["A I 1024.0", "= Q 0.0"]}], plc_type="S7-300").valid)
        # S7-400 allows up to 16383
        self.assertTrue(MODULE.validate_and_analyze_program([{"instructions": ["A I 1024.0", "= Q 0.0"]}], plc_type="S7-400").valid)
        self.assertFalse(MODULE.validate_and_analyze_program([{"instructions": ["A I 16384.0", "= Q 0.0"]}], plc_type="S7-400").valid)

    def test_german_mnemonic_support(self):
        german_net = [
            {"title": "German Net", "instructions": ["U E 0.0", "UN E 0.1", "= A 0.0"]}
        ]
        report = MODULE.validate_and_analyze_program(german_net)
        self.assertTrue(report.valid, report.errors)

    def test_timer_counter_and_constants_validation(self):
        timer_net = [
            {
                "title": "On-delay timer",
                "instructions": ["A I 0.0", "L S5T#5S", "SD T 1", "A T 1", "= Q 0.0"],
            },
            {
                "title": "Up-counter",
                "instructions": ["A I 0.2", "CU C 1", "A I 0.3", "R C 1"],
            },
            {
                "title": "Comparison & math",
                "instructions": ["L MW 10", "L 100", ">I", "= M 0.1"],
            },
        ]
        report = MODULE.validate_and_analyze_program(timer_net)
        self.assertTrue(report.valid, report.errors)

    def test_rejects_foreign_plc_addressing_with_clear_hint(self):
        report = MODULE.validate_and_analyze_program([{"instructions": ["A X0", "= Y0"]}])
        self.assertFalse(report.valid)
        self.assertTrue(any("Mitsubishi/Japanese style" in err for err in report.errors))

    def test_foreign_mnemonic_hints_and_autofix(self):
        foreign = [{"instructions": ["LD I 0.0", "OUT Q 0.0"]}]
        # Without auto_fix: rejected with suggestion
        rep_no_fix = MODULE.validate_and_analyze_program(foreign, auto_fix_mnemonics=False)
        self.assertFalse(rep_no_fix.valid)
        self.assertTrue(any("unsupported mnemonic 'LD'; use Siemens 'A'" in err for err in rep_no_fix.errors))

        # With auto_fix: converted
        rep_fixed = MODULE.validate_and_analyze_program(foreign, auto_fix_mnemonics=True)
        self.assertTrue(rep_fixed.valid, rep_fixed.errors)
        self.assertEqual(rep_fixed.normalized_networks[0].instructions, ["A I 0.0", "= Q 0.0"])

    def test_duplicate_output_coil_warning(self):
        networks = [
            {"title": "Net 1", "instructions": ["A I 0.0", "= Q 0.0"]},
            {"title": "Net 2", "instructions": ["A I 0.1", "= Q 0.0"]},
        ]
        report = MODULE.validate_and_analyze_program(networks)
        self.assertTrue(report.valid)  # Valid AWL syntax, but triggers safety warning
        self.assertTrue(any("Duplicate assignment coil 'Q 0.0'" in w for w in report.warnings))

    def test_star_delta_switching_hazard_warning(self):
        networks = [
            {"title": "Motor STAR contactor", "instructions": ["A M 0.0", "= Q 0.1"]},
            {"title": "Motor DELTA contactor", "instructions": ["A M 0.1", "= Q 0.2"]},
        ]
        report = MODULE.validate_and_analyze_program(networks)
        self.assertTrue(report.valid)
        self.assertTrue(any("Star-Delta switching hazard detected" in w for w in report.warnings))

        # With mutual interlock: warning resolved
        interlocked = [
            {"title": "Motor STAR contactor", "instructions": ["A M 0.0", "AN Q 0.2", "= Q 0.1"]},
            {"title": "Motor DELTA contactor", "instructions": ["A M 0.1", "AN Q 0.1", "= Q 0.2"]},
        ]
        report_ok = MODULE.validate_and_analyze_program(interlocked)
        self.assertFalse(any("Star-Delta switching hazard detected" in w for w in report_ok.warnings))

    def test_symbol_table_validation(self):
        valid_symbols = {
            "START_PB": "I 0.0",
            "STOP_PB": "I 0.1",
            "MAIN_CONTACTOR": "Q 0.0",
        }
        report = MODULE.validate_and_analyze_program(SAMPLE_NETWORKS, symbols_input=valid_symbols)
        self.assertTrue(report.valid, report.errors)

        # Invalid symbols (too long, duplicate name, bad address)
        bad_symbols = {
            "START_PB_EXCEEDING_TWENTY_FOUR_CHARS": "I 0.0",
            "BAD_ADDR": "INVALID_ADDR",
        }
        report_bad = MODULE.validate_and_analyze_program(SAMPLE_NETWORKS, symbols_input=bad_symbols)
        self.assertFalse(report_bad.valid)


class AwlTests(unittest.TestCase):
    def test_awl_generation_and_parsing_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            awl_content = awl_for(SAMPLE_NETWORKS, block_type="OB", block_number=1)
            file_path = MODULE.save_s7_artifact(awl_content, Path(temp_dir) / "OB1.awl")

            parsed = MODULE.parse_s7_awl_source(file_path)
            self.assertNotIn("error", parsed)
            self.assertEqual(parsed["block_type"], "ORGANIZATION_BLOCK")
            self.assertEqual(parsed["block_number"], "1")
            self.assertEqual(parsed["network_count"], 2)

            # Compare signatures
            cand_report = MODULE.validate_and_analyze_program(SAMPLE_NETWORKS)
            expected_insts = MODULE.s7_instruction_signature([
                {"instructions": net.instructions} for net in cand_report.normalized_networks
            ])
            actual_insts = MODULE.s7_instruction_signature(parsed["networks"])
            self.assertEqual(actual_insts, expected_insts)

            expected_titles = MODULE.s7_network_title_signature([
                {"title": net.title} for net in cand_report.normalized_networks
            ])
            actual_titles = MODULE.s7_network_title_signature(parsed["networks"])
            self.assertEqual(actual_titles, expected_titles)

    def test_never_overwrites_artifact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "block.awl"
            MODULE.save_s7_artifact("first", path)
            with self.assertRaises(FileExistsError):
                MODULE.save_s7_artifact("second", path)
            self.assertEqual(path.read_text(encoding="utf-8"), "first")

    def test_parse_rejects_missing_markers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bad_path = Path(temp_dir) / "bad.awl"
            bad_path.write_text("ORGANIZATION_BLOCK OB 1\nTITLE = test\n", encoding="utf-8")
            parsed = MODULE.parse_s7_awl_source(bad_path)
            self.assertIn("error", parsed)


class SymbolTableTests(unittest.TestCase):
    def test_symbol_table_generation(self):
        symbols = [
            {"symbol": "START", "address": "I 0.0", "data_type": "BOOL", "comment": "Start PB"},
            {"symbol": "MOTOR", "address": "Q 0.0", "data_type": "BOOL", "comment": "Contactor"},
        ]
        sdf = MODULE.generate_s7_symbol_sdf([
            MODULE.S7SymbolEntry(**item) for item in symbols
        ])
        self.assertIn("Symbol\tAddress\tData type\tComment", sdf)
        self.assertIn("START\tI 0.0\tBOOL\tStart PB", sdf)

        csv_str = MODULE.generate_s7_symbol_csv([
            MODULE.S7SymbolEntry(**item) for item in symbols
        ])
        self.assertIn('"START","I 0.0","BOOL","Start PB"', csv_str)


class TransactionTests(unittest.TestCase):
    def run_write(self, mock_controller, networks=SAMPLE_NETWORKS):
        old_controller = MODULE.controller
        old_workspace = MODULE.WORKSPACE_DIR
        with tempfile.TemporaryDirectory() as temp_dir:
            MODULE.controller = mock_controller
            MODULE.WORKSPACE_DIR = Path(temp_dir)
            MODULE.GUI_ACTIVE_WRITE.clear()
            try:
                return json.loads(MODULE.s7_write_program(networks=networks))
            finally:
                MODULE.controller = old_controller
                MODULE.WORKSPACE_DIR = old_workspace
                MODULE.GUI_ACTIVE_WRITE.clear()

    def test_verified_transaction_returns_success(self):
        original = awl_for(SAMPLE_NETWORKS)
        mock = MockS7Controller(original)
        result = self.run_write(mock)
        self.assertEqual(result["status"], "SUCCESS", result)
        self.assertEqual(result["network_count"], 2)

    def test_compile_failure_rolls_back_and_verifies_backup(self):
        original = awl_for(SAMPLE_NETWORKS)
        mock = MockS7Controller(original, compile_failure_once=True)
        result = self.run_write(mock)
        self.assertEqual(result["status"], "BUILD_FAILED", result)
        self.assertTrue(result["rollback"]["restored"], result)
        self.assertEqual(mock.import_count, 2)  # 1 candidate + 1 rollback

    def test_readback_mismatch_triggers_rollback(self):
        original = awl_for(SAMPLE_NETWORKS)
        mock = MockS7Controller(original, readback_mismatch=True)
        result = self.run_write(mock)
        self.assertEqual(result["status"], "READBACK_MISMATCH", result)
        self.assertTrue(result["rollback"]["restored"], result)

    def test_import_failure_attempts_rollback(self):
        original = awl_for(SAMPLE_NETWORKS)
        mock = MockS7Controller(original, import_failure=True)
        result = self.run_write(mock)
        self.assertEqual(result["status"], "WRITE_FAILED", result)
        self.assertTrue(result["rollback"]["restored"], result)

    def test_backup_failure_prevents_import(self):
        original = awl_for(SAMPLE_NETWORKS)
        mock = MockS7Controller(original, backup_failure=True)
        result = self.run_write(mock)
        self.assertEqual(result["status"], "BACKUP_FAILED", result)
        self.assertEqual(mock.import_count, 0)

    def test_invalid_input_never_touches_gui(self):
        class NoTouchController:
            def connect(self):
                raise AssertionError("GUI should never be touched for invalid input!")

        old = MODULE.controller
        MODULE.controller = NoTouchController()
        try:
            bad_input = [{"instructions": ["A I 0.8", "= Q 0.0"]}]  # invalid bit index
            result = json.loads(MODULE.s7_write_program(networks=bad_input))
            self.assertEqual(result["status"], "REJECTED_BEFORE_WRITE")
        finally:
            MODULE.controller = old

    def test_offline_mode_generates_verified_source_artifacts(self):
        class OfflineController:
            def connect(self):
                return False, "SIMATIC Manager is not running"

        old = MODULE.controller
        old_ws = MODULE.WORKSPACE_DIR
        with tempfile.TemporaryDirectory() as temp_dir:
            MODULE.controller = OfflineController()
            MODULE.WORKSPACE_DIR = Path(temp_dir)
            try:
                res = json.loads(MODULE.s7_write_program(networks=SAMPLE_NETWORKS))
                self.assertEqual(res["status"], "OFFLINE_SOURCE_GENERATED")
                self.assertTrue(Path(res["awl_source_path"]).is_file())
            finally:
                MODULE.controller = old
                MODULE.WORKSPACE_DIR = old_ws

    def test_gui_calls_are_serialized(self):
        calls = []
        entered = threading.Event()

        @MODULE.serialized_gui
        def operation(name):
            calls.append((name, "start"))
            if name == "first":
                entered.set()
                time.sleep(0.15)
            calls.append((name, "end"))

        t1 = threading.Thread(target=operation, args=("first",))
        t2 = threading.Thread(target=operation, args=("second",))
        t1.start()
        entered.wait(1)
        t2.start()
        t1.join(2)
        t2.join(2)
        self.assertEqual(
            calls,
            [("first", "start"), ("first", "end"), ("second", "start"), ("second", "end")],
        )


class GuiResultTests(unittest.TestCase):
    def test_unknown_modal_is_not_dismissed_as_confirmation(self):
        class Child:
            def __init__(self, text, kind):
                self.text, self.kind = text, kind
            def window_text(self):
                return self.text
            def friendly_class_name(self):
                return self.kind

        class Dialog:
            handle = 2
            def window_text(self):
                return "Unexpected"
            def class_name(self):
                return "#32770"
            def is_enabled(self):
                return True
            def children(self):
                return [
                    Child("Unexpected message", "Static"),
                    Child("Abort", "Button"),
                ]

        class App:
            def windows(self, visible_only=True):
                return [Dialog()]

        c = MODULE.SimaticManagerController()
        c.app = App()
        c.main_win = type("Main", (), {"handle": 1})()
        with self.assertRaisesRegex(RuntimeError, "Unrecognized modal dialog"):
            c._dismiss_popups()



class AdditionalEdgeCaseTests(unittest.TestCase):
    def test_parentheses_and_nesting_logic(self):
        nested = [
            {
                "title": "Nested AND-OR logic",
                "instructions": [
                    "A(",
                    "A I 0.0",
                    "O I 0.1",
                    ")",
                    "A(",
                    "A I 0.2",
                    "O I 0.3",
                    ")",
                    "= Q 0.0",
                ],
            }
        ]
        report = MODULE.validate_and_analyze_program(nested)
        self.assertTrue(report.valid, report.errors)

    def test_db_and_peripheral_addressing(self):
        db_periph = [
            {
                "title": "DB and Peripheral Access",
                "instructions": [
                    "A DB1.DBX 0.0",
                    "L PIW 256",
                    "T DB1.DBW 2",
                    "L 16#FF00",
                    "T PQW 256",
                    "= DB1.DBX 4.0",
                ],
            }
        ]
        report = MODULE.validate_and_analyze_program(db_periph)
        self.assertTrue(report.valid, report.errors)

    def test_fc_and_fb_awl_generation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fc_awl = awl_for(SAMPLE_NETWORKS, block_type="FC", block_number=10)
            fc_path = MODULE.save_s7_artifact(fc_awl, Path(temp_dir) / "FC10.awl")
            parsed_fc = MODULE.parse_s7_awl_source(fc_path)
            self.assertEqual(parsed_fc["block_type"], "FUNCTION")
            self.assertEqual(parsed_fc["block_number"], "10")

            fb_awl = awl_for(SAMPLE_NETWORKS, block_type="FB", block_number=20)
            fb_path = MODULE.save_s7_artifact(fb_awl, Path(temp_dir) / "FB20.awl")
            parsed_fb = MODULE.parse_s7_awl_source(fb_path)
            self.assertEqual(parsed_fb["block_type"], "FUNCTION_BLOCK")
            self.assertEqual(parsed_fb["block_number"], "20")

    def test_s7_validate_program_tool(self):
        res = json.loads(MODULE.s7_validate_program(networks=SAMPLE_NETWORKS))
        self.assertTrue(res["valid"])
        self.assertEqual(res["network_count"], 2)

    def test_s7_write_symbol_table_tool(self):
        symbols = {"START": "I 0.0", "STOP": "I 0.1", "MOTOR": "Q 0.0"}
        res = json.loads(MODULE.s7_write_symbol_table(symbols=symbols))
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["symbol_count"], 3)
        self.assertTrue(Path(res["symbol_sdf_path"]).is_file())
        self.assertTrue(Path(res["symbol_csv_path"]).is_file())

    def test_s7_get_status_tool(self):
        res = json.loads(MODULE.s7_get_status())
        self.assertIn("simatic_manager_connected", res)
        self.assertIn("supported_plc_families", res)
        self.assertIn("S7-300", res["supported_plc_families"])


if __name__ == "__main__":
    unittest.main()

