import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lad_converter import (
    LADViolation,
    convert_awl_to_pure_lad,
    evaluate_stl_rlo,
    parse_awl_networks,
    validate_lad_source,
    verify_boolean_equivalence,
)

SAMPLE_NET32_AWL = """ORGANIZATION_BLOCK OB 1
TITLE = "Test Net 32"
VERSION : 0.1
VAR_TEMP
  TEMP_VAR : INT ;
END_VAR
BEGIN

NETWORK
TITLE = Network 32: Step 122 & 128 Elevator Down Actuation
// Actuates Elevator Down Q0.0 during Step 34 and Step 40
      A     "STEP_34";
      O     "STEP_40";
      AN    "ELEVATOR_UP";
      S     "ELEVATOR_DOWN";
      R     "ELEVATOR_UP";

END_ORGANIZATION_BLOCK
"""

SAMPLE_MULTI_RESET_AWL = """ORGANIZATION_BLOCK OB 1
TITLE = "Test Multi Reset"
VERSION : 0.1
VAR_TEMP
  TEMP_VAR : INT ;
END_VAR
BEGIN

NETWORK
TITLE = Network 11: Reset Actuators
      A     "RESET_EDGE";
      R     "ELEVATOR_UP";
      R     "PUSHER_CYL";
      R     "AIRSLIDE_VALVE";

END_ORGANIZATION_BLOCK
"""

SAMPLE_BARE_MOVE_AWL = """ORGANIZATION_BLOCK OB 1
TITLE = "Test Bare Move"
VERSION : 0.1
VAR_TEMP
  TEMP_VAR : INT ;
END_VAR
BEGIN

NETWORK
TITLE = Network 3: First Scan Setup
      AN    "FIRST_SCAN_DONE";
      L     W#16#3081;
      T     MW   840;

END_ORGANIZATION_BLOCK
"""

AUDIT_3_2_A_BOOLEAN_INVERSION_AWL = """ORGANIZATION_BLOCK OB 1
TITLE = "Audit 3.2.A Boolean Precedence"
VERSION : 0.1
BEGIN

NETWORK
TITLE = (START_PB AND SAFETY_OK) OR MANUAL_BYPASS
      A     "START_PB";
      A     "SAFETY_OK";
      O     "MANUAL_BYPASS";
      =     "MOTOR_RUN";

END_ORGANIZATION_BLOCK
"""

AUDIT_3_2_B_MULTI_MOVE_AWL = """ORGANIZATION_BLOCK OB 1
TITLE = "Audit 3.2.B Multi Move Ordering"
VERSION : 0.1
BEGIN

NETWORK
TITLE = Dual Load Transfer
      A     "EN";
      L     100;
      T     MW 10;
      L     200;
      T     MW 12;

END_ORGANIZATION_BLOCK
"""

AUDIT_3_2_B_ARITHMETIC_AWL = """ORGANIZATION_BLOCK OB 1
TITLE = "Audit 3.2.B Arithmetic Preservation"
VERSION : 0.1
BEGIN

NETWORK
TITLE = Add Integer
      A     "CALC_EN";
      L     MW 10;
      L     MW 12;
      +I    ;
      T     MW 14;

END_ORGANIZATION_BLOCK
"""

AUDIT_3_2_C_INDEPENDENT_RUNGS_AWL = """ORGANIZATION_BLOCK OB 1
TITLE = "Audit 3.2.C Independent Rungs"
VERSION : 0.1
BEGIN

NETWORK
TITLE = Two Independent Coils in Single STL Network
      A     I 0.0;
      =     M 0.0;
      A     I 0.1;
      =     M 0.1;

END_ORGANIZATION_BLOCK
"""

AUDIT_INTERFACE_FC_AWL = """FUNCTION FC 10 : VOID
TITLE = "Safety Interlock Function"
AUTHOR : SIEMENS
FAMILY : SAFETY
NAME : SAFE_FC
VERSION : 1.2

VAR_INPUT
  START_PB : BOOL ;
  SAFETY_OK : BOOL ;
  MANUAL_BYPASS : BOOL ;
END_VAR
VAR_OUTPUT
  MOTOR_RUN : BOOL ;
END_VAR
VAR_IN_OUT
  STATE_WORD : WORD ;
END_VAR
VAR_TEMP
  TEMP_FLAG : BOOL ;
END_VAR
BEGIN

NETWORK
TITLE = Interlock Rung
      A     #START_PB;
      A     #SAFETY_OK;
      O     #MANUAL_BYPASS;
      =     #MOTOR_RUN;

END_FUNCTION
"""

