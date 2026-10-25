import pytest
from lad_converter import (
    validate_lad_source,
    convert_awl_to_pure_lad,
    parse_awl_networks,
    LADViolation,
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


def test_converts_full_material_handling_elevator():
    orig_path = r"C:\Users\jethr\Documents\Codex-Simatic7\Material_Handling_Elevator_LAD.awl"
    with open(orig_path, "r", encoding="utf-8") as f:
        orig_content = f.read()

    # Original has violations (Network 32, Network 11, etc.)
    report_orig = validate_lad_source(orig_content)
    assert not report_orig.is_lad_compliant
    assert len(report_orig.violations) > 0

    # Convert to pure LAD
    converted = convert_awl_to_pure_lad(
        orig_content, block_title="Material Handling Elevator - Pure 100% LAD"
    )
    report_converted = validate_lad_source(converted)

    # Converted block must have 100% compliance with 0 violations
    assert report_converted.is_lad_compliant
    assert len(report_converted.violations) == 0
    assert report_converted.compliant_networks == report_converted.total_networks
