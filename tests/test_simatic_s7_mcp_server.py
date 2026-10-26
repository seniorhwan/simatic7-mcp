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
        self.assertTrue(
            MODULE.validate_and_analyze_program([{"instructions": ["A I 1023.7", "= Q 1023.7"]}], plc_type="S7-300").valid
        )
        self.assertFalse(
            MODULE.validate_and_analyze_program([{"instructions": ["A I 1024.0", "= Q 0.0"]}], plc_type="S7-300").valid
        )
        # S7-400 allows up to 16383
        self.assertTrue(
            MODULE.validate_and_analyze_program([{"instructions": ["A I 1024.0", "= Q 0.0"]}], plc_type="S7-400").valid
        )
        self.assertFalse(
            MODULE.validate_and_analyze_program([{"instructions": ["A I 16384.0", "= Q 0.0"]}], plc_type="S7-400").valid
        )

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

        asc_str = MODULE.generate_s7_symbol_asc([
            MODULE.S7SymbolEntry(**item) for item in symbols
        ])
        self.assertTrue(asc_str.startswith("126,START"))
        self.assertIn("126,MOTOR", asc_str)

    def test_symbol_table_asc_parsing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            asc_path = Path(tmp_dir) / "test_symbols.asc"
            asc_content = (
                "126,START_REQ               M 10.0      BOOL      START REQUEST\r\n"
                "126,TIMER_T0                T 0         TIMER     Timer T0 1000ms\r\n"
            )
            asc_path.write_text(asc_content, encoding="latin1")
            parsed = MODULE.parse_s7_symbol_table(asc_path)
            self.assertEqual(parsed.get("symbol_count"), 2)
            self.assertEqual(parsed["symbol_map"]["START_REQ"], "M 10.0")
            self.assertEqual(parsed["symbol_map"]["TIMER_T0"], "T 0")


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

    def test_local_data_addressing(self):
        local_net = [
            {
                "title": "Local temp bit and word access",
                "instructions": ["A L 0.0", "= L 0.7", "L LW 2", "T LD 4"],
            }
        ]
        report = MODULE.validate_and_analyze_program(local_net)
        self.assertTrue(report.valid, report.errors)
        self.assertIn("L 0.0", report.devices_used)
        self.assertIn("L 0.7", report.devices_used)

    def test_block_operands_and_opened_db(self):
        call_net = [
            {
                "title": "Block calls and DB access",
                "instructions": [
                    "OPN DB 1",
                    "A DBX 0.0",
                    "= DBX 0.1",
                    "L DBW 2",
                    "T DBD 4",
                    "CALL FC 1",
                    "CALL FB 1, DB 2",
                    "CC FC 10",
                    "UC FC 20",
                ],
            }
        ]
        report = MODULE.validate_and_analyze_program(call_net)
        self.assertTrue(report.valid, report.errors)

    def test_jump_labels_roundtrip(self):
        jump_net = [
            {
                "title": "Branching with labels",
                "instructions": [
                    "A I 0.0",
                    "JC LAB1",
                    "L 0",
                    "T MW 10",
                    "JU END1",
                    "LAB1: L 100",
                    "T MW 10",
                    "END1: NOP 0",
                ],
            }
        ]
        report = MODULE.validate_and_analyze_program(jump_net)
        self.assertTrue(report.valid, report.errors)
        with tempfile.TemporaryDirectory() as td:
            awl = MODULE.generate_s7_awl_source(report.normalized_networks)
            path = Path(td) / "jump.awl"
            path.write_text(awl, encoding="utf-8")
            parsed = MODULE.parse_s7_awl_source(path)
            self.assertNotIn("error", parsed)
            self.assertEqual(len(parsed["networks"][0]["instructions"]), 8)
            exp_sig = MODULE.s7_instruction_signature([{"instructions": net.instructions} for net in report.normalized_networks])
            act_sig = MODULE.s7_instruction_signature(parsed["networks"])
            self.assertEqual(act_sig, exp_sig)

    def test_status_bits_validation(self):
        status_net = [
            {
                "title": "Status bits check",
                "instructions": [
                    "A ==0",
                    "A >0",
                    "A OS",
                    "A OV",
                    "A BR",
                    "= M 0.0",
                ],
            }
        ]
        report = MODULE.validate_and_analyze_program(status_net)
        self.assertTrue(report.valid, report.errors)

    def test_extended_literals_with_underscores(self):
        lit_net = [
            {
                "title": "Extended literals",
                "instructions": [
                    "L S5T#1M_30S",
                    "L S5TIME#10S",
                    "L T#1D_2H",
                    "L D#2026-10-24",
                    "L TOD#12:30:00",
                    "L B#16#FF",
                    "L 2#1010_0101",
                    "T MD 20",
                ],
            }
        ]
        report = MODULE.validate_and_analyze_program(lit_net)
        self.assertTrue(report.valid, report.errors)

    def test_address_register_and_stack(self):
        ar_net = [
            {
                "title": "Address register instructions",
                "instructions": [
                    "LAR1 P#0.0",
                    "TAR1 MD 10",
                    "CAR",
                    "PUSH",
                    "POP",
                    "STP",
                ],
            }
        ]
        report = MODULE.validate_and_analyze_program(ar_net)
        self.assertTrue(report.valid, report.errors)

    def test_german_jumps_and_timers(self):
        de_net = [
            {
                "title": "German jumps and timers",
                "instructions": [
                    "U E 0.0",
                    "L S5T#2S",
                    "SI T 1",
                    "U T 1",
                    "SPA LAB_DE",
                    "LAB_DE: ZV C 1",
                    "BEA",
                ],
            }
        ]
        report = MODULE.validate_and_analyze_program(de_net)
        self.assertTrue(report.valid, report.errors)

    def test_mnemonic_language_normalization(self):
        foreign_net = [
            {"title": "Foreign mnemonics", "instructions": ["LD I 0.0", "OUT Q 0.0"]}
        ]
        rep_de = MODULE.validate_and_analyze_program(foreign_net, auto_fix_mnemonics=True, mnemonic_language="German")
        self.assertTrue(rep_de.valid, rep_de.errors)
        self.assertEqual(rep_de.normalized_networks[0].instructions, ["U I 0.0", "= Q 0.0"])

        rep_intl = MODULE.validate_and_analyze_program(foreign_net, auto_fix_mnemonics=True, mnemonic_language="International")
        self.assertTrue(rep_intl.valid, rep_intl.errors)
        self.assertEqual(rep_intl.normalized_networks[0].instructions, ["A I 0.0", "= Q 0.0"])

    def test_awl_parser_strips_inline_comments(self):
        with tempfile.TemporaryDirectory() as td:
            awl_text = (
                "ORGANIZATION_BLOCK OB 1\n"
                "TITLE = \"Test\"\n"
                "BEGIN\n"
                "NETWORK\n"
                "TITLE = Net1\n"
                "      A     I 0.0; // Start PB\n"
                "      =     Q 0.0; // Contactor\n"
                "END_ORGANIZATION_BLOCK\n"
            )
            p = Path(td) / "inline.awl"
            p.write_text(awl_text, encoding="utf-8")
            parsed = MODULE.parse_s7_awl_source(p)
            self.assertNotIn("error", parsed)
            insts = parsed["networks"][0]["instructions"]
            self.assertEqual(insts, ["A I 0.0", "= Q 0.0"])

    def test_star_delta_with_symbols_and_german(self):
        german_hazard = [
            {"title": "Motor STERN Schütz", "instructions": ["U M 0.0", "= A 0.1"]},
            {"title": "Motor DREIECK Schütz", "instructions": ["U M 0.1", "= A 0.2"]},
        ]
        rep_hazard = MODULE.validate_and_analyze_program(german_hazard)
        self.assertTrue(rep_hazard.valid)
        self.assertTrue(any("Star-Delta switching hazard detected" in w for w in rep_hazard.warnings))

        symbols = {"STAR_CONTACTOR": "Q 0.1", "DELTA_CONTACTOR": "Q 0.2"}
        interlocked = [
            {"title": "Motor STAR contactor", "instructions": ["A M 0.0", "AN \"DELTA_CONTACTOR\"", "= \"STAR_CONTACTOR\""]},
            {"title": "Motor DELTA contactor", "instructions": ["A M 0.1", "AN \"STAR_CONTACTOR\"", "= \"DELTA_CONTACTOR\""]},
        ]
        rep_ok = MODULE.validate_and_analyze_program(interlocked, symbols_input=symbols)
        self.assertFalse(any("Star-Delta switching hazard detected" in w for w in rep_ok.warnings))

    def test_symbol_table_bidirectional_and_read(self):
        addr_key = {"I 0.0": "START", "Q 0.0": "MOTOR"}
        rep = MODULE.validate_and_analyze_program(SAMPLE_NETWORKS, symbols_input=addr_key)
        self.assertTrue(rep.valid, rep.errors)

        with tempfile.TemporaryDirectory() as td:
            csv_path = Path(td) / "sym.csv"
            csv_content = MODULE.generate_s7_symbol_csv([
                MODULE.S7SymbolEntry("START", "I 0.0", "BOOL", "Start PB"),
                MODULE.S7SymbolEntry("STOP", "I 0.1", "BOOL", "Stop PB"),
            ])
            csv_path.write_text(csv_content, encoding="utf-8")
            read_res = json.loads(MODULE.s7_read_symbol_table(str(csv_path)))
            self.assertTrue(read_res["success"])
            self.assertEqual(read_res["symbol_count"], 2)
            self.assertEqual(read_res["symbol_map"]["START"], "I 0.0")

    def test_s7_list_supported_plc_types_tool(self):
        res = json.loads(MODULE.s7_list_supported_plc_types())
        self.assertIn("families", res)
        families = [f["family"] for f in res["families"]]
        self.assertIn("S7-300", families)
        self.assertIn("S7-400", families)
        self.assertIn("WinAC", families)

    def test_s7_create_project_offline(self):
        old_ws = MODULE.WORKSPACE_DIR
        with tempfile.TemporaryDirectory() as td:
            MODULE.WORKSPACE_DIR = Path(td)
            try:
                res = json.loads(MODULE.s7_create_project(project_name="Line2_Station", plc_type="S7-400"))
                self.assertTrue(res["success"])
                self.assertEqual(res["project_name"], "Line2_Station")
                meta_path = Path(td) / "Projects" / "Line2_Station" / "project.json"
                self.assertTrue(meta_path.is_file())
            finally:
                MODULE.WORKSPACE_DIR = old_ws