AUDIT_INTERFACE_FB_AWL = """FUNCTION_BLOCK FB 5
TITLE = "Valve Controller FB"
VERSION : 0.1

VAR_INPUT
  OPEN_CMD : BOOL ;
  CLOSE_CMD : BOOL ;
END_VAR
VAR_OUTPUT
  VALVE_OUT : BOOL ;
END_VAR
VAR
  LATCH_STATE : BOOL ;
END_VAR
VAR_TEMP
  SCRATCH : INT ;
END_VAR
BEGIN

NETWORK
TITLE = Latch Valve
      A     #OPEN_CMD;
      AN    #CLOSE_CMD;
      S     #VALVE_OUT;
      R     #LATCH_STATE;

END_FUNCTION_BLOCK
"""


def test_detects_network_32_violations():
    report = validate_lad_source(SAMPLE_NET32_AWL)
    assert not report.is_lad_compliant
    assert report.total_networks == 1
    assert report.compliant_networks == 0

    rule_names = {v.rule_name for v in report.violations}
    assert "Single_Coil_Rule" in rule_names
    assert "Parenthesized_OR_Rule" in rule_names


def test_converts_network_32_to_pure_lad():
    converted = convert_awl_to_pure_lad(SAMPLE_NET32_AWL, block_title="Fixed Net 32")
    report = validate_lad_source(converted)
    assert report.is_lad_compliant
    assert report.compliant_networks == report.total_networks
    assert len(report.violations) == 0

    # Verify that it was split into 2 networks with parenthesized OR
    networks = parse_awl_networks(converted)
    assert len(networks) == 2

    # Verify parenthesization in both networks
    for net in networks:
        inst_text = " ".join(net["instructions"])
        assert "A(" in inst_text
        assert "O" in inst_text
        assert ")" in inst_text
        assert "AN" in inst_text


def test_detects_and_converts_multi_reset():
    report = validate_lad_source(SAMPLE_MULTI_RESET_AWL)
    assert not report.is_lad_compliant
    assert any(v.rule_name == "Single_Coil_Rule" for v in report.violations)

    converted = convert_awl_to_pure_lad(SAMPLE_MULTI_RESET_AWL)
    report_converted = validate_lad_source(converted)
    assert report_converted.is_lad_compliant
    assert len(parse_awl_networks(converted)) == 3


def test_detects_and_converts_bare_move():
    report = validate_lad_source(SAMPLE_BARE_MOVE_AWL)
    assert not report.is_lad_compliant
    assert any(v.rule_name == "LAD_MOVE_Box_Rule" for v in report.violations)

    converted = convert_awl_to_pure_lad(SAMPLE_BARE_MOVE_AWL)
    report_converted = validate_lad_source(converted)
    assert report_converted.is_lad_compliant

    net = parse_awl_networks(converted)[0]
    inst_text = " ".join(net["instructions"])
    assert "JNB" in inst_text
    assert "NOP" in inst_text and "0" in inst_text


def test_audit_3_2_a_no_boolean_inversion():
    """
    Verify Audit 3.2.A:
    Input: A "START_PB"; A "SAFETY_OK"; O "MANUAL_BYPASS"; = "MOTOR_RUN";
    Meaning: (START_PB AND SAFETY_OK) OR MANUAL_BYPASS
    Must NOT be inverted into (START_PB OR MANUAL_BYPASS) AND SAFETY_OK.
    """
    converted = convert_awl_to_pure_lad(AUDIT_3_2_A_BOOLEAN_INVERSION_AWL)
    report = validate_lad_source(converted)
    assert report.is_lad_compliant, report.violations

    nets = parse_awl_networks(converted)
    assert len(nets) == 1
    insts = nets[0]["instructions"]

    # Check exact truth table across all 8 combinations of (START_PB, SAFETY_OK, MANUAL_BYPASS)
    orig_insts = ['A "START_PB"', 'A "SAFETY_OK"', 'O "MANUAL_BYPASS"']
    conv_contact_insts = [i for i in insts if not i.startswith("=")]
    eq, msg = verify_boolean_equivalence(orig_insts, conv_contact_insts)
    assert eq, msg

    # Explicitly test the emergency bypass hazard from the audit:
    # MANUAL_BYPASS=True, SAFETY_OK=False, START_PB=False -> must evaluate to True!
    hazard_env = {'"START_PB"': False, '"SAFETY_OK"': False, '"MANUAL_BYPASS"': True}
    assert evaluate_stl_rlo(conv_contact_insts, hazard_env) is True

    # Explicitly test SAFETY_OK=True, MANUAL_BYPASS=False, START_PB=False -> must be False
    idle_env = {'"START_PB"': False, '"SAFETY_OK"': True, '"MANUAL_BYPASS"': False}
    assert evaluate_stl_rlo(conv_contact_insts, idle_env) is False


