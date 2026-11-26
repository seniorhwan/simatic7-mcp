"""
Comprehensive end-to-end tests for direct physical PLC S7comm ISO-on-TCP program
deployment (0x1A/0x1B/0x1C + _INSE + _MODU), block upload/backup (0x1D/0x1E/0x1F),
block deletion (_DELE), Siemens MC7 container parsing/building, Global DB MC7
compilation, STEP 7 Classic SUBBLK.DBF/SUBBLK.DBT extraction, and safety guardrails.
"""

from __future__ import annotations

import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Tuple

import simatic_s7_mcp_server as srv
from simatic_s7_mcp import (
    MC7_OP_BE,
    EnhancedS7LoopbackServer,
    VirtualS7PLC,
    build_db_mc7_block,
    build_download_block_ack_pdu,
    build_download_ended_ack_pdu,
    build_end_upload_pdu,
    build_mc7_block,
    build_pi_copy_ram_to_rom_pdu,
    build_pi_delete_block_pdu,
    build_pi_insert_block_pdu,
    build_request_download_pdu,
    build_start_upload_pdu,
    build_upload_block_pdu,
    extract_mc7_blocks_from_step7_project,
    parse_mc7_block,
    parse_s7_frame,
)


def _create_synthetic_step7_project(
    root_dir: Path,
    block_entries: List[Tuple[int, int, int, bytes, str, str]],
) -> Path:
    """
    Create a genuine dBase III+ STEP 7 Classic offline database at
    ``<root_dir>/ombstx/offline/00000001/SUBBLK.DBF`` + ``SUBBLK.DBT``
    where each entry is ``(subblk_typ, blk_number, blk_lang, mc5_bytes, author, name)``.
    """
    off_dir = root_dir / "ombstx" / "offline" / "00000001"
    off_dir.mkdir(parents=True, exist_ok=True)
    (root_dir / "TestPlant.s7p").write_bytes(b"STEP7_PROJECT")

    # Build 512-byte-blocked SUBBLK.DBT memo file
    dbt = bytearray(512)
    next_blk = 1
    memo_ptrs: List[int] = []
    for _, _, _, mc5_bytes, _, _ in block_entries:
        memo_ptrs.append(next_blk)
        padded_len = ((len(mc5_bytes) + 511) // 512) * 512
        padded_len = max(512, padded_len)
        blk_buf = bytearray(padded_len)
        blk_buf[:len(mc5_bytes)] = mc5_bytes
        dbt.extend(blk_buf)
        next_blk += padded_len // 512
    struct.pack_into("<I", dbt, 0, next_blk)
    (off_dir / "SUBBLK.DBT").write_bytes(bytes(dbt))

    # Build dBase III+ SUBBLK.DBF table
    cols = [
        ("SUBBLKTYP", "N", 5),
        ("BLKNUMBER", "N", 6),
        ("BLKLANG", "N", 4),
        ("BLKFLAGS", "N", 4),
        ("MC5LEN", "N", 8),
        ("SSBLEN", "N", 6),
        ("ADDLEN", "N", 6),
        ("LOCDATLEN", "N", 6),
        ("VERSION", "N", 4),
        ("CHECKSUM", "N", 6),
        ("USERNAME", "C", 8),
        ("FAMILY", "C", 8),
        ("BLOCKNAME", "C", 8),
        ("MC5CODE", "M", 10),
    ]
    num_records = len(block_entries)
    header_len = 32 + len(cols) * 32 + 1
    record_len = 1 + sum(c[2] for c in cols)

    dbf = bytearray(32)
    dbf[0] = 0x83  # dBase III+ with memo file
    struct.pack_into("<IHH", dbf, 4, num_records, header_len, record_len)
    for cname, ctype, clen in cols:
        desc = bytearray(32)
        desc[:len(cname)] = cname.encode("ascii")
        desc[11] = ord(ctype)
        desc[16] = clen
        dbf.extend(desc)
    dbf.append(0x0D)  # header terminator

    for idx, (styp, bnum, blang, mc5_bytes, author, bname) in enumerate(block_entries):
        rec = bytearray(b" ")  # active record flag
        vals: Dict[str, str] = {
            "SUBBLKTYP": str(styp),
            "BLKNUMBER": str(bnum),
            "BLKLANG": str(blang),
            "BLKFLAGS": "1",
            "MC5LEN": str(len(mc5_bytes)),
            "SSBLEN": "0",
            "ADDLEN": "0",
            "LOCDATLEN": "20" if styp == 8 else "0",
            "VERSION": "17",  # 0x11 -> v1.1
            "CHECKSUM": "0",
            "USERNAME": author,
            "FAMILY": "PLANT",
            "BLOCKNAME": bname,
            "MC5CODE": str(memo_ptrs[idx]),
        }
        for cname, ctype, clen in cols:
            s = vals[cname]
            if ctype in ("N", "M"):
                raw_field = s.rjust(clen, " ")[:clen].encode("ascii")
            else:
                raw_field = s.ljust(clen, " ")[:clen].encode("ascii")
            rec.extend(raw_field)
        assert len(rec) == record_len
        dbf.extend(rec)
    dbf.append(0x1A)  # EOF marker
    (off_dir / "SUBBLK.DBF").write_bytes(bytes(dbf))
    return root_dir


class TestMC7BinaryAndWireCodecs(unittest.TestCase):
    def test_mc7_container_build_and_parse_roundtrip(self) -> None:
        body = b"\x00\x00" * 16 + MC7_OP_BE
        raw = build_mc7_block(
            block_type="OB",
            block_number=1,
            mc7_body=body,
            language="STL",
            author="SIEMENS",
            family="MAIN",
            symbolic_name="CYCL_EXC",
            version="2.3",
        )
        self.assertEqual(raw[0:2], b"\x70\x70")
        parsed = parse_mc7_block(raw)
        self.assertTrue(parsed["valid"])
        self.assertEqual(parsed["block_type"], "OB")
        self.assertEqual(parsed["block_number"], 1)
        self.assertEqual(parsed["block_name"], "OB1")
        self.assertEqual(parsed["language"], "STL")
        self.assertEqual(parsed["local_data_length"], 20)
        self.assertEqual(parsed["mc7_size"], len(body))
        self.assertEqual(parsed["load_size"], len(raw))
        self.assertEqual(parsed["author"], "SIEMENS")
        self.assertEqual(parsed["family"], "MAIN")
        self.assertEqual(parsed["symbolic_name"], "CYCL_EXC")
        self.assertEqual(parsed["version"], "2.3")
        self.assertEqual(bytes.fromhex(parsed["mc7_body_hex"]), body)

    def test_build_db_mc7_block_from_fields_and_source(self) -> None:
        raw_db, meta = build_db_mc7_block(
            db_number=42,
            fields=[
                {"name": "Enable", "type": "BOOL", "initial": True},
                {"name": "Alarm", "type": "BOOL", "initial": False},
                {"name": "SpeedRpm", "type": "INT", "initial": 1450},
                {"name": "PressureBar", "type": "REAL", "initial": 6.5},
            ],
        )
        self.assertEqual(meta["block_name"], "DB42")
        self.assertEqual(meta["language"], "DB")
        body = bytes.fromhex(meta["mc7_body_hex"])
        # Byte 0 bit 0 = Enable (True), Byte 2..3 = INT 1450 (0x05AA), Byte 4..7 = REAL 6.5
        self.assertEqual(body[0] & 0x01, 1)
        self.assertEqual(struct.unpack(">h", body[2:4])[0], 1450)
        self.assertAlmostEqual(struct.unpack(">f", body[4:8])[0], 6.5, places=4)

        db_src = """
DATA_BLOCK DB 77
STRUCT
  Running : BOOL := TRUE;
  Setpoint : INT := 320;
END_STRUCT;
BEGIN
  Setpoint := 512;
END_DATA_BLOCK
"""
        raw_src_db, meta2 = build_db_mc7_block(db_number=0, db_source=db_src)
        self.assertEqual(meta2["block_name"], "DB77")
        body2 = bytes.fromhex(meta2["mc7_body_hex"])
        self.assertEqual(body2[0] & 0x01, 1)
        self.assertEqual(struct.unpack(">h", body2[2:4])[0], 512)
        self.assertEqual(parse_mc7_block(raw_src_db)["block_number"], 77)

    def test_s7comm_block_transfer_wire_pdus(self) -> None:
        req_dl = build_request_download_pdu("OB", 1, load_size=108, mc7_size=36, pdu_ref=7)
        ros, ref, err, param, data = parse_s7_frame(req_dl)
        self.assertEqual((ros, ref, err), (0x01, 7, 0))
        self.assertEqual(param[0], 0x1A)
        self.assertEqual(param[9:18], b"_0800001P")
        self.assertEqual(param[19:32], b"1000108000036")
        self.assertEqual(data, b"")

        ack_1b = build_download_block_ack_pdu(b"\xAA\xBB\xCC\xDD", more_data=True, pdu_ref=99)
        ros, ref, err, param, data = parse_s7_frame(ack_1b)
        self.assertEqual((ros, ref, err), (0x03, 99, 0))
        self.assertEqual(param, b"\x1b\x01")
        self.assertEqual(data, b"\x00\x04\x00\xfb\xaa\xbb\xcc\xdd")

        ack_1c = build_download_ended_ack_pdu(pdu_ref=100)
        ros, ref, err, param, data = parse_s7_frame(ack_1c)
        self.assertEqual((ros, ref, err, param), (0x03, 100, 0, b"\x1c"))

        pi_inse = build_pi_insert_block_pdu("FC", 10, pdu_ref=11)
        ros, ref, err, param, _ = parse_s7_frame(pi_inse)
        self.assertEqual((ros, ref, err), (0x01, 11, 0))
        self.assertEqual(param[0], 0x28)
        self.assertEqual(param[7], 0xFD)
        self.assertEqual(param[8:10], b"\x00\x0a")
        self.assertEqual(param[10:20], b"\x01\x000C00010P")
        self.assertEqual(param[20:], b"\x05_INSE")

        pi_dele = build_pi_delete_block_pdu("DB", 5, pdu_ref=12)
        _, _, _, param_d, _ = parse_s7_frame(pi_dele)
        self.assertEqual(param_d[10:20], b"\x01\x000A00005B")
        self.assertEqual(param_d[20:], b"\x05_DELE")

        pi_modu = build_pi_copy_ram_to_rom_pdu(pdu_ref=13)
        _, _, _, param_m, _ = parse_s7_frame(pi_modu)
        self.assertEqual(param_m[8:], b"\x00\x02EP\x05_MODU")

        req_up = build_start_upload_pdu("FB", 2, pdu_ref=14)
        _, _, _, param_u, _ = parse_s7_frame(req_up)
        self.assertEqual(param_u[0], 0x1D)
        self.assertEqual(param_u[9:18], b"_0E00002A")

        req_up_next = build_upload_block_pdu(b"\x00\x00\x00\x05", pdu_ref=15)
        _, _, _, param_un, _ = parse_s7_frame(req_up_next)
        self.assertEqual(param_un, b"\x1e\x00\x00\x00\x00\x00\x00\x05")

        req_up_end = build_end_upload_pdu(b"\x00\x00\x00\x05", pdu_ref=16)
        _, _, _, param_ue, _ = parse_s7_frame(req_up_end)
        self.assertEqual(param_ue, b"\x1f\x00\x00\x00\x00\x00\x00\x05")

    def test_extract_mc7_blocks_from_step7_offline_dbf_dbt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = _create_synthetic_step7_project(
                Path(tmp) / "MyPlant",
                [
                    (8, 1, 1, b"\x00\x00" * 120 + MC7_OP_BE, "ENG1", "MAIN_OB"),
                    (12, 10, 2, b"\x00\x00" * 40 + MC7_OP_BE, "ENG1", "CTRL_FC"),
                    (10, 20, 5, struct.pack(">hf", 1234, 42.5), "ENG1", "PROC_DB"),
                ],
            )
            res = extract_mc7_blocks_from_step7_project(proj)
            self.assertTrue(res["ok"])
            self.assertEqual(res["block_count"], 3)
            self.assertEqual(sorted(res["block_names"]), ["DB20", "FC10", "OB1"])
            self.assertEqual(res["metadata"]["OB1"]["local_data_length"], 20)
            self.assertEqual(res["metadata"]["FC10"]["language"], "LAD")
            self.assertEqual(res["metadata"]["DB20"]["mc7_size"], 6)

            # Filtered extraction
            sub = extract_mc7_blocks_from_step7_project(proj, block_names=["FC10"])
            self.assertTrue(sub["ok"])
            self.assertEqual(sub["block_names"], ["FC10"])


class TestPhysicalPLCTCPTransferEndToEnd(unittest.TestCase):
    """Real TCP socket end-to-end tests against a strict 240-byte PDU S7-300 loopback PLC."""

    def setUp(self) -> None:
        self._prev_mode = srv.ACTIVE_ACCESS_MODE
        self._prev_writes = os.environ.get("S7_WRITES_ENABLED")
        self.plc = VirtualS7PLC(order_number="6ES7 315-2EH14-0AB0")
        # Seed an initial OB1 in the PLC so we can verify automatic pre-overwrite backup!
        self.initial_ob1 = build_mc7_block("OB", 1, b"\x00\x00" * 8 + MC7_OP_BE, author="ORIG")
        self.plc.store_mc7_block("OB1", self.initial_ob1)
        # Use strict 240-byte S7-300 PDU to force real multi-chunk 0x1B downloads AND 0x1E uploads!
        self.server = EnhancedS7LoopbackServer(host="127.0.0.1", port=0, plc=self.plc, max_pdu=240)
        self.port = self.server.start()

    def tearDown(self) -> None:
        srv.runtime_client.pool.close_all()
        self.server.stop()
        srv.set_access_mode(self._prev_mode.value)
        if self._prev_writes is None:
            os.environ.pop("S7_WRITES_ENABLED", None)
        else:
            os.environ["S7_WRITES_ENABLED"] = self._prev_writes

    def test_guardrails_block_unauthorized_deployments_and_allow_read_only_upload(self) -> None:
        # 1. Read-only upload MUST succeed even in READ_ONLY mode with S7_WRITES_ENABLED=false
        srv.set_access_mode("read-only")
        os.environ["S7_WRITES_ENABLED"] = "false"
        up = json.loads(
            srv.s7_plc_upload_block(block_name="OB1", host="127.0.0.1", port=self.port, save_to_backup=False, include_hex=True)
        )
        self.assertTrue(up["success"])
        self.assertEqual(bytes.fromhex(up["mc7_hex"]), self.initial_ob1)

        # 2. Deploy blocked in READ_ONLY
        res_ro = json.loads(
            srv.s7_plc_deploy_program(
                host="127.0.0.1", port=self.port, db_number=50, db_byte_size=16, confirm_live_write=True
            )
        )
        self.assertFalse(res_ro["success"])
        self.assertEqual(res_ro["failure_category"], "access_denied")

        # 3. Deploy blocked in READ_WRITE
        srv.set_access_mode("read-write")
        res_rw = json.loads(
            srv.s7_plc_deploy_program(
                host="127.0.0.1", port=self.port, db_number=50, db_byte_size=16, confirm_live_write=True
            )
        )
        self.assertFalse(res_rw["success"])
        self.assertEqual(res_rw["failure_category"], "access_denied")

        # 4. Deploy to physical PLC blocked in FULL without S7_WRITES_ENABLED=true
        srv.set_access_mode("full")
        os.environ["S7_WRITES_ENABLED"] = "false"
        res_env = json.loads(
            srv.s7_plc_deploy_program(
                host="192.168.0.10", port=102, db_number=50, db_byte_size=16, confirm_live_write=True
            )
        )
        self.assertFalse(res_env["success"])
        self.assertEqual(res_env["status"], "WRITE_GUARDRAIL_BLOCKED")

        # 5. Deploy blocked in FULL with S7_WRITES_ENABLED=true but confirm_live_write=False
        os.environ["S7_WRITES_ENABLED"] = "true"
        res_cf = json.loads(
            srv.s7_plc_deploy_program(
                host="127.0.0.1", port=self.port, db_number=50, db_byte_size=16, confirm_live_write=False
            )
        )
        self.assertFalse(res_cf["success"])
        self.assertEqual(res_cf["status"], "WRITE_GUARDRAIL_BLOCKED")

    def test_multi_chunk_step7_project_deploy_backup_upload_delete_and_ram_to_rom(self) -> None:
        srv.set_access_mode("full")
        os.environ["S7_WRITES_ENABLED"] = "true"

        # Verify preflight connection_check reports direct TCP deploy & upload readiness
        chk = json.loads(srv.s7_plc_connection_check(host="127.0.0.1", port=self.port))
        self.assertTrue(chk["success"])
        self.assertTrue(chk["capabilities"]["can_upload_blocks_via_tcp"])
        self.assertTrue(chk["capabilities"]["can_deploy_program_via_tcp"])

        with tempfile.TemporaryDirectory() as tmp:
            # Large OB1 (>600B) + FC10 (>350B) + DB25 to force multi-chunk 0x1B/0x1E over 240B PDU!
            large_ob1_body = (b"\x00\x00" * 300) + MC7_OP_BE  # 602B -> >3 chunks on 240B PDU!
            fc10_body = (b"\x00\x00" * 160) + MC7_OP_BE       # 322B -> 2 chunks
            db25_body = struct.pack(">hfi", 777, 12.25, 99999) + bytes(250)  # 260B -> 2 chunks

            proj = _create_synthetic_step7_project(
                Path(tmp) / "PhysicalPlant",
                [
                    (8, 1, 1, large_ob1_body, "ENG", "OB1_NEW"),
                    (12, 10, 2, fc10_body, "ENG", "MOTOR_FC"),
                    (10, 25, 5, db25_body, "ENG", "MOTOR_DB"),
                ],
            )

            # Deploy full STEP 7 project directly over S7comm TCP port 102!
            dep = json.loads(
                srv.s7_plc_deploy_program(
                    host="127.0.0.1",
                    port=self.port,
                    project_path=str(proj),
                    backup_before_overwrite=True,
                    stop_cpu_for_download=True,
                    copy_ram_to_rom=True,
                    verify_online=True,
                    confirm_live_write=True,
                )
            )
            self.assertTrue(dep["success"], dep)
            self.assertEqual(dep["transport"], "s7comm_iso_tcp")
            self.assertEqual(dep["deployed_count"], 3)
            # Dependency ordering: DB25 -> FC10 -> OB1!
            self.assertEqual([b["block_name"] for b in dep["deployed_blocks"]], ["DB25", "FC10", "OB1"])
            # Multi-chunk verification on 240-byte S7-300 PDU
            ob1_dep = next(b for b in dep["deployed_blocks"] if b["block_name"] == "OB1")
            self.assertGreaterEqual(ob1_dep["chunks_sent"], 3)
            self.assertTrue(ob1_dep["activated"])
            # Pre-overwrite backup captured the ORIGINAL OB1 before overwriting!
            self.assertTrue(dep["pre_overwrite_backups"]["OB1"]["backed_up"])
            orig_backup_file = Path(dep["pre_overwrite_backups"]["OB1"]["backup_file"])
            self.assertTrue(orig_backup_file.is_file())
            self.assertEqual(orig_backup_file.read_bytes(), self.initial_ob1)
            # CPU stopped, restarted, ROM copied, and verified online!
            self.assertTrue(dep["cpu_stopped_for_download"])
            self.assertTrue(dep["cpu_restarted"])
            self.assertTrue(dep["copy_ram_to_rom"]["ok"])
            self.assertTrue(dep["online_verification"]["ok"])
            self.assertEqual(dep["online_verification"]["cpu_mode"], "RUN")
            self.assertIn("OB1", dep["online_verification"]["verified_blocks"])
            self.assertIn("FC10", dep["online_verification"]["verified_blocks"])
            self.assertIn("DB25", dep["online_verification"]["verified_blocks"])
            # ROM snapshot in PLC persisted all 3 blocks
            self.assertIn("OB1", self.plc.rom_snapshot)
            self.assertIn("DB25", self.plc.rom_snapshot)

            # Live S7comm Read Var from newly deployed DB25 immediately returns the exact values!
            r_int = json.loads(srv.s7_runtime_read(address="DB25.DBW0", data_type="INT", host="127.0.0.1", port=self.port))
            r_real = json.loads(srv.s7_runtime_read(address="DB25.DBD2", data_type="REAL", host="127.0.0.1", port=self.port))
            self.assertEqual(r_int["value"], 777)
            self.assertAlmostEqual(r_real["value"], 12.25, places=4)

            # Multi-chunk Upload (0x1D -> 0x1E -> 0x1F) of OB1 verifies byte-for-byte equality!
            out_mc7 = Path(tmp) / "uploaded_ob1.mc7"
            up = json.loads(
                srv.s7_plc_upload_block(
                    block_name="OB1",
                    host="127.0.0.1",
                    port=self.port,
                    output_path=str(out_mc7),
                    include_hex=True,
                )
            )
            self.assertTrue(up["success"])
            self.assertGreaterEqual(up["chunks_received"], 3)
            self.assertEqual(out_mc7.read_bytes(), self.plc.mc7_blocks["OB1"])
            self.assertEqual(up["parsed_header"]["author"], "ENG")
            self.assertEqual(up["parsed_header"]["mc7_size"], len(large_ob1_body))

            # Delete FC10 (with automatic backup!) and verify removal from online blocks
            del_res = json.loads(
                srv.s7_plc_delete_block(
                    block_name="FC10",
                    host="127.0.0.1",
                    port=self.port,
                    backup_before_delete=True,
                    confirm_live_write=True,
                )
            )
            self.assertTrue(del_res["success"])
            self.assertTrue(Path(del_res["backup"]["backup_file"]).is_file())
            inv = json.loads(srv.s7_runtime_list_online_blocks(host="127.0.0.1", port=self.port))
            self.assertNotIn("FC10", inv["block_names"])

            # Restore FC10 from the .mc7 backup file directly via s7_plc_deploy_program!
            restore = json.loads(
                srv.s7_plc_deploy_program(
                    host="127.0.0.1",
                    port=self.port,
                    mc7_file_path=del_res["backup"]["backup_file"],
                    confirm_live_write=True,
                )
            )
            self.assertTrue(restore["success"])
            inv2 = json.loads(srv.s7_runtime_list_online_blocks(host="127.0.0.1", port=self.port))
            self.assertIn("FC10", inv2["block_names"])

            # Standalone Copy RAM to ROM tool
            rom_only = json.loads(
                srv.s7_plc_copy_ram_to_rom(host="127.0.0.1", port=self.port, confirm_live_write=True)
            )
            self.assertTrue(rom_only["success"])

            # Clean up temporary backup files created inside WORKSPACE_DIR/Backups
            for p_str in [
                dep["pre_overwrite_backups"]["OB1"].get("backup_file"),
                del_res["backup"].get("backup_file"),
            ]:
                if p_str and Path(p_str).exists():
                    Path(p_str).unlink()

    def test_pure_python_global_db_deploy_and_com_tcp_fallback(self) -> None:
        srv.set_access_mode("full")
        os.environ["S7_WRITES_ENABLED"] = "true"

        # 1. Pure-Python Global DB compilation & direct TCP deploy (no STEP 7 needed!)
        dep_db = json.loads(
            srv.s7_plc_deploy_program(
                host="127.0.0.1",
                port=self.port,
                db_number=88,
                db_fields=[
                    {"name": "AutoMode", "type": "BOOL", "initial": True},
                    {"name": "FlowSetpoint", "type": "REAL", "initial": 84.5},
                ],
                backup_before_overwrite=False,
                confirm_live_write=True,
            )
        )
        self.assertTrue(dep_db["success"], dep_db)
        self.assertEqual(dep_db["source_origin"], "pure_python_db_mc7")
        r_flow = json.loads(srv.s7_runtime_read(address="DB88.DBD2", data_type="REAL", host="127.0.0.1", port=self.port))
        self.assertAlmostEqual(r_flow["value"], 84.5, places=4)

        # 2. s7_com_download_blocks automatic TCP fallback when COM is unavailable on Linux/macOS
        with tempfile.TemporaryDirectory() as tmp:
            proj = _create_synthetic_step7_project(
                Path(tmp) / "FallbackProj",
                [(12, 33, 1, b"\x00\x00" * 10 + MC7_OP_BE, "CI", "FBK_FC")],
            )
            fbk = json.loads(
                srv.s7_com_download_blocks(
                    blocks=["FC33"],
                    host="127.0.0.1",
                    port=self.port,
                    project_path=str(proj),
                    fallback_to_tcp=True,
                    confirm_live_write=True,
                )
            )
            self.assertTrue(fbk["success"], fbk)
            self.assertEqual(fbk["tcp_fallback"]["transport"], "s7comm_iso_tcp_fallback")
            self.assertIn("FC33", fbk["online_verification"]["online_block_names"])

    def test_raw_stl_source_without_step7_compiler_refuses_unsafe_bytecode(self) -> None:
        srv.set_access_mode("full")
        os.environ["S7_WRITES_ENABLED"] = "true"
        res = json.loads(
            srv.s7_plc_deploy_program(
                host="127.0.0.1",
                port=self.port,
                block_name="FC5",
                source_code="FUNCTION FC 5 : VOID\nBEGIN\nNETWORK\n A I 0.0;\n = Q 0.0;\nEND_FUNCTION\n",
                compile_via_step7=False,
                confirm_live_write=True,
            )
        )
        self.assertFalse(res["success"])
        self.assertEqual(res["failure_category"], "compile_failed")

    def test_new_tools_registered_with_annotations(self) -> None:
        expected = {
            "s7_plc_diagnose_connectivity",
            "s7_plc_deploy_program",
            "s7_plc_upload_block",
            "s7_plc_delete_block",
            "s7_plc_copy_ram_to_rom",
        }
        tool_mgr = getattr(srv.mcp, "_tool_manager", None)
        if tool_mgr is not None and hasattr(tool_mgr, "_tools"):
            tools = tool_mgr._tools
            for name in expected:
                self.assertIn(name, tools)
                ann = getattr(tools[name], "annotations", None)
                self.assertIsNotNone(ann, f"Missing ToolAnnotations on {name}")
        else:
            for name in expected:
                self.assertTrue(callable(getattr(srv, name, None)))


class TestPreUploadConnectivityDiagnostics(unittest.TestCase):
    """Real TCP socket tests for s7_plc_diagnose_connectivity and s7_plc_deploy_program preflight gate."""

    def setUp(self) -> None:
        self._prev_mode = srv.ACTIVE_ACCESS_MODE
        self._prev_writes = os.environ.get("S7_WRITES_ENABLED")
        self.plc = VirtualS7PLC(order_number="6ES7 315-2EH14-0AB0")
        self.plc.store_mc7_block("OB1", build_mc7_block("OB", 1, b"\x00\x00" * 16 + MC7_OP_BE, author="DIAG"))
        self.server = EnhancedS7LoopbackServer(host="127.0.0.1", port=0, plc=self.plc, max_pdu=240)
        self.port = self.server.start()

    def tearDown(self) -> None:
        srv.runtime_client.pool.close_all()
        self.server.stop()
        srv.set_access_mode(self._prev_mode.value)
        if self._prev_writes is None:
            os.environ.pop("S7_WRITES_ENABLED", None)
        else:
            os.environ["S7_WRITES_ENABLED"] = self._prev_writes

    def test_healthy_s7_300_plc_returns_ready_and_all_7_stages_pass(self) -> None:
        srv.set_access_mode("full")
        os.environ["S7_WRITES_ENABLED"] = "true"

        res = json.loads(
            srv.s7_plc_diagnose_connectivity(
                host="127.0.0.1",
                port=self.port,
                rack=0,
                slot=2,
                probe_count=3,
                verify_upload_channel=True,
            )
        )
        self.assertTrue(res["success"], res)
        self.assertTrue(res["read_ready"])
        self.assertTrue(res["upload_ready"])
        self.assertEqual(res["readiness_verdict"], "READY")
        self.assertTrue(res["network_reachable"])
        self.assertTrue(res["s7comm_healthy"])
        self.assertEqual(res["selected_connection_type"], "PG")
        self.assertEqual(res["negotiated_pdu"], 240)
        self.assertEqual(res["max_block_chunk_bytes"], 216)
        self.assertEqual(len(res["stages"]), 7)
        stage_map = {st["stage"]: st for st in res["stages"]}
        self.assertEqual(
            set(stage_map.keys()),
            {
                "dns_resolution",
                "tcp_reachability",
                "cotp_tsap_probe",
                "s7_setup_comm",
                "cpu_health_szl",
                "block_transfer_readiness",
                "deployment_guardrails",
            },
        )
        for st in res["stages"]:
            self.assertEqual(st["status"], "PASS", st)
        self.assertEqual(stage_map["tcp_reachability"]["details"]["probes_succeeded"], 3)
        self.assertEqual(stage_map["tcp_reachability"]["details"]["packet_loss_pct"], 0.0)
        self.assertTrue(stage_map["block_transfer_readiness"]["details"]["upload_channel_probe"]["ok"])
        self.assertEqual(stage_map["block_transfer_readiness"]["details"]["upload_channel_probe"]["probed_block"], "OB1")

        # Target blocks overwrite analysis detects OB1 overwrite + FC99 creation
        res_tgt = json.loads(
            srv.s7_plc_diagnose_connectivity(
                host="127.0.0.1",
                port=self.port,
                probe_count=1,
                target_blocks=["OB1", "FC99"],
                verify_upload_channel=False,
            )
        )
        self.assertTrue(res_tgt["upload_ready"])
        impact = res_tgt["online_blocks"]["target_block_impact"]
        self.assertEqual(impact["will_overwrite_existing"], ["OB1"])
        self.assertEqual(impact["will_create_new"], ["FC99"])
        self.assertEqual(res_tgt["readiness_verdict"], "DEGRADED")
        self.assertTrue(any("OB1" in w for w in res_tgt["warnings"]))

    def test_occupied_pg_resource_falls_back_to_op_and_warns_degraded(self) -> None:
        srv.set_access_mode("full")
        os.environ["S7_WRITES_ENABLED"] = "true"
        op_server = EnhancedS7LoopbackServer(
            host="127.0.0.1",
            port=0,
            plc=self.plc,
            max_pdu=240,
            allowed_connection_types=["OP", "S7_BASIC"],
        )
        op_port = op_server.start()
        try:
            res = json.loads(
                srv.s7_plc_diagnose_connectivity(
                    host="127.0.0.1",
                    port=op_port,
                    rack=0,
                    slot=2,
                    probe_count=2,
                    connection_type="AUTO",
                )
            )
            self.assertTrue(res["success"], res)
            self.assertTrue(res["read_ready"])
            self.assertTrue(res["upload_ready"])
            self.assertEqual(res["readiness_verdict"], "DEGRADED")
            self.assertEqual(res["selected_connection_type"], "OP")
            cotp_stage = next(st for st in res["stages"] if st["stage"] == "cotp_tsap_probe")
            self.assertEqual(cotp_stage["status"], "WARN")
            self.assertEqual(
                cotp_stage["details"]["tsap_availability"],
                {"PG": False, "OP": True, "S7_BASIC": True},
            )
        finally:
            srv.runtime_client.pool.close_all()
            op_server.stop()

    def test_wrong_rack_slot_auto_discovers_s7_400_slot_3(self) -> None:
        srv.set_access_mode("full")
        os.environ["S7_WRITES_ENABLED"] = "true"
        s7_400_plc = VirtualS7PLC(order_number="6ES7 416-3XR05-0AB0")
        s7_400_server = EnhancedS7LoopbackServer(
            host="127.0.0.1",
            port=0,
            plc=s7_400_plc,
            max_pdu=480,
            allowed_rack_slots=[(0, 3)],
        )
        s400_port = s7_400_server.start()
        try:
            # Request S7-300 default slot=2 against an S7-400 CPU sitting on rack=0, slot=3
            res = json.loads(
                srv.s7_plc_diagnose_connectivity(
                    host="127.0.0.1",
                    port=s400_port,
                    rack=0,
                    slot=2,
                    probe_count=1,
                    auto_probe_slots=True,
                )
            )
            self.assertFalse(res["success"])
            self.assertFalse(res["read_ready"])
            self.assertFalse(res["upload_ready"])
            self.assertEqual(res["readiness_verdict"], "BLOCKED")
            self.assertTrue(res["network_reachable"])
            self.assertFalse(res["s7comm_healthy"])
            self.assertIsNotNone(res["discovered_rack_slot"])
            self.assertEqual(res["discovered_rack_slot"]["rack"], 0)
            self.assertEqual(res["discovered_rack_slot"]["slot"], 3)
            self.assertTrue(any("slot=3" in b for b in res["blocking_issues"]))
        finally:
            srv.runtime_client.pool.close_all()
            s7_400_server.stop()

    def test_unreachable_tcp_port_and_deploy_preflight_fail_fast(self) -> None:
        import socket as _sock

        # Allocate and immediately close an ephemeral TCP port so connection is reliably refused
        tmp_sock = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        tmp_sock.bind(("127.0.0.1", 0))
        closed_port = tmp_sock.getsockname()[1]
        tmp_sock.close()

        srv.set_access_mode("full")
        os.environ["S7_WRITES_ENABLED"] = "true"

        diag = json.loads(
            srv.s7_plc_diagnose_connectivity(
                host="127.0.0.1",
                port=closed_port,
                probe_count=2,
                timeout_s=0.5,
            )
        )
        self.assertFalse(diag["success"])
        self.assertFalse(diag["network_reachable"])
        self.assertFalse(diag["upload_ready"])
        self.assertEqual(diag["readiness_verdict"], "BLOCKED")
        tcp_stage = next(st for st in diag["stages"] if st["stage"] == "tcp_reachability")
        self.assertEqual(tcp_stage["status"], "FAIL")
        self.assertEqual(tcp_stage["details"]["packet_loss_pct"], 100.0)

        # s7_plc_deploy_program with preflight_check=True fails fast with structured preflight_diagnostics
        dep = json.loads(
            srv.s7_plc_deploy_program(
                host="127.0.0.1",
                port=closed_port,
                db_number=91,
                db_byte_size=16,
                preflight_check=True,
                timeout_s=1.0,
                confirm_live_write=True,
            )
        )
        self.assertFalse(dep["success"])
        self.assertEqual(dep["status"], "PREFLIGHT_DIAGNOSTIC_FAILED")
        self.assertEqual(dep["failure_category"], "transport_error")
        self.assertIn("preflight_diagnostics", dep)
        self.assertFalse(dep["preflight_diagnostics"]["network_reachable"])

    def test_read_only_mode_diagnostic_succeeds_but_flags_upload_blocked_and_virtual_ready(self) -> None:
        srv.set_access_mode("read-only")
        os.environ["S7_WRITES_ENABLED"] = "false"

        res = json.loads(
            srv.s7_plc_diagnose_connectivity(
                host="127.0.0.1",
                port=self.port,
                probe_count=1,
            )
        )
        self.assertTrue(res["success"])
        self.assertTrue(res["read_ready"])
        self.assertFalse(res["upload_ready"])
        self.assertEqual(res["readiness_verdict"], "BLOCKED")
        guard_stage = next(st for st in res["stages"] if st["stage"] == "deployment_guardrails")
        self.assertEqual(guard_stage["status"], "FAIL")
        self.assertTrue(any("--mode full" in b for b in res["blocking_issues"]))
        self.assertTrue(any("S7_WRITES_ENABLED" in b for b in res["blocking_issues"]))

        # Virtual PLC always reports READY
        v_res = json.loads(srv.s7_plc_diagnose_connectivity(host="virtual"))
        self.assertTrue(v_res["success"])
        self.assertTrue(v_res["upload_ready"])
        self.assertEqual(v_res["readiness_verdict"], "READY")
        self.assertEqual(len(v_res["stages"]), 7)

        # Read-only mode blocks disk-writing project/symbol/comment tools
        self.assertEqual(
            json.loads(srv.s7_create_project(project_name="BlockedProj"))["failure_category"],
            "access_denied",
        )
        self.assertEqual(
            json.loads(srv.s7_write_symbol_table(symbols={"START": "I 0.0"}))["failure_category"],
            "access_denied",
        )
        self.assertEqual(
            json.loads(srv.s7_write_device_comments(device_comments={"I 0.0": "Start"}))["failure_category"],
            "access_denied",
        )

    def test_db_hex_byte_and_uninitialized_string_header_and_invalid_target_blocks(self) -> None:
        # 1. BYTE hex literal (B#16#A5) and uninitialized STRING[16] preserve exact S7 DB layout
        db_src = """
DATA_BLOCK DB 99
STRUCT
  MaskByte : BYTE := B#16#A5;
  Label : STRING[16];
END_STRUCT;
BEGIN
END_DATA_BLOCK
"""
        _, meta = build_db_mc7_block(db_number=0, db_source=db_src)
        body = bytes.fromhex(meta["mc7_body_hex"])
        self.assertEqual(body[0], 0xA5)
        # STRING[16] aligns to even byte offset 2, byte 2 = max_len (16), byte 3 = actual_len (0)
        self.assertEqual(body[2], 16)
        self.assertEqual(body[3], 0)

        # 2. Invalid target_blocks entry does not falsely fail S7comm Block Management
        srv.set_access_mode("full")
        os.environ["S7_WRITES_ENABLED"] = "true"
        res = json.loads(
            srv.s7_plc_diagnose_connectivity(
                host="127.0.0.1",
                port=self.port,
                probe_count=1,
                target_blocks=["OB1", "BADBLOCK"],
                verify_upload_channel=False,
            )
        )
        self.assertTrue(res["read_ready"])
        self.assertTrue(res["upload_ready"])
        impact = res["online_blocks"]["target_block_impact"]
        self.assertEqual(impact["will_overwrite_existing"], ["OB1"])
        self.assertEqual(impact["invalid_identifiers"], ["BADBLOCK"])
        self.assertTrue(any("BADBLOCK" in w for w in res["warnings"]))


if __name__ == "__main__":
    unittest.main()


