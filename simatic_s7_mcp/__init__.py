"""
SIMATIC STEP 7 Classic MCP Server Core Package.

Provides modular, production-grade engineering engines for Siemens SIMATIC
Manager / STEP 7 Classic (S7-300, S7-400, WinAC):
  - contracts: Access modes, failure taxonomy, SHA-256 content_hash, audit log
  - hw_config: DIN-rail / Rack hardware configuration, MLFB catalog, .cfg codec
  - iec_languages: FBD, SCL (ST), S7-GRAPH (SFC), Global/Instance DB & UDT engines
  - com_worker: Persistent <15ms 32-bit COM & S7ProSim daemon + C++ bridge support
  - runtime_engine: Pooled multi-item S7comm, CPU SZL diagnostics, watch tables & Virtual S7 PLC
  - cli_and_agents: Diagnostics doctor, IDE config generator, MCP resources & prompts
"""

from .cli_and_agents import (
    build_catalog_resource_text,
    build_iec_reference_resource_text,
    build_prompt_convert_and_verify_lad,
    build_prompt_new_project_wizard,
    build_prompt_plcsim_automated_test,
    build_prompt_safe_commissioning,
    generate_client_config,
    run_doctor,
)
from .com_worker import PersistentComWorker
from .contracts import (
    AccessMode,
    AuditLogger,
    FailureCategory,
    canonical_json,
    compute_content_hash,
    make_tool_annotations,
    resolve_access_mode,
)
from .hw_config import (
    S7_HARDWARE_CATALOG,
    StationHardwareConfig,
    parse_station_cfg,
    resolve_catalog_module,
    search_hardware_catalog,
)
from .iec_languages import (
    STEP7_BLKLANG_CODES,
    compile_scl_via_cli,
    compute_s7_struct_layout,
    convert_awl_to_pure_fbd,
    find_step7_scl_compiler_exe,
    generate_data_block_source,
    generate_udt_source,
    validate_and_generate_graph_sfc,
    validate_fbd_source,
    validate_scl_source,
)
from .runtime_engine import (
    EnhancedS7LoopbackServer,
    ParsedS7Address,
    S7ConnectionPool,
    VirtualS7PLC,
    WatchTableMonitor,
    decode_s7_value,
    encode_s7_value,
    parse_s7_address,
)

__all__ = [
    "AccessMode",
    "AuditLogger",
    "FailureCategory",
    "canonical_json",
    "compute_content_hash",
    "make_tool_annotations",
    "resolve_access_mode",
    "S7_HARDWARE_CATALOG",
    "StationHardwareConfig",
    "parse_station_cfg",
    "resolve_catalog_module",
    "search_hardware_catalog",
    "STEP7_BLKLANG_CODES",
    "compile_scl_via_cli",
    "compute_s7_struct_layout",
    "convert_awl_to_pure_fbd",
    "find_step7_scl_compiler_exe",
    "generate_data_block_source",
    "generate_udt_source",
    "validate_and_generate_graph_sfc",
    "validate_fbd_source",
    "validate_scl_source",
    "PersistentComWorker",
    "EnhancedS7LoopbackServer",
    "ParsedS7Address",
    "S7ConnectionPool",
    "VirtualS7PLC",
    "WatchTableMonitor",
    "decode_s7_value",
    "encode_s7_value",
    "parse_s7_address",
    "run_doctor",
    "generate_client_config",
    "build_catalog_resource_text",
    "build_iec_reference_resource_text",
    "build_prompt_new_project_wizard",
    "build_prompt_safe_commissioning",
    "build_prompt_convert_and_verify_lad",
    "build_prompt_plcsim_automated_test",
]