def test_audit_3_2_b_multi_move_ordering_and_arithmetic_preservation():
    """
    Verify Audit 3.2.B:
    1) Multiple L/T pairs must NOT be reordered into L 100; L 200; T MW 10; T MW 12;
       (which clobbers accumulator 1 and writes 200 to both MW 10 and MW 12).
    2) Arithmetic instructions (+I, -I, *I, /I) must NEVER be stripped.
    """
    converted_moves = convert_awl_to_pure_lad(AUDIT_3_2_B_MULTI_MOVE_AWL)
    report_moves = validate_lad_source(converted_moves)
    assert report_moves.is_lad_compliant, report_moves.violations

    nets = parse_awl_networks(converted_moves)
    assert len(nets) == 2
    assert "L 100" in nets[0]["clean_instructions"]
    assert "T MW 10" in nets[0]["clean_instructions"]
    assert "L 200" not in nets[0]["clean_instructions"]
    assert "L 200" in nets[1]["clean_instructions"]
    assert "T MW 12" in nets[1]["clean_instructions"]

    # Check arithmetic preservation (+I)
    converted_arith = convert_awl_to_pure_lad(AUDIT_3_2_B_ARITHMETIC_AWL)
    nets_arith = parse_awl_networks(converted_arith)
    assert len(nets_arith) == 1
    arith_insts = nets_arith[0]["clean_instructions"]
    assert "+I" in arith_insts
    # Verify order: L MW 10 -> L MW 12 -> +I -> T MW 14
    idx_l10 = arith_insts.index("L MW 10")
    idx_l12 = arith_insts.index("L MW 12")
    idx_add = arith_insts.index("+I")
    idx_t14 = arith_insts.index("T MW 14")
    assert idx_l10 < idx_l12 < idx_add < idx_t14


def test_audit_3_2_c_multi_coil_no_cross_contamination():
    """
    Verify Audit 3.2.C:
    Input: A I 0.0; = M 0.0; A I 0.1; = M 0.1;
    In S7 STL, '=' resets the First-Check (FC) bit to 0.
    Network 1 must ONLY have A I 0.0; = M 0.0;
    Network 2 must ONLY have A I 0.1; = M 0.1;
    """
    converted = convert_awl_to_pure_lad(AUDIT_3_2_C_INDEPENDENT_RUNGS_AWL)
    report = validate_lad_source(converted)
    assert report.is_lad_compliant, report.violations

    nets = parse_awl_networks(converted)
    assert len(nets) == 2
    assert nets[0]["clean_instructions"] == ["A I 0.0", "= M 0.0"]
    assert nets[1]["clean_instructions"] == ["A I 0.1", "= M 0.1"]


def test_interface_preservation_fc_and_fb():
    """
    Verify Failure Layer 2.4:
    FUNCTION FC and FUNCTION_BLOCK FB headers, VAR_INPUT, VAR_OUTPUT, VAR_IN_OUT,
    VAR, VAR_TEMP, and END_FUNCTION / END_FUNCTION_BLOCK must be preserved intact.
    """
    converted_fc = convert_awl_to_pure_lad(AUDIT_INTERFACE_FC_AWL)
    assert "FUNCTION FC 10 : VOID" in converted_fc
    assert "VAR_INPUT" in converted_fc
    assert "START_PB : BOOL ;" in converted_fc
    assert "VAR_OUTPUT" in converted_fc
    assert "MOTOR_RUN : BOOL ;" in converted_fc
    assert "VAR_IN_OUT" in converted_fc
    assert "STATE_WORD : WORD ;" in converted_fc
    assert "END_FUNCTION" in converted_fc
    assert "ORGANIZATION_BLOCK" not in converted_fc

    converted_fb = convert_awl_to_pure_lad(AUDIT_INTERFACE_FB_AWL)
    assert "FUNCTION_BLOCK FB 5" in converted_fb
    assert "VAR_INPUT" in converted_fb
    assert "VAR_OUTPUT" in converted_fb
    assert "LATCH_STATE : BOOL ;" in converted_fb
    assert "END_FUNCTION_BLOCK" in converted_fb
    assert "ORGANIZATION_BLOCK" not in converted_fb