class QualityOfLifeAndToolsTests(unittest.TestCase):
    def setUp(self):
        self.old_ws = MODULE.WORKSPACE_DIR
        self.old_controller = MODULE.controller
        self.temp_dir = tempfile.TemporaryDirectory()
        MODULE.WORKSPACE_DIR = Path(self.temp_dir.name)
        MODULE.controller = MODULE.SimaticManagerController()

    def tearDown(self):
        MODULE.WORKSPACE_DIR = self.old_ws
        MODULE.controller = self.old_controller
        self.temp_dir.cleanup()

    def test_device_comments_read_write_roundtrip(self):
        comments = {
            "I 0.0": "Main Start Pushbutton",
            "I 0.1": "Main Stop Pushbutton",
            "Q 0.0": "Main Contactor Coil",
        }
        res_write = json.loads(MODULE.s7_write_device_comments(device_comments=comments))
        self.assertEqual(res_write["status"], "SUCCESS")
        self.assertEqual(res_write["comment_count"], 3)
        self.assertTrue(Path(res_write["symbol_sdf_path"]).is_file())
        self.assertTrue(Path(res_write["symbol_csv_path"]).is_file())

        # Read back from SDF
        res_read_sdf = json.loads(MODULE.s7_read_device_comments(res_write["symbol_sdf_path"]))
        self.assertTrue(res_read_sdf["success"])
        self.assertEqual(res_read_sdf["device_comments"]["I 0.0"], "Main Start Pushbutton")
        self.assertEqual(res_read_sdf["symbol_to_address"]["Main_Start_Pushbutton"], "I 0.0")

        # Read back without path (workspace default)
        res_read_def = json.loads(MODULE.s7_read_device_comments())
        self.assertTrue(res_read_def["success"])
        self.assertEqual(res_read_def["device_comments"]["Q 0.0"], "Main Contactor Coil")

    def test_duplicate_device_comments_disambiguation(self):
        dup_comments = {
            "I 0.1": "Spare Input Sensor",
            "I 0.2": "Spare Input Sensor",
            "I 0.3": "Spare Input Sensor",
        }
        res_write = json.loads(MODULE.s7_write_device_comments(device_comments=dup_comments))
        self.assertEqual(res_write["status"], "SUCCESS")
        self.assertEqual(res_write["comment_count"], 3)
        symbols = [p["symbol"] for p in res_write["processed_comments"]]
        self.assertEqual(len(symbols), len(set(symbols)), "Symbols must be unique despite identical comments")

        # Verify readback works
        res_read = json.loads(MODULE.s7_read_device_comments(res_write["symbol_sdf_path"]))
        self.assertTrue(res_read["success"])
        self.assertEqual(len(res_read["symbols"]), 3)

    def test_search_symbols_and_comments(self):
        comments = {
            "I 0.0": "Start Conveyor",
            "I 0.1": "Stop Conveyor",
            "Q 0.0": "Conveyor Motor 1",
            "Q 0.1": "Water Pump Run",
            "M 10.0": "Conveyor Running State",
        }
        res_write = json.loads(MODULE.s7_write_device_comments(device_comments=comments))
        sdf_path = res_write["symbol_sdf_path"]

        # Search wildcard
        s1 = json.loads(MODULE.s7_search_symbols_and_comments(query="*Conveyor*", comment_file_path=sdf_path))
        self.assertTrue(s1["success"])
        self.assertGreaterEqual(s1["total_matches"], 3)

        # Filter by ADDRESS
        s2 = json.loads(MODULE.s7_search_symbols_and_comments(
            query="Q 0.*", filter_type="ADDRESS", comment_file_path=sdf_path
        ))
        self.assertTrue(s2["success"])
        self.assertEqual(s2["total_matches"], 2)

        # Filter by COMMENT
        s3 = json.loads(MODULE.s7_search_symbols_and_comments(
            query="*Pump*", filter_type="COMMENT", comment_file_path=sdf_path
        ))
        self.assertTrue(s3["success"])
        self.assertEqual(s3["total_matches"], 1)
        self.assertEqual(s3["matches"][0]["symbol"], "Water_Pump_Run")

        # Filter by DATA_TYPE
        s4 = json.loads(MODULE.s7_search_symbols_and_comments(
            query="BOOL", filter_type="DATA_TYPE", comment_file_path=sdf_path
        ))
        self.assertTrue(s4["success"])
        self.assertEqual(s4["total_matches"], 5)

        # Empty query error
        s_err = json.loads(MODULE.s7_search_symbols_and_comments(query="", comment_file_path=sdf_path))
        self.assertFalse(s_err["success"])

    def test_annotate_awl_code_styles(self):
        source_awl = (
            "ORGANIZATION_BLOCK OB 1\n"
            "BEGIN\n"
            "NETWORK\n"
            "TITLE = Motor Control\n"
            "      A     I 0.0;\n"
            "      AN    I 0.1;\n"
            "      =     Q 0.0;\n"
            "END_ORGANIZATION_BLOCK\n"
        )
        symbols = {"START_PB": "I 0.0", "STOP_PB": "I 0.1", "MOTOR_RUN": "Q 0.0"}

        # COMMENTS style
        res_comm = json.loads(MODULE.s7_annotate_awl_code(source_code=source_awl, symbols=symbols, style="COMMENTS"))
        self.assertTrue(res_comm["success"])
        self.assertIn("// \"START_PB\"", res_comm["annotated_awl_source"])
        self.assertIn("// \"MOTOR_RUN\"", res_comm["annotated_awl_source"])

        # Parsable after annotation
        parsed = MODULE.parse_s7_awl_source(Path(res_comm["annotated_awl_path"]))
        self.assertNotIn("error", parsed)
        self.assertEqual(parsed["networks"][0]["instructions"], ["A I 0.0", "AN I 0.1", "= Q 0.0"])

        # SYMBOLIC style
        res_sym = json.loads(MODULE.s7_annotate_awl_code(source_code=source_awl, symbols=symbols, style="SYMBOLIC"))
        self.assertTrue(res_sym["success"])
        self.assertIn("\"START_PB\"", res_sym["annotated_awl_source"])
        self.assertIn("\"MOTOR_RUN\"", res_sym["annotated_awl_source"])

        # ABSOLUTE style
        sym_awl = (
            "ORGANIZATION_BLOCK OB 1\n"
            "BEGIN\n"
            "NETWORK\n"
            "TITLE = Symbolic Control\n"
            "      A     \"START_PB\";\n"
            "      =     \"MOTOR_RUN\";\n"
            "END_ORGANIZATION_BLOCK\n"
        )
        res_abs = json.loads(MODULE.s7_annotate_awl_code(source_code=sym_awl, symbols=symbols, style="ABSOLUTE"))
        self.assertTrue(res_abs["success"])
        self.assertIn("I 0.0", res_abs["annotated_awl_source"])
        self.assertIn("Q 0.0", res_abs["annotated_awl_source"])
        parsed_abs = MODULE.parse_s7_awl_source(Path(res_abs["annotated_awl_path"]))
        self.assertEqual(parsed_abs["networks"][0]["instructions"], ["A I 0.0", "= Q 0.0"])

    def test_project_and_station_creation_and_open(self):
        res_create = json.loads(
            MODULE.s7_create_project(
                project_name="Assembly_Cell_1",
                plc_type="S7-300",
                cpu_type="CPU 315-2 DP",
                station_name="Cell1_Station",
                launch_if_closed=False,
            )
        )
        self.assertTrue(res_create["success"])
        self.assertEqual(res_create["cpu_type"], "CPU 315-2 DP")
        self.assertEqual(res_create["station_name"], "Cell1_Station")
        s7p_file = Path(res_create["s7p_path"])
        self.assertTrue(s7p_file.is_file())

        proj_dir = s7p_file.parent
        self.assertTrue((proj_dir / "station.json").is_file())
        self.assertTrue((proj_dir / "Blocks" / "OB1.awl").is_file())
        self.assertTrue((proj_dir / "Symbols" / "symbols.sdf").is_file())

        # Test open project
        res_open = json.loads(MODULE.s7_open_project(str(s7p_file), launch_if_closed=False))
        self.assertTrue(res_open["success"])
        self.assertEqual(MODULE.controller.project_name, "Assembly_Cell_1")

        # Test open block
        res_block = json.loads(MODULE.s7_open_block("OB1"))
        self.assertTrue(res_block["success"])
        self.assertIn("block_path", res_block["details"])

        # Test open non-existent block
        res_bad_blk = json.loads(MODULE.s7_open_block("FC999"))
        self.assertFalse(res_bad_blk["success"])

        # Test open non-existent project
        res_bad_proj = json.loads(MODULE.s7_open_project("NonExistent_Proj_99"))
        self.assertFalse(res_bad_proj["success"])

    def test_cross_references_and_memory_overlaps(self):
        networks = [
            {
                "title": "Bit and Byte output write",
                "instructions": ["A I 0.0", "= Q 0.0", "= QB 0"],
            },
            {
                "title": "Word and Byte memory access",
                "instructions": ["L MW 10", "T MB 11"],
            },
            {
                "title": "Unpaired Set Marker",
                "instructions": ["A I 0.2", "S M 20.0"],
            },
        ]
        res = json.loads(MODULE.s7_get_cross_references(networks=networks))
        self.assertTrue(res["success"])
        refs = res["cross_references"]
        self.assertIn("I 0.0", refs)
        self.assertEqual(refs["I 0.0"]["read_count"], 1)
        self.assertEqual(refs["I 0.0"]["write_count"], 0)

        self.assertIn("Q 0.0", refs)
        self.assertEqual(refs["Q 0.0"]["write_count"], 1)

        self.assertIn("QB 0", refs)
        self.assertEqual(refs["QB 0"]["write_count"], 1)

        # Verify memory overlaps detected
        overlaps = res["memory_overlaps"]
        self.assertTrue(
            any("Q 0.0" in o and "QB 0" in o for o in overlaps),
            f"Expected Q 0.0/QB 0 overlap in {overlaps}",
        )
        self.assertTrue(
            any("MW 10" in o and "MB 11" in o for o in overlaps),
            f"Expected MW 10/MB 11 overlap in {overlaps}",
        )

    def test_memory_usage_map(self):
        networks = [
            {
                "title": "IO and Markers",
                "instructions": [
                    "A I 0.0",
                    "A I 0.7",
                    "= Q 0.1",
                    "= M 10.5",
                    "L S5T#5S",
                    "SD T 3",
                    "A I 1.0",
                    "CU C 2",
                    "A DB1.DBX 0.0",
                    "= DB1.DBX 0.1",
                ],
            }
        ]
        res = json.loads(MODULE.s7_get_memory_usage_map(networks=networks, plc_type="S7-300"))
        self.assertTrue(res["success"])
        self.assertEqual(res["inputs"]["used_bytes"], 2)
        self.assertEqual(res["outputs"]["used_bytes"], 1)
        self.assertEqual(res["markers"]["used_bytes"], 1)
        self.assertTrue(any("0:X" in row and "7:X" in row for row in res["inputs"]["bit_grid"]))
        self.assertEqual(res["timers"]["used_count"], 1)
        self.assertIn(3, res["timers"]["indices"])
        self.assertEqual(res["counters"]["used_count"], 1)
        self.assertIn(2, res["counters"]["indices"])
        self.assertIn("DB1", res["data_blocks"]["dbs"])

    def test_block_overview(self):
        proj_dir = MODULE.WORKSPACE_DIR / "Projects" / "Overview_Test" / "Blocks"
        proj_dir.mkdir(parents=True, exist_ok=True)
        ob1_code = (
            "ORGANIZATION_BLOCK OB 1\n"
            "BEGIN\n"
            "NETWORK\n"
            "TITLE = Call FC10\n"
            "      CALL  FC 10;\n"
            "END_ORGANIZATION_BLOCK\n"
        )
        fc10_code = (
            "FUNCTION FC 10 : VOID\n"
            "BEGIN\n"
            "NETWORK\n"
            "TITLE = FC Rung\n"
            "      A     I 0.0;\n"
            "      =     Q 0.0;\n"
            "END_FUNCTION\n"
        )
        (proj_dir / "OB1.awl").write_text(ob1_code, encoding="utf-8")
        (proj_dir / "FC10.awl").write_text(fc10_code, encoding="utf-8")

        res = json.loads(MODULE.s7_get_block_overview(project_path_or_name=str(proj_dir.parent)))
        self.assertTrue(res["success"])
        self.assertEqual(res["total_blocks"], 2)
        call_graph = res["call_hierarchy"]
        self.assertIn("OB1", call_graph)
        self.assertIn("FC 10", call_graph["OB1"])

    def test_deep_program_analysis_detects_hazards(self):
        hazard_networks = [
            {
                "title": "Motor STAR contactor",
                "instructions": ["A M 0.0", "= Q 0.1"],
            },
            {
                "title": "Motor DELTA contactor",
                "instructions": ["A M 0.1", "= Q 0.2"],
            },
            {
                "title": "Duplicate Coil net 1",
                "instructions": ["A I 0.0", "= Q 0.5"],
            },
            {
                "title": "Duplicate Coil net 2",
                "instructions": ["A I 0.1", "= Q 0.5"],
            },
            {
                "title": "Unpaired Set",
                "instructions": ["A I 0.2", "S M 30.0"],
            },
            {
                "title": "Timer without evaluation",
                "instructions": ["A I 0.3", "L S5T#1S", "SD T 4"],
            },
            {
                "title": "Direct Peripheral Access",
                "instructions": ["L PIW 256", "T PQW 256"],
            },
            {
                "title": "Excessive nesting depth > 7",
                "instructions": [
                    "A(", "A(", "A(", "A(", "A(", "A(", "A(", "A(",
                    "A I 0.0",
                    ")", ")", ")", ")", ")", ")", ")", ")",
                    "= M 40.0",
                ],
            },
        ]
        res = json.loads(MODULE.s7_analyze_program(networks=hazard_networks, plc_type="S7-300"))
        self.assertTrue(res["success"])
        self.assertEqual(res["status"], "FAILED")
        self.assertLess(res["safety_score"], 70)

        hazards_str = " ".join(res["hazards"])
        warnings_str = " ".join(res["warnings"])
        self.assertIn("CRITICAL INTERLOCK HAZARD", hazards_str)
        self.assertIn("DUPLICATE COIL HAZARD", hazards_str)
        self.assertIn("NESTING STACK OVERFLOW", hazards_str)
        self.assertIn("UNPAIRED SET COIL", warnings_str)
        self.assertIn("TIMER HYGIENE", warnings_str)
        self.assertIn("PERIPHERAL DIRECT ACCESS", warnings_str)
        self.assertGreater(res["metrics"]["estimated_sweep_time_us"], 0)

    def test_deep_program_analysis_clean_program(self):
        clean_networks = [
            {
                "title": "Motor STAR contactor with reciprocal interlock",
                "instructions": ["A M 0.0", "AN Q 0.2", "= Q 0.1"],
            },
            {
                "title": "Motor DELTA contactor with reciprocal interlock",
                "instructions": ["A M 0.1", "AN Q 0.1", "= Q 0.2"],
            },
            {
                "title": "Paired Set",
                "instructions": ["A I 0.2", "S M 30.0"],
            },
            {
                "title": "Paired Reset",
                "instructions": ["A I 0.3", "R M 30.0"],
            },
            {
                "title": "Timer evaluated properly",
                "instructions": ["A I 0.4", "L S5T#2S", "SD T 1", "A T 1", "= Q 0.3"],
            },
        ]
        res = json.loads(MODULE.s7_analyze_program(networks=clean_networks, plc_type="S7-300"))
        self.assertTrue(res["success"])
        self.assertEqual(res["status"], "PASSED")
        self.assertEqual(res["safety_score"], 100)
        self.assertEqual(len(res["hazards"]), 0)

    def test_auto_detect_controllers_tool(self):
        res = json.loads(MODULE.s7_auto_detect_controllers())
        self.assertTrue(res["success"])
        self.assertIn("step7_installed", res)
        self.assertIn("simatic_manager_exe", res)
        self.assertIn("running_siemens_processes", res)
        self.assertIn("workspace_dir", res)
        self.assertIn("plcsim_exe", res)

    def test_device_comments_duplicate_string_disambiguation(self):
        dup_comments = {
            "I 0.0": "Spare",
            "I 0.1": "Spare",
            "I 0.2": "Spare",
        }
        res_write = json.loads(MODULE.s7_write_device_comments(device_comments=dup_comments))
        self.assertEqual(res_write["status"], "SUCCESS")
        self.assertEqual(res_write["comment_count"], 3)
        symbols = [p["symbol"] for p in res_write["processed_comments"]]
        self.assertEqual(len(symbols), len(set(symbols)), "Generated symbols must be unique")
        # Check that comment text is preserved, not emptied
        for p in res_write["processed_comments"]:
            self.assertEqual(p["comment"], "Spare")

        # Read back
        res_read = json.loads(MODULE.s7_read_device_comments(res_write["symbol_sdf_path"]))
        self.assertTrue(res_read["success"])
        self.assertEqual(res_read["device_comments"]["I 0.0"], "Spare")
        self.assertEqual(res_read["device_comments"]["I 0.1"], "Spare")

    def test_device_comments_list_without_symbol_name(self):
        list_comments = [
            {"address": "I 0.0", "comment": "Start PB"},
            {"address": "I 0.1", "comment": "Stop PB"},
            {"address": "Q 0.0", "comment": "Main Motor Contactor"},
        ]
        res_write = json.loads(MODULE.s7_write_device_comments(device_comments=list_comments))
        self.assertEqual(res_write["status"], "SUCCESS")
        self.assertEqual(res_write["comment_count"], 3)
        self.assertEqual(res_write["processed_comments"][0]["symbol"], "Start_PB")
        self.assertEqual(res_write["processed_comments"][0]["data_type"], "BOOL")
        self.assertEqual(res_write["processed_comments"][0]["comment"], "Start PB")

    def test_search_symbols_special_regex_characters(self):
        comments = {
            "I 0.0": "Pump (Main)",
            "I 0.1": "Sensor [High+Low]",
            "DB1.DBX 0.0": "DB Valve Status",
        }
        res_write = json.loads(MODULE.s7_write_device_comments(device_comments=comments))
        sdf_path = res_write["symbol_sdf_path"]

        # Search with parentheses in glob
        s1 = json.loads(MODULE.s7_search_symbols_and_comments(query="Pump (*", comment_file_path=sdf_path))
        self.assertTrue(s1["success"])
        self.assertEqual(s1["total_matches"], 1)

        # Search with brackets and plus
        s2 = json.loads(MODULE.s7_search_symbols_and_comments(query="*High+Low*", comment_file_path=sdf_path))
        self.assertTrue(s2["success"])
        self.assertEqual(s2["total_matches"], 1)

        # Search with DB dot
        s3 = json.loads(MODULE.s7_search_symbols_and_comments(query="DB1.*", comment_file_path=sdf_path))
        self.assertTrue(s3["success"])
        self.assertEqual(s3["total_matches"], 1)

    def test_annotate_awl_snippet_and_normalized_networks(self):
        snippet = "A I 0.0\n= Q 0.0"
        symbols = {"START": "I 0.0", "MOTOR": "Q 0.0"}

        # Annotate bare snippet
        res = json.loads(MODULE.s7_annotate_awl_code(source_code=snippet, symbols=symbols, style="SYMBOLIC"))
        self.assertTrue(res["success"])
        self.assertIn('"START"', res["annotated_awl_source"])
        self.assertIn('"MOTOR"', res["annotated_awl_source"])
        # Check that normalized_networks reflects symbolic style
        instructions = res["normalized_networks"][0]["instructions"]
        self.assertEqual(instructions, ['A "START"', '= "MOTOR"'])

        # Absolute style
        res_abs = json.loads(MODULE.s7_annotate_awl_code(source_code=snippet, symbols=symbols, style="ABSOLUTE"))
        self.assertTrue(res_abs["success"])
        self.assertEqual(res_abs["normalized_networks"][0]["instructions"], ["A I 0.0", "= Q 0.0"])

    def test_parse_awl_network_number_and_data_block(self):
        awl_content = (
            "ORGANIZATION_BLOCK OB 1\n"
            "BEGIN\n"
            "NETWORK 1\n"
            "TITLE = Sweep 1\n"
            "A I 0.0\n"
            "NETWORK 2 : Sweep 2\n"
            "= Q 0.0\n"
            "END_ORGANIZATION_BLOCK\n"
        )
        p = MODULE.WORKSPACE_DIR / "test_net_numbered.awl"
        p.write_text(awl_content, encoding="utf-8")
        try:
            parsed = MODULE.parse_s7_awl_source(p)
            self.assertNotIn("error", parsed)
            self.assertEqual(parsed["network_count"], 2)
            self.assertEqual(parsed["networks"][0]["instructions"], ["A I 0.0"])
            self.assertEqual(parsed["networks"][1]["instructions"], ["= Q 0.0"])
        finally:
            if p.exists():
                p.unlink()

        # Parse Data Block
        db_content = (
            "DATA_BLOCK DB 1\n"
            "TITLE = Plant Data Block\n"
            "BEGIN\n"
            "END_DATA_BLOCK\n"
        )
        pdb = MODULE.WORKSPACE_DIR / "test_db1.awl"
        pdb.write_text(db_content, encoding="utf-8")
        try:
            parsed_db = MODULE.parse_s7_awl_source(pdb)
            self.assertNotIn("error", parsed_db)
            self.assertEqual(parsed_db["block_type"], "DATA_BLOCK")
            self.assertEqual(parsed_db["block_number"], "1")
        finally:
            if pdb.exists():
                pdb.unlink()

    def test_block_overview_by_project_name(self):
        res_create = json.loads(MODULE.s7_create_project(project_name="Overview_Named_Project"))
        self.assertTrue(res_create["success"])
        res_overview = json.loads(MODULE.s7_get_block_overview("Overview_Named_Project"))
        self.assertTrue(res_overview["success"])
        self.assertGreaterEqual(res_overview["total_blocks"], 1)
        self.assertTrue(any("OB1" in b.get("tag", b.get("block_tag", "")) for b in res_overview["blocks"]))

    def test_comprehensive_memory_overlaps_md_and_db(self):
        networks = [
            {
                "title": "DWord and Word Overlap",
                "instructions": ["L 100", "T MD 0", "L MW 2"],
            },
            {
                "title": "DB DWord and Bit Overlap",
                "instructions": ["L 500", "T DB1.DBD 0", "A DB1.DBX 0.0", "= M 0.0"],
            },
            {
                "title": "Odd Word Overlap",
                "instructions": ["L 20", "T MW 10", "L MW 11"],
            },
        ]
        res = json.loads(MODULE.s7_get_cross_references(networks=networks))
        self.assertTrue(res["success"])
        overlaps = res["memory_overlaps"]
        self.assertTrue(
            any("MD 0" in o and "MW 2" in o for o in overlaps),
            f"Expected MD 0 / MW 2 overlap in {overlaps}",
        )
        self.assertTrue(
            any("DB1.DBD 0" in o and "DB1.DBX 0.0" in o for o in overlaps),
            f"Expected DB1.DBD 0 / DB1.DBX 0.0 overlap in {overlaps}",
        )
        self.assertTrue(
            any("MW 10" in o and "MW 11" in o for o in overlaps),
            f"Expected MW 10 / MW 11 overlap in {overlaps}",
        )

    def test_memory_usage_map_symbolic_resolution(self):
        networks = [
            {
                "title": "Symbolic IO",
                "instructions": ['A "START_PB"', '= "MOTOR_RUN"'],
            }
        ]
        symbols = {"START_PB": "I 0.0", "MOTOR_RUN": "Q 0.0"}
        res = json.loads(MODULE.s7_get_memory_usage_map(networks=networks, symbols=symbols))
        self.assertTrue(res["success"])
        self.assertEqual(res["inputs"]["used_bytes"], 1)
        self.assertEqual(res["outputs"]["used_bytes"], 1)
        self.assertTrue(any("0:X" in row for row in res["inputs"]["bit_grid"]))
        self.assertTrue(any("0:X" in row for row in res["outputs"]["bit_grid"]))

    def test_write_program_with_spaced_block_name(self):
        networks = [{"title": "FC Test", "instructions": ["NOP 0"]}]
        res = json.loads(MODULE.s7_write_program(networks=networks, block_name="FC 10"))
        self.assertEqual(res["status"], "OFFLINE_SOURCE_GENERATED")
        awl_path = Path(res["awl_source_path"])
        content = awl_path.read_text(encoding="utf-8")
        self.assertIn("FUNCTION FC 10", content)

    def test_read_program_offline_fallback(self):
        proj = "Offline_Read_Proj"
        res_create = json.loads(MODULE.s7_create_project(project_name=proj))
        self.assertTrue(res_create["success"])
        # s7_read_program in offline mode should find OB1.awl in project
        res_read = json.loads(MODULE.s7_read_program("OB1"))
        self.assertTrue(res_read["success"])
        self.assertEqual(res_read["mode"], "offline")
        self.assertEqual(res_read["block_number"], "1")

    def test_deep_analysis_counts_word_and_float_ops_and_unmatched_paren(self):
        networks = [
            {
                "title": "Float and DWord math",
                "instructions": [
                    "L 1.5",
                    "L 2.5",
                    "+R",
                    "T MD 10",
                    "==R",
                    "AW",
                    "ROUND",
                ],
            },
            {
                "title": "Unmatched parenthesis",
                "instructions": ["A I 0.0", ")", "= Q 0.0"],
            },
        ]
        res = json.loads(MODULE.s7_analyze_program(networks=networks))
        self.assertTrue(res["success"])
        self.assertGreater(res["metrics"]["word_operations"], 3)
        self.assertTrue(any("Unmatched closing parenthesis" in h for h in res["hazards"]))

    def test_dismiss_popups_multilingual(self):
        class MockBtn:
            def __init__(self, text):
                self._text = text
                self.clicked = False

            def friendly_class_name(self):
                return "Button"

            def window_text(self):
                return self._text

            def click(self):
                self.clicked = True

        class MockStatic:
            def friendly_class_name(self):
                return "Static"

            def window_text(self):
                return "Voulez-vous enregistrer les modifications ?"

        class MockDlg:
            def __init__(self, buttons):
                self._children = [MockStatic()] + [MockBtn(b) for b in buttons]
                self.handle = 12345

            def class_name(self):
                return "#32770"

            def is_enabled(self):
                return True

            def window_text(self):
                return "SIMATIC Confirmation"

            def children(self):
                return self._children

        ctrl = MODULE.SimaticManagerController()
        ctrl.app = type("MockApp", (), {
            "windows": lambda self, visible_only=True: [MockDlg(["Oui", "Non"])]
        })()
        msgs = ctrl._dismiss_popups(confirm=True, timeout=0.1)
        self.assertTrue(any("enregistrer" in m for m in msgs))


