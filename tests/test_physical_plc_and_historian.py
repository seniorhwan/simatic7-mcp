"""
Verification suite for physical PLC connectivity, S7-300/400 protocol hardening,
COM download remediation, online block listing, and AI-agent data historian.
"""

from __future__ import annotations

import csv
import json
import os
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import simatic_s7_mcp_server as srv
from simatic_s7_mcp import (
    AccessMode,
    EnhancedS7LoopbackServer,
    PLCDataCollector,
    S7ConnectionPool,
    VirtualS7PLC,
    build_cotp_cr,
)
from simatic_s7_mcp.com_worker import _PERSISTENT_PS32_DAEMON_SCRIPT


class TestPhysicalPlcProtocolAndHistorian(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_mode = srv.ACTIVE_ACCESS_MODE
        self._orig_writes = os.environ.get("S7_WRITES_ENABLED")

    def tearDown(self) -> None:
        srv.set_access_mode(self._orig_mode)
        if self._orig_writes is None:
            os.environ.pop("S7_WRITES_ENABLED", None)
        else:
            os.environ["S7_WRITES_ENABLED"] = self._orig_writes

    def test_cotp_tsap_encoding_and_pg_to_op_fallback(self) -> None:
        # Verify exact Siemens S7-300 TSAP bytes for PG (0x01), OP (0x02), S7_BASIC (0x03)
        cr_pg = build_cotp_cr(0, 2, "PG")
        cr_op = build_cotp_cr(0, 2, "OP")
        cr_basic = build_cotp_cr(0, 3, "S7_BASIC")
        self.assertEqual(cr_pg[-2:], bytes([0x01, 0x02]))
        self.assertEqual(cr_op[-2:], bytes([0x02, 0x02]))
        self.assertEqual(cr_basic[-2:], bytes([0x03, 0x03]))

        # Simulate a physical S7-300 whose single PG slot is occupied (only OP accepted)
        plc = VirtualS7PLC(order_number="6ES7 315-2EH14-0AB0")
        plc.write_address("MW 20", 4321, data_type="INT")
        loopback = EnhancedS7LoopbackServer(plc=plc, allowed_connection_types=["OP"])
        port = loopback.start()
        pool = S7ConnectionPool()
        try:
            res = pool.batch_read(
                host="127.0.0.1",
                port=port,
                rack=0,
                slot=2,
                items=[{"address": "MW20", "data_type": "INT"}],
                connection_type="AUTO",
            )
            self.assertTrue(res["ok"])
            self.assertEqual(res["connection_type"], "OP")
            self.assertEqual(res["items"][0]["value"], 4321)
        finally:
            pool.close_all()
            loopback.stop()

    def test_s7_300_240byte_pdu_dynamic_chunking(self) -> None:
        # Classic physical S7-300 CPU 314/315 negotiates a 240-byte PDU limit.
        # Writing 14 DWORDs in a single PDU exceeds 240 bytes -> server rejects with 0x8500
        # unless the client dynamically chunks by negotiated_pdu!
        plc = VirtualS7PLC(order_number="6ES7 314-1AE04-0AB0")
        loopback = EnhancedS7LoopbackServer(plc=plc, max_pdu=240)
        port = loopback.start()
        pool = S7ConnectionPool()
        try:
            write_items = [
                {"address": f"MD{i * 4}", "data_type": "DINT", "value": 1000 + i}
                for i in range(14)
            ]
            w_res = pool.batch_write(host="127.0.0.1", port=port, items=write_items, max_items_per_pdu=16)
            self.assertTrue(w_res["ok"])
            self.assertEqual(w_res["negotiated_pdu"], 240)
            self.assertGreaterEqual(w_res["pdu_count"], 2)

            read_items = [{"address": f"MD{i * 4}", "data_type": "DINT"} for i in range(14)]
            r_res = pool.batch_read(host="127.0.0.1", port=port, items=read_items, max_items_per_pdu=20)
            self.assertTrue(r_res["ok"])
            self.assertEqual(r_res["negotiated_pdu"], 240)
            self.assertEqual([it["value"] for it in r_res["items"]], [1000 + i for i in range(14)])
        finally:
            pool.close_all()
            loopback.stop()

    def test_szl_diagnostics_never_fabricates_order_number(self) -> None:
        plc = VirtualS7PLC(order_number="")
        loopback = EnhancedS7LoopbackServer(plc=plc)
        port = loopback.start()
        pool = S7ConnectionPool()
        try:
            diag = pool.read_szl_diagnostics(host="127.0.0.1", port=port)
            self.assertTrue(diag["ok"])
            self.assertEqual(diag["order_number"], "UNKNOWN")
            self.assertEqual(diag["cpu_mode"], "RUN")
        finally:
            pool.close_all()
            loopback.stop()

    def test_online_block_listing_and_connection_check_over_tcp(self) -> None:
        plc = VirtualS7PLC(order_number="6ES7 315-2EH14-0AB0")
        plc.load_block("OB1", "A I 0.0\n= Q 0.0")
        plc.load_block("FC10", "L MW 10\n+ 1\nT MW 10")
        plc.load_block("FB5", "A M 0.0\n= M 0.1")
        plc.dbs[20] = bytearray(128)
        loopback = EnhancedS7LoopbackServer(plc=plc, max_pdu=240, allowed_connection_types=["OP"])
        port = loopback.start()
        try:
            blks = json.loads(
                srv.s7_runtime_list_online_blocks(host="127.0.0.1", port=port, rack=0, slot=2)
            )
            self.assertTrue(blks["success"])
            self.assertEqual(blks["counts"]["OB"], 1)
            self.assertEqual(blks["counts"]["FC"], 1)
            self.assertEqual(blks["counts"]["FB"], 1)
            self.assertGreaterEqual(blks["counts"]["DB"], 2)
            self.assertIn("OB1", blks["block_names"])
            self.assertIn("FC10", blks["block_names"])
            self.assertIn("FB5", blks["block_names"])
            self.assertIn("DB20", blks["block_names"])

            chk = json.loads(
                srv.s7_plc_connection_check(host="127.0.0.1", port=port, rack=0, slot=2)
            )
            self.assertTrue(chk["success"])
            self.assertTrue(chk["tcp_s7comm"]["connected"])
            self.assertEqual(chk["tcp_s7comm"]["connection_type"], "OP")
            self.assertEqual(chk["tcp_s7comm"]["negotiated_pdu"], 240)
            self.assertEqual(chk["cpu"]["order_number"], "6ES7 315-2EH14-0AB0")
            self.assertEqual(chk["cpu"]["cpu_mode"], "RUN")
            self.assertTrue(chk["capabilities"]["can_monitor_and_gather_data"])
            self.assertIn("FC10", chk["online_blocks"]["block_names"])
        finally:
            srv.runtime_client.pool.invalidate("127.0.0.1", port, 0, 2)
            loopback.stop()

    def test_collect_plc_data_historian_analytics_alerts_and_export(self) -> None:
        plc = VirtualS7PLC()
        plc.write_address("MD 100", 20.0, data_type="REAL")
        plc.write_address("Q 0.0", False, data_type="BOOL")
        loopback = EnhancedS7LoopbackServer(plc=plc)
        port = loopback.start()

        stop_evt = threading.Event()
        started_evt = threading.Event()

        def _mutate_process() -> None:
            step = 0
            while not stop_evt.is_set() and step < 60:
                step += 1
                plc.write_address("MD 100", 20.0 + step * 5.0, data_type="REAL")
                plc.write_address("Q 0.0", (step % 2) == 0, data_type="BOOL")
                if step >= 4:
                    started_evt.set()
                time.sleep(0.015)
            started_evt.set()

        t = threading.Thread(target=_mutate_process, daemon=True)
        t.start()
        started_evt.wait(timeout=2.0)
        exported_paths: list[Path] = []
        try:
            res = json.loads(
                srv.s7_collect_plc_data(
                    tags=[
                        {"symbol": "TEMP_PV", "data_type": "REAL"},
                        {"symbol": "PUMP_RUN", "data_type": "BOOL"},
                    ],
                    symbol_table={"TEMP_PV": "MD100", "PUMP_RUN": "Q0.0"},
                    host="127.0.0.1",
                    port=port,
                    samples=10,
                    interval_ms=20,
                    thresholds={
                        "TEMP_PV": {"max": 35.0},
                        "PUMP_RUN": {"equals": True},
                    },
                    export_format="both",
                    dataset_name="unit test/../../escape_check",
                )
            )
            self.assertTrue(res["success"])
            self.assertEqual(res["sample_count"], 10)
            self.assertEqual(res["transport"], "s7comm_iso_tcp")

            temp_stats = res["per_tag_stats"]["TEMP_PV"]
            self.assertEqual(temp_stats["samples_ok"], 10)
            self.assertGreaterEqual(temp_stats["max"], temp_stats["min"])
            self.assertIn("mean", temp_stats)
            self.assertIn("stddev", temp_stats)

            pump_stats = res["per_tag_stats"]["PUMP_RUN"]
            self.assertEqual(pump_stats["samples_ok"], 10)
            self.assertIn("true_ratio", pump_stats)
            self.assertIn("rising_edges", pump_stats)

            # Threshold alerts should fire once TEMP_PV > 35.0 and PUMP_RUN == True
            self.assertGreater(res["alert_count"], 0)

            # Exported CSV & JSONL must reside safely inside WORKSPACE_DIR/Datasets (no traversal)
            csv_p = Path(res["exported_files"]["csv"])
            jsonl_p = Path(res["exported_files"]["jsonl"])
            exported_paths.extend([csv_p, jsonl_p])
            self.assertTrue(csv_p.is_file())
            self.assertTrue(jsonl_p.is_file())
            self.assertEqual(csv_p.parent.resolve(), (srv.WORKSPACE_DIR / "Datasets").resolve())

            with csv_p.open("r", encoding="utf-8") as fh:
                csv_rows = list(csv.reader(fh))
            self.assertEqual(len(csv_rows), 11)  # 1 header + 10 samples
            self.assertEqual(csv_rows[0][-2:], ["TEMP_PV", "PUMP_RUN"])

            with jsonl_p.open("r", encoding="utf-8") as fh:
                jsonl_rows = [json.loads(line) for line in fh if line.strip()]
            self.assertEqual(len(jsonl_rows), 10)
        finally:
            stop_evt.set()
            t.join(timeout=2.0)
            srv.runtime_client.pool.invalidate("127.0.0.1", port, 0, 2)
            loopback.stop()
            for p in exported_paths:
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass

    def test_com_download_script_hardening_and_post_download_verification(self) -> None:
        # 1. Verify COM PS daemon script fixes physical PLC download bugs:
        #    - S7DoNotOverwrite = 2 (never 0 = S7AskUser modal hang)
        #    - $null = suppression on Download() calls
        #    - Station download support + S7ONLINE PG/PC registry diagnostics
        self.assertIn("function Get-PgPcInterface", _PERSISTENT_PS32_DAEMON_SCRIPT)
        self.assertIn("SINEC\\LogNames\\S7ONLINE", _PERSISTENT_PS32_DAEMON_SCRIPT)
        self.assertIn("if ($null -eq $req.overwrite -or [bool]$req.overwrite) { 1 } else { 2 }", _PERSISTENT_PS32_DAEMON_SCRIPT)
        self.assertIn("$null = $ctx.station.Download($mode)", _PERSISTENT_PS32_DAEMON_SCRIPT)
        self.assertIn("$null = $ctx.blocks.Download($mode)", _PERSISTENT_PS32_DAEMON_SCRIPT)
        self.assertIn("$null = $ctx.program.Download($mode)", _PERSISTENT_PS32_DAEMON_SCRIPT)
        self.assertIn("$null = $hit.Download($mode)", _PERSISTENT_PS32_DAEMON_SCRIPT)

        # 2. Guardrails must still block download when not in FULL mode or S7_WRITES_ENABLED != true
        srv.set_access_mode(AccessMode.READ_WRITE)
        os.environ["S7_WRITES_ENABLED"] = "true"
        blocked_mode = json.loads(srv.s7_com_download_blocks(blocks=["OB1"], confirm_live_write=True))
        self.assertFalse(blocked_mode["success"])
        self.assertEqual(blocked_mode["status"], "ACCESS_DENIED")

        srv.set_access_mode(AccessMode.FULL)
        os.environ["S7_WRITES_ENABLED"] = "false"
        blocked_env = json.loads(srv.s7_com_download_blocks(blocks=["OB1"], confirm_live_write=True))
        self.assertFalse(blocked_env["success"])
        self.assertEqual(blocked_env["status"], "WRITE_GUARDRAIL_BLOCKED")

        # 3. With FULL + S7_WRITES_ENABLED=true + confirm_live_write=True, verify download_target
        #    passes through and post-download S7comm TCP verification confirms online blocks.
        os.environ["S7_WRITES_ENABLED"] = "true"
        plc = VirtualS7PLC(order_number="6ES7 315-2EH14-0AB0")
        plc.load_block("OB1", "A I 0.0\n= Q 0.0")
        loopback = EnhancedS7LoopbackServer(plc=plc)
        port = loopback.start()
        try:
            mock_com_resp = {
                "ok": True,
                "scope": "station",
                "method": "station.Download",
                "project_name": "Plant_Line1",
                "station_name": "SIMATIC 300(1)",
                "overwrite_mode": 1,
                "pgpc_interface": {"detected": True, "device_name": "TCP/IP(Auto)", "is_plcsim": False},
            }
            with patch.object(srv.controller.com_bridge.worker, "call", return_value=mock_com_resp) as m_call:
                out = json.loads(
                    srv.s7_com_download_blocks(
                        blocks=[],
                        project_name="Plant_Line1",
                        download_target="station",
                        verify_host="127.0.0.1",
                        verify_port=port,
                        timeout_s=120.0,
                        confirm_live_write=True,
                    )
                )
            self.assertTrue(out["success"])
            m_call.assert_called_once()
            _, call_args, call_kwargs = m_call.mock_calls[0]
            self.assertEqual(call_args[0], "download_blocks")
            self.assertEqual(call_args[1]["download_target"], "station")
            self.assertEqual(call_kwargs["timeout_s"], 120.0)
            self.assertTrue(out["online_verification"]["ok"])
            self.assertIn("OB1", out["online_verification"]["online_block_names"])
        finally:
            srv.runtime_client.pool.invalidate("127.0.0.1", port, 0, 2)
            loopback.stop()

    def test_new_mcp_tools_registered_with_annotations(self) -> None:
        expected = (
            "s7_plc_connection_check",
            "s7_runtime_list_online_blocks",
            "s7_collect_plc_data",
        )
        if hasattr(srv.mcp, "_tool_manager"):
            tools = {t.name: t.annotations for t in srv.mcp._tool_manager.list_tools()}
            for name in expected:
                self.assertIn(name, tools, f"Missing registered MCP tool: {name}")
                ann = tools[name]
                self.assertIsNotNone(ann, f"Missing annotations on {name}")
                ro = ann.readOnlyHint if hasattr(ann, "readOnlyHint") else ann.get("readOnlyHint")
                self.assertTrue(ro)
        else:
            tools_map = getattr(srv.mcp, "_tools", {})
            for name in expected:
                self.assertIn(name, tools_map, f"Missing registered MCP tool: {name}")
                ann = tools_map[name].get("annotations")
                self.assertIsNotNone(ann, f"Missing annotations on {name}")
                ro = ann.readOnlyHint if hasattr(ann, "readOnlyHint") else ann.get("readOnlyHint")
                self.assertTrue(ro)

    def test_plc_data_collector_sanitize_name(self) -> None:
        self.assertEqual(PLCDataCollector._sanitize_name("../../etc/passwd"), "etc_passwd")
        self.assertEqual(PLCDataCollector._sanitize_name("   "), "plc_dataset")


if __name__ == "__main__":
    unittest.main()