def test_converts_full_material_handling_elevator():
    orig_path = Path(r"C:\Users\jethr\Documents\Codex-Simatic7\Material_Handling_Elevator_LAD.awl")
    if not orig_path.exists():
        return
    orig_content = orig_path.read_text(encoding="utf-8")

    # Original has violations (Network 32, Network 17, Network 34, Network 11, etc.)
    report_orig = validate_lad_source(orig_content)
    assert not report_orig.is_lad_compliant
    assert len(report_orig.violations) > 0

    # Convert to pure LAD
    converted = convert_awl_to_pure_lad(
        orig_content, block_title="Material Handling Elevator - Pure 100% LAD"
    )
    report_converted = validate_lad_source(converted)

    # Converted block must have 100% compliance with 0 violations
    assert report_converted.is_lad_compliant, report_converted.violations
    assert len(report_converted.violations) == 0
    assert report_converted.compliant_networks == report_converted.total_networks

    # Verify idempotency: converting the already-converted pure LAD produces identical networks
    reconverted = convert_awl_to_pure_lad(
        converted, block_title="Material Handling Elevator - Pure 100% LAD"
    )
    assert parse_awl_networks(converted) == parse_awl_networks(reconverted)


def test_fp_fn_inline_edge_detection_preserved():
    """
    Verify that inline edge-detection contacts (FP / FN) in official Siemens LAD
    are preserved inside a single rung without splitting into a dangling contact chain.
    """
    awl = """ORGANIZATION_BLOCK OB 1
BEGIN
NETWORK
TITLE = Rising and Falling Edge Rungs
      A     I 0.0;
      FP    M 0.3;
      A     Q 4.2;
      =     Q 4.3;
      A     I 0.1;
      FN    M 0.4;
      =     Q 4.4;
END_ORGANIZATION_BLOCK
"""
    converted = convert_awl_to_pure_lad(awl)
    report = validate_lad_source(converted)
    assert report.is_lad_compliant, report.violations
    nets = parse_awl_networks(converted)
    assert len(nets) == 2
    assert nets[0]["clean_instructions"] == ["A I 0.0", "FP M 0.3", "A Q 4.2", "= Q 4.3"]
    assert nets[1]["clean_instructions"] == ["A I 0.1", "FN M 0.4", "= Q 4.4"]


def test_xor_expansion_and_truth_table():
    """
    Verify that STL XOR (X / XN) instructions are expanded into pure LAD parallel
    branches with 100% truth-table equivalence.
    """
    awl = """ORGANIZATION_BLOCK OB 1
BEGIN
NETWORK
TITLE = XOR Rung
      A     I 0.0;
      X     I 0.1;
      =     Q 0.0;
END_ORGANIZATION_BLOCK
"""
    orig_report = validate_lad_source(awl)
    assert not orig_report.is_lad_compliant
    converted = convert_awl_to_pure_lad(awl)
    conv_report = validate_lad_source(converted)
    assert conv_report.is_lad_compliant, conv_report.violations
    nets = parse_awl_networks(converted)
    assert len(nets) == 1
    conv_contacts = [i for i in nets[0]["instructions"] if not i.startswith("=")]
    eq, ce = verify_boolean_equivalence(["A I 0.0", "X I 0.1"], conv_contacts)
    assert eq, ce


def test_midline_not_after_coil_preserved():
    """
    Verify that midline NOT after a coil (A I 0.0; = M 0.0; NOT; = M 0.1;)
    inverts the preserved upstream RLO rather than wiping out A I 0.0.
    """
    awl = """ORGANIZATION_BLOCK OB 1
BEGIN
NETWORK
TITLE = Midline NOT
      A     I 0.0;
      =     M 0.0;
      NOT   ;
      =     M 0.1;
END_ORGANIZATION_BLOCK
"""
    converted = convert_awl_to_pure_lad(awl)
    report = validate_lad_source(converted)
    assert report.is_lad_compliant, report.violations
    nets = parse_awl_networks(converted)
    assert len(nets) == 2
    assert nets[0]["clean_instructions"] == ["A I 0.0", "= M 0.0"]
    assert nets[1]["clean_instructions"] == ["AN I 0.0", "= M 0.1"]