class TestSimaticS7EnhancementsAndEdgeCases(unittest.TestCase):
    def test_instruction_comments_supported(self):
        networks = [
            {
                "title": "Network with comments",
                "instructions": [
                    "// Check start button",
                    "A I 0.0",
                    "// Set latch marker",
                    "= M 0.0",
                ],
            }
        ]
        report = MODULE.validate_and_analyze_program(networks)
        self.assertTrue(report.valid, f"Validation failed: {report.errors}")
        awl = MODULE.generate_s7_awl_source(report.normalized_networks)
        self.assertIn("// Check start button", awl)
        self.assertIn("// Set latch marker", awl)

        # Signature ignores pure comments
        sig = MODULE.s7_instruction_signature([{"instructions": report.normalized_networks[0].instructions}])
        self.assertEqual(len(sig), 2)
        self.assertEqual(sig[0][0], "A")
        self.assertEqual(sig[1][0], "=")

    def test_parse_s7_symbol_table_standard_keys(self):
        with tempfile.TemporaryDirectory() as td:
            # 1. Native Siemens 126 ASC format
            asc_path = Path(td) / "symbols.asc"
            asc_content = (
                f"126,{'START_PB':<24}{'I 0.0':<12}{'BOOL':<10}Start Pushbutton\r\n"
                f"126,{'MOTOR_RUN':<24}{'Q 0.0':<12}{'BOOL':<10}Motor Run Output\r\n"
            )
            asc_path.write_bytes(asc_content.encode("latin1"))

            res = MODULE.parse_s7_symbol_table(asc_path)
            self.assertIn("symbols", res)
            self.assertIn("entries", res)
            self.assertIn("file", res)
            self.assertEqual(res["symbol_count"], 2)
            self.assertEqual(res["symbols"][0]["symbol"], "START_PB")
            self.assertEqual(res["symbols"][0]["address"], "I 0.0")

            # 2. SDF format
            sdf_path = Path(td) / "symbols.sdf"
            sdf_content = (
                "Symbol\tAddress\tData type\tComment\r\n"
                "STOP_PB\tI 0.1\tBOOL\tStop Pushbutton\r\n"
            )
            sdf_path.write_text(sdf_content, encoding="utf-8")
            res_sdf = MODULE.parse_s7_symbol_table(sdf_path)
            self.assertIn("symbols", res_sdf)
            self.assertIn("entries", res_sdf)
            self.assertIn("file", res_sdf)
            self.assertEqual(res_sdf["symbol_count"], 1)

    def test_find_default_symbol_table_file_asc(self):
        with tempfile.TemporaryDirectory() as td:
            orig_ws = MODULE.WORKSPACE_DIR
            try:
                MODULE.WORKSPACE_DIR = Path(td)
                sym_dir = MODULE.WORKSPACE_DIR / "Symbols"
                sym_dir.mkdir(parents=True)
                asc_file = sym_dir / "symbols.asc"
                asc_content = (
                    f"126,{'RESET_PB':<24}{'I 0.2':<12}{'BOOL':<10}Reset Pushbutton\r\n"
                )
                asc_file.write_bytes(asc_content.encode("latin1"))

                found = MODULE.find_default_symbol_table_file()
                self.assertIsNotNone(found)
                self.assertEqual(found.name, "symbols.asc")

                # Test s7_read_device_comments picks it up automatically
                read_res = json.loads(MODULE.s7_read_device_comments())
                self.assertTrue(read_res["success"])
                self.assertEqual(read_res["symbol_count"], 1)
                self.assertEqual(read_res["device_comments"]["I 0.2"], "Reset Pushbutton")

                # Test s7_search_symbols_and_comments picks it up
                search_res = json.loads(MODULE.s7_search_symbols_and_comments(query="RESET*"))
                self.assertTrue(search_res["success"])
                self.assertEqual(len(search_res["results"]), 1)
                self.assertEqual(search_res["results"][0]["symbol"], "RESET_PB")
            finally:
                MODULE.WORKSPACE_DIR = orig_ws

    def test_executable_discovery(self):
        mgr = MODULE.find_step7_manager_exe()
        if mgr and mgr.is_file():
            editor = MODULE.find_step7_editor_exe()
            self.assertIsNotNone(editor)
            self.assertTrue(editor.is_file())
            self.assertEqual(editor.name.upper(), "S7KAFAPX.EXE")

            symbol_ed = MODULE.find_step7_symbol_exe()
            self.assertIsNotNone(symbol_ed)
            self.assertTrue(symbol_ed.is_file())
            self.assertEqual(symbol_ed.name.lower(), "s7symapx.exe")

            plcsim = MODULE.find_step7_plcsim_exe()
            self.assertIsNotNone(plcsim)
            self.assertTrue(plcsim.is_file())
            self.assertEqual(plcsim.name.upper(), "S7USSAPX.EXE")

            hwcnf = MODULE.find_step7_hwcnf_exe()
            self.assertIsNotNone(hwcnf)
            self.assertTrue(hwcnf.is_file())
            self.assertEqual(hwcnf.name.upper(), "S7HWCNFX.EXE")

    def test_open_block_offline_fallback(self):
        ctrl = MODULE.SimaticManagerController()
        # Mock connect returning False (offline)
        ctrl.connect = lambda timeout=0.5: (False, "Offline")
        ok, msg, meta = ctrl.open_block("OB999_NONEXISTENT")
        self.assertFalse(ok)
        self.assertIn("not found", msg)

    def test_priority1_export_current_awl_never_synthesizes_nop0_backup(self):
        """Priority 1 Audit Fix: Never synthesize fake NOP 0 backup files when block export fails."""
        ctrl = MODULE.SimaticManagerController()
        ctrl.connect = lambda timeout=0.5: (True, "Connected")
        ctrl.main_win = None
        if getattr(ctrl, "com_bridge", None) is not None:
            ctrl.com_bridge.export_block_to_awl = lambda block_name, output_path, project_name=None: (
                False,
                "Simulated COM export failure",
            )
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_file = Path(tmp_dir) / "should_not_exist_nop0.awl"
            ok, msg = ctrl.export_current_awl(out_file)
            self.assertFalse(ok)
            self.assertFalse(out_file.exists(), "export_current_awl must never create a synthetic NOP 0 file!")
            self.assertIn("Refusing to synthesize a dummy NOP 0 backup", msg)

    def test_priority2_headless_mouse_move_and_set_focus_monkeypatch(self):
        """Priority 2 Audit Fix: pywinauto mouse.move and HwndWrapper.set_focus are safe on headless/locked sessions."""
        if getattr(MODULE, "_pywinauto_mouse", None) is not None:
            self.assertIsNone(MODULE._pywinauto_mouse.move(100, 200))
        if getattr(MODULE, "HwndWrapper", None) is not None:
            self.assertEqual(MODULE.HwndWrapper.set_focus.__name__, "_headless_safe_set_focus")

    def test_priority2_dismiss_popups_closes_modal_and_reenables_parent_before_raising(self):
        """Priority 2 Audit Fix: Unrecognized modal dialogs must be closed before raising RuntimeError."""
        ctrl = MODULE.SimaticManagerController()

        class DummyStatic:
            def friendly_class_name(self):
                return "Static"

            def window_text(self):
                return "Unexpected STEP 7 Dialog (9999:99)"

        class DummyButton:
            def friendly_class_name(self):
                return "Button"

            def window_text(self):
                return "Abort"

        class DummyPopup:
            def __init__(self):
                self.handle = 999
                self.closed = False

            def class_name(self):
                return "#32770"

            def is_enabled(self):
                return True

            def window_text(self):
                return "SIMATIC Manager (9999:99)"

            def children(self):
                return [DummyStatic(), DummyButton()]

            def close(self):
                self.closed = True

        popup = DummyPopup()

        class DummyApp:
            def windows(self, visible_only=True):
                return [popup]

        ctrl.app = DummyApp()
        ctrl.main_win = None
        with self.assertRaises(RuntimeError) as ctx:
            ctrl._dismiss_popups()
        self.assertIn("9999:99", str(ctx.exception))
        self.assertTrue(popup.closed, "Modal popup must be closed before raising RuntimeError!")

    def test_priority4_lvitem32_structure_size_and_headerless_sdf_parsing(self):
        """Priority 4 Audit Fix: _LVITEM32 is exactly 40 bytes on 64-bit Python, and headerless .sdf keeps row 1."""
        import ctypes
        self.assertEqual(ctypes.sizeof(MODULE._LVITEM32), 40)

        sdf_content = (
            '"START_PB","I 0.0","BOOL","Start Pushbutton"\n'
            '"MOTOR_RUN","Q 0.0","BOOL","Main Motor Contactor"\n'
        )
        parsed = MODULE.parse_s7_symbol_table(sdf_content)
        self.assertIn("START_PB", parsed["symbol_map"])
        self.assertEqual(parsed["symbol_map"]["START_PB"], "I 0.0")
        self.assertIn("MOTOR_RUN", parsed["symbol_map"])
        self.assertEqual(parsed["symbol_count"], 2)

    def test_priority4_real_step7_com_bridge_integration(self):
        """Priority 4 Audit Fix: Real integration test against native 32-bit Simatic.Simatic COM engine."""
        bridge = MODULE.SimaticComBridge()
        if not bridge.is_available():
            self.skipTest("32-bit PowerShell host not available")

        proj_res = json.loads(MODULE.s7_com_list_projects())
        if not proj_res.get("success"):
            self.skipTest(f"STEP 7 COM not registered: {proj_res.get('message')}")

        self.assertGreater(proj_res["user_project_count"], 0)
        test_projs = [p for p in proj_res["user_projects"] if p["name"].upper() == "TEST"]
        self.assertTrue(len(test_projs) > 0, "Expected 'TEST' project in STEP 7 COM catalog")

        blk_res = json.loads(MODULE.s7_com_list_blocks(project_name="TEST"))
        self.assertTrue(blk_res["success"])
        ob1_entries = [b for b in blk_res["blocks"] if b["name"].upper() == "OB1"]
        self.assertEqual(len(ob1_entries), 1)
        self.assertEqual(ob1_entries[0]["language"], "LAD")

        read_res = json.loads(MODULE.s7_com_read_block(block_name="OB1", project_name="TEST"))
        self.assertTrue(read_res["success"])
        self.assertEqual(read_res["network_count"], 69)

    def test_priority5_real_s7comm_loopback_tcp_read_write_and_safety_guardrail(self):
        """Priority 5 Audit Fix: Real TCP socket integration test using S7CommLoopbackServer and S7RuntimeClient."""
        server = MODULE.S7CommLoopbackServer(host="127.0.0.1", port=0)
        port = server.start()
        try:
            # 1. Ping over real TCP socket (RFC 1006 + COTP + S7comm Setup Communication)
            ping_res = json.loads(MODULE.s7_runtime_ping(ip_address="127.0.0.1", port=port))
            self.assertTrue(ping_res["connected"])
            self.assertEqual(ping_res["transport"], "s7comm_iso_tcp")

            # 2. Verify safety guardrail blocks unconfirmed writes when S7_WRITES_ENABLED is not set
            blocked_res = json.loads(
                MODULE.s7_runtime_write(
                    address="MW10",
                    value=1234,
                    ip_address="127.0.0.1",
                    port=port,
                    confirm_live_write=False,
                )
            )
            self.assertFalse(blocked_res["success"])
            self.assertEqual(blocked_res["status"], "WRITE_GUARDRAIL_BLOCKED")

            # 3. Round-trip BIT, BYTE, WORD, INT, DWORD, REAL over real TCP sockets
            cases = [
                ("M0.0", True, None),
                ("M0.1", False, None),
                ("MB2", 0xAB, None),
                ("MW10", 4321, "WORD"),
                ("DB1.DBW4", -1234, "INT"),
                ("MD20", 98765432, "DWORD"),
            ]
            for addr, val, dtype in cases:
                wr = json.loads(
                    MODULE.s7_runtime_write(
                        address=addr,
                        value=val,
                        data_type=dtype,
                        ip_address="127.0.0.1",
                        port=port,
                        confirm_live_write=True,
                    )
                )
                self.assertTrue(wr["success"], f"Write failed for {addr}: {wr}")
                rd = json.loads(
                    MODULE.s7_runtime_read(
                        address=addr,
                        data_type=dtype,
                        ip_address="127.0.0.1",
                        port=port,
                    )
                )
                self.assertTrue(rd["success"], f"Read failed for {addr}: {rd}")
                self.assertEqual(rd["value"], val)

            # REAL float round-trip
            wr_real = json.loads(
                MODULE.s7_runtime_write(
                    address="DB1.DBD30",
                    value=3.14159,
                    data_type="REAL",
                    ip_address="127.0.0.1",
                    port=port,
                    confirm_live_write=True,
                )
            )
            self.assertTrue(wr_real["success"])
            rd_real = json.loads(
                MODULE.s7_runtime_read(
                    address="DB1.DBD30",
                    data_type="REAL",
                    ip_address="127.0.0.1",
                    port=port,
                )
            )
            self.assertTrue(rd_real["success"])
            self.assertAlmostEqual(rd_real["value"], 3.14159, places=4)
        finally:
            server.stop()

    def test_ai_agent_ergonomics_flexible_program_inputs(self):
        """AI-Agent Ergonomics: validate_and_analyze_program accepts raw AWL strings, flat lists, multiline strings, and >64 char titles."""
        # 1. Raw AWL string
        raw_awl = """
        NETWORK
        TITLE = Raw AWL Network
          A I 0.0;
          = Q 0.0;
        """
        rep_raw = MODULE.validate_and_analyze_program(raw_awl)
        self.assertTrue(rep_raw.valid, f"Raw AWL failed validation: {rep_raw.errors}")
        self.assertEqual(len(rep_raw.normalized_networks), 1)

        # 2. Flat list of instruction strings
        rep_flat = MODULE.validate_and_analyze_program(["A I 0.0;", "A I 0.1;", "= Q 0.0;"])
        self.assertTrue(rep_flat.valid, f"Flat list failed validation: {rep_flat.errors}")
        self.assertEqual(len(rep_flat.normalized_networks), 1)

        # 3. Multiline instruction string + >64 char title (auto-truncated with warning, not error)
        long_title = "Network Title Exceeding Sixty Four Characters For AI Agent Ergonomics Verification Test"
        rep_multi = MODULE.validate_and_analyze_program([
            {"title": long_title, "instructions": ["A I 0.0;\nA I 0.1;\n= Q 0.0;"]}
        ])
        self.assertTrue(rep_multi.valid, f"Multiline/long-title failed validation: {rep_multi.errors}")
        self.assertEqual(len(rep_multi.normalized_networks[0]["title"]), 64)
        self.assertTrue(any("truncated to 64 characters" in w for w in rep_multi.warnings))

        # 4. Full multi-network AWL block string with ORGANIZATION_BLOCK envelope
        full_awl = """ORGANIZATION_BLOCK OB 1
TITLE = "Main Scan"
VERSION : 0.1
BEGIN
NETWORK
TITLE = Rung 1
  A I 0.0;
  = Q 0.0;
NETWORK
TITLE = Rung 2
  A I 0.1;
  = Q 0.1;
END_ORGANIZATION_BLOCK
"""
        rep_full = MODULE.validate_and_analyze_program(full_awl)
        self.assertTrue(rep_full.valid, f"Full AWL block failed validation: {rep_full.errors}")
        self.assertEqual(len(rep_full.normalized_networks), 2)
        self.assertEqual(rep_full.normalized_networks[0]["title"], "Rung 1")
        self.assertEqual(rep_full.normalized_networks[1]["title"], "Rung 2")

    def test_official_siemens_lad_call_bld_and_symbolic_db_parsing(self):
        """Official Siemens LAD exports with BLD 103, multiline CALL ( param := val ), and symbolic DB members parse and validate cleanly."""
        from lad_converter import validate_lad_source
        siemens_lad_ob1 = """ORGANIZATION_BLOCK "Cycling"
TITLE = "Official Siemens LAD OB1"
VERSION : 0.1
BEGIN
NETWORK
TITLE = Petrol Engine Call
      O     "S_On";
      O     "T_On";
      =     L     20.0;
      BLD   103;
      A     "S_Rise";
      =     L     20.1;
      BLD   103;
      A     "S_More";
      =     L     20.2;
      BLD   103;
      CALL "Engine" , "Petrol" (
           Switch_On                := L     20.0,
           Setpoint_Rise            := L     20.1,
           More_Gas                 := L     20.2,
           Preset_Speed             := 1500,
           Actual_Speed             := "S_Data".PE_Actual_Speed,
           Preset_Speed_Reached     := "Light_1");
      NOP   0;
END_ORGANIZATION_BLOCK
"""
        lad_rep = validate_lad_source(siemens_lad_ob1)
        self.assertTrue(lad_rep.is_lad_compliant, f"Official Siemens LAD flagged: {lad_rep.violations}")

        val_rep = MODULE.validate_and_analyze_program(siemens_lad_ob1)
        self.assertTrue(val_rep.valid, f"Official Siemens LAD failed validation: {val_rep.errors}")

        tmp_awl = MODULE.new_s7_artifact_path("test-siemens-lad-ob1")
        try:
            MODULE.save_s7_artifact(siemens_lad_ob1, tmp_awl)
            parsed = MODULE.parse_s7_awl_source(tmp_awl)
            self.assertNotIn("error", parsed)
            self.assertEqual(parsed["network_count"], 1)
            insts = parsed["networks"][0]["instructions"]
            self.assertTrue(any(i.startswith('CALL "Engine"') for i in insts), f"CALL not joined: {insts}")
            self.assertFalse(any(i.startswith("SWITCH_ON:") for i in insts), f":= misparsed as label: {insts}")
        finally:
            if tmp_awl.exists():
                tmp_awl.unlink()

    def test_com_write_block_preflight_rejection_and_rollback_protection(self):
        """s7_com_write_block rejects invalid AWL before touching STEP 7 and rolls back from backup if COM compile fails."""
        # 1. Invalid AWL opcode rejected BEFORE touching COM bridge
        bad_res = json.loads(
            MODULE.s7_com_write_block(
                block_name="FC99",
                source_code="FUNCTION FC 99 : VOID\nBEGIN\nNETWORK\nBOGUS_OP ???;\nEND_FUNCTION\n",
                project_name="TEST",
                convert_to_lad=False,
            )
        )
        self.assertFalse(bad_res["success"])
        self.assertEqual(bad_res["status"], "REJECTED_BEFORE_WRITE")


if __name__ == "__main__":
    unittest.main()
