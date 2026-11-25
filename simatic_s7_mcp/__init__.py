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
    S7_BLOCK_TYPE_CODES,
    S7_CONN_TYPES,
    EnhancedS7LoopbackServer,
    PLCDataCollector,
    ParsedS7Address,
    S7ConnectionPool,
    VirtualS7PLC,
    WatchTableMonitor,
    build_cotp_cr,
    build_list_blocks_count_pdu,
    build_list_blocks_of_type_pdu,
    decode_s7_value,
    encode_s7_value,
    parse_s7_address,
)
from .s7_stdlib import (
    S7_STDLIB_ALIASES,
    S7_STDLIB_SCHEMAS,
    execute_stdlib_block,
    get_stdlib_schema,
    parse_any_pointer,
    parse_iec_time_ms,
    resolve_stdlib_block_name,
    run_closed_loop_plant_sim,
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
    "S7_CONN_TYPES",
    "S7_BLOCK_TYPE_CODES",
    "EnhancedS7LoopbackServer",
    "PLCDataCollector",
    "ParsedS7Address",
    "S7ConnectionPool",
    "VirtualS7PLC",
    "WatchTableMonitor",
    "build_cotp_cr",
    "build_list_blocks_count_pdu",
    "build_list_blocks_of_type_pdu",
    "decode_s7_value",
    "encode_s7_value",
    "parse_s7_address",
    "S7_STDLIB_ALIASES",
    "S7_STDLIB_SCHEMAS",
    "parse_iec_time_ms",
    "parse_any_pointer",
    "resolve_stdlib_block_name",
    "get_stdlib_schema",
    "execute_stdlib_block",
    "run_closed_loop_plant_sim",
    "run_doctor",
    "generate_client_config",
    "build_catalog_resource_text",
    "build_iec_reference_resource_text",
    "build_prompt_new_project_wizard",
    "build_prompt_safe_commissioning",
    "build_prompt_convert_and_verify_lad",
    "build_prompt_plcsim_automated_test",
]