def test_multi_jnb_boxes_in_single_network():
    """
    Verify that a network containing multiple JNB ... NOP 0 boxes is decomposed
    into independent single-box LAD networks without cross-contaminating contacts
    or leaving undefined jump labels.
    """
    awl = """ORGANIZATION_BLOCK OB 1
BEGIN
NETWORK
TITLE = Two JNB Boxes
      A     I 0.0;
      JNB   _001;
      L     10;
      T     MW 10;
_001: NOP   0;
      A     I 0.1;
      JNB   _002;
      L     20;
      T     MW 12;
_002: NOP   0;
END_ORGANIZATION_BLOCK
"""
    converted = convert_awl_to_pure_lad(awl)
    report = validate_lad_source(converted)
    assert report.is_lad_compliant, report.violations
    nets = parse_awl_networks(converted)
    assert len(nets) == 2
    assert nets[0]["clean_instructions"] == ["A I 0.0", "JNB m001", "L 10", "T MW 10", "m001: NOP 0"]
    assert nets[1]["clean_instructions"] == ["A I 0.1", "JNB m002", "L 20", "T MW 12", "m002: NOP 0"]


def test_top_level_or_emission_matches_siemens_lad():
    """
    Verify that top-level OrNode on a coil rung emits O( A ...; A ...; ); O ...;
    directly without redundant outer A( ... ).
    """
    converted = convert_awl_to_pure_lad(AUDIT_3_2_A_BOOLEAN_INVERSION_AWL)
    nets = parse_awl_networks(converted)
    assert len(nets) == 1
    assert nets[0]["clean_instructions"] == [
        "O(",
        'A "START_PB"',
        'A "SAFETY_OK"',
        ")",
        'O "MANUAL_BYPASS"',
        '= "MOTOR_RUN"',
    ]


def test_sr_rs_flip_flop_lad_compliance():
    """
    Verify that canonical Siemens LAD SR and RS flip-flop box patterns
    pass validate_lad_source with zero Single_Coil_Rule violations.
    """
    sr_box_awl = """ORGANIZATION_BLOCK OB 1
BEGIN
NETWORK
TITLE = SR Memory Box
      A     "Automatic_On";
      S     "Automatic_Mode";
      A     "Manual_On";
      R     "Automatic_Mode";
      NOP   0;
NETWORK
TITLE = RS Memory Box with Q
      A     I 0.0;
      R     M 0.0;
      A     I 0.1;
      S     M 0.0;
      =     Q 4.0;
      NOP   0;
NETWORK
TITLE = Complex Condition SR Box
      A     #Switch_On;
      AN    "Automatic_Mode";
      S     #Engine_On;
      A(    ;
      O     #Switch_Off;
      ON    #Failure;
      )     ;
      R     #Engine_On;
      NOP   0;
END_ORGANIZATION_BLOCK
"""
    rep = validate_lad_source(sr_box_awl)
    assert rep.is_lad_compliant is True
    assert len(rep.violations) == 0

    # Non-matching targets must still trigger Single_Coil_Rule
    mismatch_awl = """ORGANIZATION_BLOCK OB 1
BEGIN
NETWORK
TITLE = Mismatched Targets
      A     I 0.0;
      S     M 0.0;
      A     I 0.1;
      R     M 0.1;
      NOP   0;
END_ORGANIZATION_BLOCK
"""
    rep_bad = validate_lad_source(mismatch_awl)
    assert rep_bad.is_lad_compliant is False
    assert any(v.rule_name == "Single_Coil_Rule" for v in rep_bad.violations)


class LadConverterUnitTestSuite(unittest.TestCase):
    def test_all(self):
        test_detects_network_32_violations()
        test_converts_network_32_to_pure_lad()
        test_detects_and_converts_multi_reset()
        test_detects_and_converts_bare_move()
        test_audit_3_2_a_no_boolean_inversion()
        test_audit_3_2_b_multi_move_ordering_and_arithmetic_preservation()
        test_audit_3_2_c_multi_coil_no_cross_contamination()
        test_interface_preservation_fc_and_fb()
        test_converts_full_material_handling_elevator()
        test_fp_fn_inline_edge_detection_preserved()
        test_xor_expansion_and_truth_table()
        test_midline_not_after_coil_preserved()
        test_multi_jnb_boxes_in_single_network()
        test_top_level_or_emission_matches_siemens_lad()
        test_sr_rs_flip_flop_lad_compliance()


if __name__ == "__main__":
    unittest.main()
