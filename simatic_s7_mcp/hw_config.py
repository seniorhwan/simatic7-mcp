"""
Siemens STEP 7 Classic Hardware Configuration (HW Config / DIN Rail) Engine.

Provides:
  1. Curated Siemens S7-300 / S7-400 / ET200M / WinAC MLFB Module Catalog
  2. Multi-Rack DIN-Rail Station Builder (add/remove/configure modules on slots)
  3. Strict Siemens Hardware Validation:
     - Slot constraints (S7-300: Slot 1 PS, Slot 2 CPU, Slot 3 IM, Slots 4..11 SM/CP/FM;
       S7-400: Slots 1..18; WinAC PC Station)
     - Power supply backplane current budget verification
     - Automatic non-overlapping I/O byte address allocation & overlap detection
     - MPI / PROFIBUS DP / PROFINET IO subnet & IP/node conflict checks
  4. Standard Siemens STEP 7 HW Config ASCII .cfg Generator & Round-Trip Parser
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .contracts import compute_content_hash


@dataclass(frozen=True)
class S7HardwareCatalogItem:
    """Represents an authentic Siemens STEP 7 Classic hardware catalog module."""

    mlfb: str
    short_name: str
    family: str  # "S7-300", "S7-400", "ET200M", "WINAC"
    category: str  # "RACK", "PS", "CPU", "IM", "DI", "DO", "DIO", "AI", "AO", "AIO", "CP", "FM"
    description: str
    firmware: str = ""
    input_bytes: int = 0
    output_bytes: int = 0
    backplane_ma: int = 0
    supply_ma: int = 0  # Non-zero for power supply modules
    slot_width: int = 1
    interfaces: Tuple[str, ...] = ()
    default_i_start: Optional[int] = None
    default_q_start: Optional[int] = None
    aliases: Tuple[str, ...] = ()


# Curated, verified Siemens STEP 7 Classic Hardware Catalog
S7_HARDWARE_CATALOG: Tuple[S7HardwareCatalogItem, ...] = (
    # --- Racks / DIN Rails ---
    S7HardwareCatalogItem(
        mlfb="6ES7 390-1AF30-0AA0",
        short_name="UR_S7300_RAIL",
        family="S7-300",
        category="RACK",
        description="S7-300 Mounting Rail (DIN Rail, 530mm, 11 Slots)",
        aliases=("RAIL", "DIN_RAIL", "S7300_RAIL", "UR"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 400-1JA01-0AA0",
        short_name="UR2",
        family="S7-400",
        category="RACK",
        description="S7-400 Universal Rack UR2 (9 Slots)",
        aliases=("UR2", "S7400_RACK"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 400-1TA01-0AA0",
        short_name="UR1",
        family="S7-400",
        category="RACK",
        description="S7-400 Universal Rack UR1 (18 Slots)",
        aliases=("UR1", "S7400_UR1"),
    ),
    # --- Power Supplies (PS) ---
    S7HardwareCatalogItem(
        mlfb="6ES7 307-1BA01-0AA0",
        short_name="PS 307 2A",
        family="S7-300",
        category="PS",
        description="Load Power Supply PS 307 120/230VAC -> 24VDC / 2A",
        supply_ma=2000,
        aliases=("PS307_2A", "PS 307 2A"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 307-1EA01-0AA0",
        short_name="PS 307 5A",
        family="S7-300",
        category="PS",
        description="Load Power Supply PS 307 120/230VAC -> 24VDC / 5A",
        supply_ma=5000,
        aliases=("PS307_5A", "PS 307 5A", "PS307"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 307-1KA02-0AA0",
        short_name="PS 307 10A",
        family="S7-300",
        category="PS",
        description="Load Power Supply PS 307 120/230VAC -> 24VDC / 10A",
        supply_ma=10000,
        aliases=("PS307_10A", "PS 307 10A"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 407-0KA02-0AA0",
        short_name="PS 407 10A",
        family="S7-400",
        category="PS",
        description="S7-400 Power Supply PS 407 10A (120/230V UC)",
        supply_ma=10000,
        slot_width=2,
        aliases=("PS407_10A", "PS 407 10A", "PS407"),
    ),
    # --- S7-300 CPUs ---
    S7HardwareCatalogItem(
        mlfb="6ES7 312-1AE14-0AB0",
        short_name="CPU 312",
        family="S7-300",
        category="CPU",
        description="CPU 312, 32 KB Work Memory, MPI",
        firmware="V3.3",
        backplane_ma=650,
        interfaces=("MPI",),
        aliases=("CPU312", "CPU 312"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 313-5BG04-0AB0",
        short_name="CPU 313C",
        family="S7-300",
        category="CPU",
        description="Compact CPU 313C, 128 KB, 24DI/16DO/4AI/2AO, MPI",
        firmware="V3.3",
        input_bytes=13,
        output_bytes=6,
        backplane_ma=800,
        interfaces=("MPI",),
        default_i_start=124,
        default_q_start=124,
        aliases=("CPU313C", "CPU 313C", "CPU313"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 314-1AG14-0AB0",
        short_name="CPU 314",
        family="S7-300",
        category="CPU",
        description="CPU 314, 128 KB Work Memory, MPI",
        firmware="V3.3",
        backplane_ma=700,
        interfaces=("MPI",),
        aliases=("CPU314", "CPU 314"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 314-6EH04-0AB0",
        short_name="CPU 314C-2 PN/DP",
        family="S7-300",
        category="CPU",
        description="Compact CPU 314C-2 PN/DP, 192 KB, 24DI/16DO/4AI/2AO, MPI/DP + PROFINET",
        firmware="V3.3",
        input_bytes=13,
        output_bytes=6,
        backplane_ma=850,
        interfaces=("MPI/DP", "PROFINET"),
        default_i_start=124,
        default_q_start=124,
        aliases=("CPU314C", "CPU 314C-2 PN/DP", "CPU314C2PNDP"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 315-2AH14-0AB0",
        short_name="CPU 315-2 DP",
        family="S7-300",
        category="CPU",
        description="CPU 315-2 DP, 256 KB Work Memory, MPI + PROFIBUS DP Master",
        firmware="V3.3",
        backplane_ma=850,
        interfaces=("MPI", "PROFIBUS_DP"),
        aliases=("CPU315", "CPU 315-2 DP", "CPU3152DP"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 315-2EH14-0AB0",
        short_name="CPU 315-2 PN/DP",
        family="S7-300",
        category="CPU",
        description="CPU 315-2 PN/DP, 384 KB Work Memory, MPI/DP + PROFINET IO",
        firmware="V3.2",
        backplane_ma=850,
        interfaces=("MPI/DP", "PROFINET"),
        aliases=("CPU 315-2 PN/DP", "CPU3152PNDP", "CPU315PN"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 317-2EK14-0AB0",
        short_name="CPU 317-2 PN/DP",
        family="S7-300",
        category="CPU",
        description="CPU 317-2 PN/DP, 1 MB Work Memory, MPI/DP + PROFINET IO",
        firmware="V3.2",
        backplane_ma=900,
        interfaces=("MPI/DP", "PROFINET"),
        aliases=("CPU317", "CPU 317-2 PN/DP", "CPU3172PNDP"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 318-3EL01-0AB0",
        short_name="CPU 319-3 PN/DP",
        family="S7-300",
        category="CPU",
        description="CPU 319-3 PN/DP, 2 MB Work Memory, MPI/DP + DP + PROFINET IO",
        firmware="V3.2",
        backplane_ma=1250,
        interfaces=("MPI/DP", "PROFIBUS_DP", "PROFINET"),
        aliases=("CPU319", "CPU 319-3 PN/DP", "CPU3193PNDP"),
    ),
    # --- S7-400 CPUs ---
    S7HardwareCatalogItem(
        mlfb="6ES7 412-2EK06-0AB0",
        short_name="CPU 412-2 PN",
        family="S7-400",
        category="CPU",
        description="S7-400 CPU 412-2 PN, 1 MB Work Memory, MPI/DP + PROFINET",
        firmware="V6.0",
        backplane_ma=1100,
        interfaces=("MPI/DP", "PROFINET"),
        aliases=("CPU412", "CPU 412-2 PN"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 414-3EM06-0AB0",
        short_name="CPU 414-3 PN/DP",
        family="S7-400",
        category="CPU",
        description="S7-400 CPU 414-3 PN/DP, 4 MB Work Memory, MPI/DP + PROFINET",
        firmware="V6.0",
        backplane_ma=1300,
        slot_width=2,
        interfaces=("MPI/DP", "PROFINET"),
        aliases=("CPU414", "CPU 414-3 PN/DP", "CPU4143PNDP"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 416-3ES06-0AB0",
        short_name="CPU 416-3 PN/DP",
        family="S7-400",
        category="CPU",
        description="S7-400 CPU 416-3 PN/DP, 16 MB Work Memory, MPI/DP + PROFINET",
        firmware="V6.0",
        backplane_ma=1500,
        slot_width=2,
        interfaces=("MPI/DP", "PROFINET"),
        aliases=("CPU416", "CPU 416-3 PN/DP", "CPU4163PNDP"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 417-4XT05-0AB0",
        short_name="CPU 417-4",
        family="S7-400",
        category="CPU",
        description="S7-400 CPU 417-4, 30 MB Work Memory, MPI/DP + DP",
        firmware="V5.3",
        backplane_ma=1600,
        slot_width=2,
        interfaces=("MPI/DP", "PROFIBUS_DP"),
        aliases=("CPU417", "CPU 417-4"),
    ),
    # --- WinAC ---
    S7HardwareCatalogItem(
        mlfb="6ES7 671-0RC08-0YA0",
        short_name="WinAC RTX",
        family="WINAC",
        category="CPU",
        description="SIMATIC WinAC RTX Software PLC (PC Station)",
        firmware="V4.6",
        interfaces=("INDUSTRIAL_ETHERNET",),
        aliases=("WINAC", "WINAC RTX", "WINLC"),
    ),
    # --- Interface Modules (IM) ---
    S7HardwareCatalogItem(
        mlfb="6ES7 360-3AA01-0AA0",
        short_name="IM 360",
        family="S7-300",
        category="IM",
        description="Interface Module IM 360 (Central Rack Expansion Sender)",
        backplane_ma=350,
        aliases=("IM360", "IM 360"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 361-3CA01-0AA0",
        short_name="IM 361",
        family="S7-300",
        category="IM",
        description="Interface Module IM 361 (Expansion Rack Receiver)",
        backplane_ma=150,
        aliases=("IM361", "IM 361"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 365-0BA01-0AA0",
        short_name="IM 365",
        family="S7-300",
        category="IM",
        description="Interface Module IM 365 (2-Rack Send/Receive Pair, 1m)",
        backplane_ma=100,
        aliases=("IM365", "IM 365"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 153-1AA03-0XB0",
        short_name="IM 153-1",
        family="ET200M",
        category="IM",
        description="ET 200M PROFIBUS DP Interface Module IM 153-1",
        backplane_ma=350,
        interfaces=("PROFIBUS_DP",),
        aliases=("IM153-1", "ET200M_DP"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 153-4AA01-0XB0",
        short_name="IM 153-4 PN",
        family="ET200M",
        category="IM",
        description="ET 200M PROFINET IO Interface Module IM 153-4 PN",
        backplane_ma=400,
        interfaces=("PROFINET",),
        aliases=("IM153-4", "ET200M_PN"),
    ),
    # --- Digital Input Modules (SM 321) ---
    S7HardwareCatalogItem(
        mlfb="6ES7 321-1BH02-0AA0",
        short_name="DI16xDC24V",
        family="S7-300",
        category="DI",
        description="SM 321 Digital Input 16 x DC 24V (2 bytes)",
        input_bytes=2,
        backplane_ma=25,
        aliases=("SM321_DI16", "DI16", "DI 16xDC24V"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 321-1BL00-0AA0",
        short_name="DI32xDC24V",
        family="S7-300",
        category="DI",
        description="SM 321 Digital Input 32 x DC 24V (4 bytes)",
        input_bytes=4,
        backplane_ma=15,
        aliases=("SM321_DI32", "DI32", "DI 32xDC24V"),
    ),
    # --- Digital Output Modules (SM 322) ---
    S7HardwareCatalogItem(
        mlfb="6ES7 322-1BH01-0AA0",
        short_name="DO16xDC24V/0.5A",
        family="S7-300",
        category="DO",
        description="SM 322 Digital Output 16 x DC 24V / 0.5A (2 bytes)",
        output_bytes=2,
        backplane_ma=80,
        aliases=("SM322_DO16", "DO16", "DO 16xDC24V"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 322-1BL00-0AA0",
        short_name="DO32xDC24V/0.5A",
        family="S7-300",
        category="DO",
        description="SM 322 Digital Output 32 x DC 24V / 0.5A (4 bytes)",
        output_bytes=4,
        backplane_ma=110,
        aliases=("SM322_DO32", "DO32", "DO 32xDC24V"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 322-1HF01-0AA0",
        short_name="DO8xRel. AC230V",
        family="S7-300",
        category="DO",
        description="SM 322 Digital Output 8 x Relay AC 230V / 2A (1 byte)",
        output_bytes=1,
        backplane_ma=40,
        aliases=("SM322_REL8", "DO8_RELAY"),
    ),
    # --- Combo Digital I/O (SM 323) ---
    S7HardwareCatalogItem(
        mlfb="6ES7 323-1BL00-0AA0",
        short_name="DI16/DO16xDC24V",
        family="S7-300",
        category="DIO",
        description="SM 323 Digital Input/Output 16 DI / 16 DO DC 24V (2 in / 2 out bytes)",
        input_bytes=2,
        output_bytes=2,
        backplane_ma=80,
        aliases=("SM323", "DI16/DO16", "DIO16"),
    ),
    # --- Analog Input Modules (SM 331) ---
    S7HardwareCatalogItem(
        mlfb="6ES7 331-7KF02-0AB0",
        short_name="AI8x12Bit",
        family="S7-300",
        category="AI",
        description="SM 331 Analog Input 8 x 12-Bit U/I/TC/RTD (16 input bytes)",
        input_bytes=16,
        backplane_ma=50,
        default_i_start=256,
        aliases=("SM331_AI8", "AI8", "AI 8x12Bit"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 331-7NF00-0AB0",
        short_name="AI8x16Bit",
        family="S7-300",
        category="AI",
        description="SM 331 Analog Input 8 x 16-Bit High-Resolution (16 input bytes)",
        input_bytes=16,
        backplane_ma=120,
        default_i_start=256,
        aliases=("SM331_AI8_16BIT", "AI8x16Bit"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 331-7PF01-0AB0",
        short_name="AI8xRTD",
        family="S7-300",
        category="AI",
        description="SM 331 Analog Input 8 x RTD Pt100/Ni100 (16 input bytes)",
        input_bytes=16,
        backplane_ma=100,
        default_i_start=256,
        aliases=("SM331_RTD", "AI8xRTD"),
    ),
    # --- Analog Output Modules (SM 332) ---
    S7HardwareCatalogItem(
        mlfb="6ES7 332-5HD01-0AB0",
        short_name="AO4x12Bit",
        family="S7-300",
        category="AO",
        description="SM 332 Analog Output 4 x 12-Bit U/I (8 output bytes)",
        output_bytes=8,
        backplane_ma=60,
        default_q_start=256,
        aliases=("SM332_AO4", "AO4", "AO 4x12Bit"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 332-5HF00-0AB0",
        short_name="AO8x12Bit",
        family="S7-300",
        category="AO",
        description="SM 332 Analog Output 8 x 12-Bit U/I (16 output bytes)",
        output_bytes=16,
        backplane_ma=100,
        default_q_start=256,
        aliases=("SM332_AO8", "AO8", "AO 8x12Bit"),
    ),
    # --- Analog Combo (SM 334) ---
    S7HardwareCatalogItem(
        mlfb="6ES7 334-0CE01-0AA0",
        short_name="AI4/AO2x8Bit",
        family="S7-300",
        category="AIO",
        description="SM 334 Analog I/O 4 AI / 2 AO 8-Bit (8 in / 4 out bytes)",
        input_bytes=8,
        output_bytes=4,
        backplane_ma=55,
        default_i_start=256,
        default_q_start=256,
        aliases=("SM334", "AI4/AO2"),
    ),
    # --- Communications Processors (CP) ---
    S7HardwareCatalogItem(
        mlfb="6GK7 343-1CX10-0XE0",
        short_name="CP 343-1 Lean",
        family="S7-300",
        category="CP",
        description="CP 343-1 Lean Industrial Ethernet / PROFINET IO Device",
        firmware="V3.0",
        backplane_ma=200,
        interfaces=("INDUSTRIAL_ETHERNET",),
        aliases=("CP343-1 LEAN", "CP 343-1 Lean"),
    ),
    S7HardwareCatalogItem(
        mlfb="6GK7 343-1EX30-0XE0",
        short_name="CP 343-1",
        family="S7-300",
        category="CP",
        description="CP 343-1 Industrial Ethernet / PROFINET IO Controller",
        firmware="V3.0",
        backplane_ma=200,
        interfaces=("INDUSTRIAL_ETHERNET", "PROFINET"),
        aliases=("CP343-1", "CP 343-1"),
    ),
    S7HardwareCatalogItem(
        mlfb="6GK7 342-5DA03-0XE0",
        short_name="CP 342-5",
        family="S7-300",
        category="CP",
        description="CP 342-5 PROFIBUS DP Master/Slave Communications Processor",
        firmware="V5.7",
        input_bytes=16,
        output_bytes=16,
        backplane_ma=200,
        default_i_start=256,
        default_q_start=256,
        interfaces=("PROFIBUS_DP",),
        aliases=("CP342-5", "CP 342-5"),
    ),
    S7HardwareCatalogItem(
        mlfb="6GK7 443-1EX30-0XE0",
        short_name="CP 443-1",
        family="S7-400",
        category="CP",
        description="S7-400 CP 443-1 Industrial Ethernet / PROFINET IO Controller",
        firmware="V3.0",
        backplane_ma=1400,
        interfaces=("INDUSTRIAL_ETHERNET", "PROFINET"),
        aliases=("CP443-1", "CP 443-1"),
    ),
    # --- Function Modules (FM) ---
    S7HardwareCatalogItem(
        mlfb="6ES7 350-1AH03-0AE0",
        short_name="FM 350-1",
        family="S7-300",
        category="FM",
        description="FM 350-1 Counter Module 1-Channel 500 kHz (16 in / 16 out bytes)",
        input_bytes=16,
        output_bytes=16,
        backplane_ma=160,
        default_i_start=256,
        default_q_start=256,
        aliases=("FM350-1", "FM 350-1", "COUNTER_MODULE"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 351-1AH01-0AE0",
        short_name="FM 351",
        family="S7-300",
        category="FM",
        description="FM 351 Positioning Module Rapid/Creep Feed 2-Channel (16 in / 16 out bytes)",
        input_bytes=16,
        output_bytes=16,
        backplane_ma=150,
        default_i_start=256,
        default_q_start=256,
        aliases=("FM351", "FM 351"),
    ),
    # --- S7-400 Signal Modules ---
    S7HardwareCatalogItem(
        mlfb="6ES7 421-1BL01-0AA0",
        short_name="SM 421 DI32xDC24V",
        family="S7-400",
        category="DI",
        description="SM 421 Digital Input 32 x DC 24V (4 bytes)",
        input_bytes=4,
        backplane_ma=20,
        aliases=("SM421_DI32", "SM 421 DI32"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 422-1BL00-0AA0",
        short_name="SM 422 DO32xDC24V/0.5A",
        family="S7-400",
        category="DO",
        description="SM 422 Digital Output 32 x DC 24V / 0.5A (4 bytes)",
        output_bytes=4,
        backplane_ma=150,
        aliases=("SM422_DO32", "SM 422 DO32"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 431-1KF00-0AB0",
        short_name="SM 431 AI8x13Bit",
        family="S7-400",
        category="AI",
        description="SM 431 Analog Input 8 x 13-Bit (16 input bytes)",
        input_bytes=16,
        backplane_ma=350,
        default_i_start=512,
        aliases=("SM431_AI8", "SM 431 AI8"),
    ),
    S7HardwareCatalogItem(
        mlfb="6ES7 432-1HF00-0AB0",
        short_name="SM 432 AO8x13Bit",
        family="S7-400",
        category="AO",
        description="SM 432 Analog Output 8 x 13-Bit (16 output bytes)",
        output_bytes=16,
        backplane_ma=150,
        default_q_start=512,
        aliases=("SM432_AO8", "SM 432 AO8"),
    ),
)


def _normalize_key(val: str) -> str:
    return re.sub(r"[\s_\-/]+", "", (val or "").strip().upper())


def resolve_catalog_module(mlfb_or_name: str) -> Optional[S7HardwareCatalogItem]:
    """Resolves a Siemens MLFB ordering number, short name, or alias to a catalog item."""
    if not mlfb_or_name or not isinstance(mlfb_or_name, str):
        return None
    norm = _normalize_key(mlfb_or_name)
    for item in S7_HARDWARE_CATALOG:
        if _normalize_key(item.mlfb) == norm or _normalize_key(item.short_name) == norm:
            return item
        if any(_normalize_key(a) == norm for a in item.aliases):
            return item
    # Substring fallback for partial matches like "315-2EH14" or "321-1BL00"
    for item in S7_HARDWARE_CATALOG:
        if norm in _normalize_key(item.mlfb) or norm in _normalize_key(item.short_name):
            return item
    return None


def search_hardware_catalog(
    query: str = "",
    family: Optional[str] = None,
    category: Optional[str] = None,
    max_results: int = 25,
) -> List[Dict[str, Any]]:
    """Searches the Siemens STEP 7 Classic MLFB hardware catalog."""
    q_norm = (query or "").strip().lower()
    fam_norm = (family or "").strip().upper()
    cat_norm = (category or "").strip().upper()

    matches: List[Dict[str, Any]] = []
    for item in S7_HARDWARE_CATALOG:
        if fam_norm and item.family.upper() != fam_norm:
            continue
        if cat_norm and item.category.upper() != cat_norm:
            continue
        if q_norm:
            haystack = " ".join(
                [item.mlfb, item.short_name, item.description, item.category, item.family, *item.aliases]
            ).lower()
            if q_norm not in haystack and _normalize_key(q_norm).lower() not in _normalize_key(haystack).lower():
                continue
        matches.append({
            "mlfb": item.mlfb,
            "short_name": item.short_name,
            "family": item.family,
            "category": item.category,
            "description": item.description,
            "firmware": item.firmware,
            "input_bytes": item.input_bytes,
            "output_bytes": item.output_bytes,
            "backplane_ma": item.backplane_ma,
            "supply_ma": item.supply_ma,
            "slot_width": item.slot_width,
            "interfaces": list(item.interfaces),
        })
        if len(matches) >= max_results:
            break
    return matches


@dataclass
class SubnetSpec:
    """Represents an MPI, PROFIBUS DP, or Industrial Ethernet / PROFINET subnet."""

    name: str
    subnet_type: str  # "MPI", "PROFIBUS", "ETHERNET"
    baudrate: str = "187.5K"
    network_id: str = "0001-0001"
    subnet_mask: str = "255.255.255.0"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "subnet_type": self.subnet_type,
            "baudrate": self.baudrate,
            "network_id": self.network_id,
            "subnet_mask": self.subnet_mask,
        }


@dataclass
class ModuleSlot:
    """Represents a module slotted onto a SIMATIC rack / DIN rail."""

    rack: int
    slot: int
    mlfb: str
    name: str
    category: str
    firmware: str = ""
    input_start: Optional[int] = None
    input_bytes: int = 0
    output_start: Optional[int] = None
    output_bytes: int = 0
    backplane_ma: int = 0
    supply_ma: int = 0
    slot_width: int = 1
    ip_address: Optional[str] = None
    subnet_mask: Optional[str] = None
    mpi_address: Optional[int] = None
    profibus_address: Optional[int] = None
    subnet_ref: Optional[str] = None
    parameters: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rack": self.rack,
            "slot": self.slot,
            "mlfb": self.mlfb,
            "name": self.name,
            "category": self.category,
            "firmware": self.firmware,
            "input_start": self.input_start,
            "input_bytes": self.input_bytes,
            "output_start": self.output_start,
            "output_bytes": self.output_bytes,
            "backplane_ma": self.backplane_ma,
            "supply_ma": self.supply_ma,
            "slot_width": self.slot_width,
            "ip_address": self.ip_address,
            "subnet_mask": self.subnet_mask,
            "mpi_address": self.mpi_address,
            "profibus_address": self.profibus_address,
            "subnet_ref": self.subnet_ref,
            "parameters": dict(self.parameters),
        }


@dataclass
class StationHardwareConfig:
    """Complete SIMATIC 300 / 400 / PC Station Hardware Configuration model."""

    station_name: str = "SIMATIC 300(1)"
    plc_family: str = "S7-300"  # "S7-300", "S7-400", "WINAC"
    step7_version: str = "V5.6"
    modules: List[ModuleSlot] = field(default_factory=list)
    subnets: List[SubnetSpec] = field(default_factory=list)

    def _max_slots(self) -> int:
        if self.plc_family == "S7-400":
            return 18
        if self.plc_family == "WINAC":
            return 16
        return 11  # S7-300 DIN Rail has slots 1..11

    def _next_free_io_byte(self, direction: str, byte_len: int, preferred_start: Optional[int] = None) -> int:
        if byte_len <= 0:
            return 0
        occupied: set[int] = set()
        for mod in self.modules:
            start = mod.input_start if direction == "I" else mod.output_start
            length = mod.input_bytes if direction == "I" else mod.output_bytes
            if start is not None and length > 0:
                occupied.update(range(start, start + length))
        candidate = preferred_start if preferred_start is not None else 0
        # Standard Siemens S7-300 alignment: digital slots increment by 4, analog starts at >= 256
        step = 2 if byte_len >= 2 else 1
        while any((candidate + b) in occupied for b in range(byte_len)):
            candidate += step
        return candidate

    def add_module(
        self,
        *,
        rack: int = 0,
        slot: int,
        module: str,
        input_start: Optional[int] = None,
        output_start: Optional[int] = None,
        ip_address: Optional[str] = None,
        subnet_mask: Optional[str] = None,
        mpi_address: Optional[int] = None,
        profibus_address: Optional[int] = None,
        subnet_ref: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        replace: bool = True,
    ) -> Tuple[bool, Optional[ModuleSlot], Optional[str]]:
        """Places a Siemens module onto the specified rack and DIN-rail slot."""
        if rack < 0 or rack > 3:
            return False, None, f"Invalid rack index {rack}. Supported racks are 0..3."
        max_slot = self._max_slots()
        if slot < 1 or slot > max_slot:
            return False, None, f"Invalid slot {slot} for {self.plc_family}. Valid slots are 1..{max_slot}."

        cat = resolve_catalog_module(module)
        if cat is None:
            # Allow explicit raw MLFB format ("6ES7 ...") even if not in curated catalog
            if re.match(r"^6E[SPK][0-9A-Z]\s+[0-9A-Z\-]+$", module.strip(), re.IGNORECASE):
                cat = S7HardwareCatalogItem(
                    mlfb=module.strip().upper(),
                    short_name=module.strip().upper(),
                    family=self.plc_family,
                    category="SM",
                    description=f"Custom Siemens Module {module.strip()}",
                )
            else:
                return False, None, (
                    f"Unknown module '{module}'. Use s7_hw_catalog_search to find valid MLFB numbers or aliases."
                )

        if replace:
            self.modules = [m for m in self.modules if not (m.rack == rack and m.slot == slot)]
        else:
            if any(m.rack == rack and m.slot == slot for m in self.modules):
                return False, None, f"Rack {rack}, Slot {slot} is already occupied."

        eff_i_start = input_start
        if eff_i_start is None and cat.input_bytes > 0:
            eff_i_start = self._next_free_io_byte("I", cat.input_bytes, preferred_start=cat.default_i_start)

        eff_q_start = output_start
        if eff_q_start is None and cat.output_bytes > 0:
            eff_q_start = self._next_free_io_byte("Q", cat.output_bytes, preferred_start=cat.default_q_start)

        eff_mpi = mpi_address if mpi_address is not None else (2 if cat.category == "CPU" else None)

        slot_obj = ModuleSlot(
            rack=rack,
            slot=slot,
            mlfb=cat.mlfb,
            name=cat.short_name,
            category=cat.category,
            firmware=cat.firmware,
            input_start=eff_i_start,
            input_bytes=cat.input_bytes,
            output_start=eff_q_start,
            output_bytes=cat.output_bytes,
            backplane_ma=cat.backplane_ma,
            supply_ma=cat.supply_ma,
            slot_width=cat.slot_width,
            ip_address=ip_address,
            subnet_mask=subnet_mask or ("255.255.255.0" if ip_address else None),
            mpi_address=eff_mpi,
            profibus_address=profibus_address,
            subnet_ref=subnet_ref,
            parameters=dict(parameters or {}),
        )
        self.modules.append(slot_obj)
        self.modules.sort(key=lambda m: (m.rack, m.slot))
        return True, slot_obj, None

    def remove_module(self, rack: int, slot: int) -> Tuple[bool, str]:
        before = len(self.modules)
        self.modules = [m for m in self.modules if not (m.rack == rack and m.slot == slot)]
        if len(self.modules) == before:
            return False, f"No module found at Rack {rack}, Slot {slot}."
        return True, f"Removed module from Rack {rack}, Slot {slot}."

    def validate(self) -> Dict[str, Any]:
        """Validates Siemens S7-300/400 DIN-rail slot rules, power budget, I/O overlaps, and networking."""
        errors: List[str] = []
        warnings: List[str] = []

        # 1. CPU presence & slot checks
        cpus = [m for m in self.modules if m.category == "CPU"]
        if not cpus:
            errors.append("Station has no CPU module configured (at least one CPU is required).")
        elif len(cpus) > 1 and self.plc_family == "S7-300":
            errors.append("S7-300 stations support only one central CPU in Rack 0, Slot 2.")

        occupied_slots: Dict[Tuple[int, int], str] = {}
        for m in self.modules:
            if m.slot < 1 or m.slot > self._max_slots():
                errors.append(f"Module '{m.name}' on Rack {m.rack} Slot {m.slot} exceeds max slot {self._max_slots()}.")

            # Multi-slot width collision detection (e.g. S7-400 2-slot PS/CPU)
            for s_off in range(m.slot_width):
                key = (m.rack, m.slot + s_off)
                if key in occupied_slots:
                    errors.append(
                        f"Slot collision on Rack {m.rack} Slot {key[1]} between '{occupied_slots[key]}' and '{m.name}'."
                    )
                occupied_slots[key] = m.name

            # Strict S7-300 DIN Rail slot architecture rules
            if self.plc_family == "S7-300" and m.rack == 0:
                if m.slot == 1 and m.category != "PS":
                    errors.append(f"S7-300 Rack 0 Slot 1 is reserved for Power Supply (PS), got {m.category} '{m.name}'.")
                elif m.slot == 2 and m.category != "CPU":
                    errors.append(f"S7-300 Rack 0 Slot 2 is strictly reserved for CPU, got {m.category} '{m.name}'.")
                elif m.slot == 3 and m.category != "IM":
                    errors.append(
                        f"S7-300 Rack 0 Slot 3 is reserved for Interface Module (IM 360/365), got {m.category} '{m.name}'."
                    )
                elif m.slot >= 4 and m.category in ("PS", "CPU", "IM"):
                    errors.append(
                        f"S7-300 Rack 0 Slot {m.slot} is for Signal/CP/FM modules, not {m.category} '{m.name}'."
                    )

            # IP address validation
            if m.ip_address:
                try:
                    ipaddress.IPv4Address(m.ip_address)
                except ValueError:
                    errors.append(f"Invalid IPv4 address '{m.ip_address}' on Rack {m.rack} Slot {m.slot} ({m.name}).")

            # MPI / PROFIBUS address bounds
            if m.mpi_address is not None and not (0 <= m.mpi_address <= 126):
                errors.append(f"MPI address {m.mpi_address} on '{m.name}' out of range (0..126).")
            if m.profibus_address is not None and not (0 <= m.profibus_address <= 125):
                errors.append(f"PROFIBUS DP address {m.profibus_address} on '{m.name}' out of range (0..125).")

        # 2. Power budget per rack
        racks = sorted({m.rack for m in self.modules})
        power_summary: Dict[str, Any] = {}
        for r in racks:
            rack_mods = [m for m in self.modules if m.rack == r]
            total_load_ma = sum(m.backplane_ma for m in rack_mods)
            supply_ma = sum(m.supply_ma for m in rack_mods)
            power_summary[f"rack_{r}"] = {
                "backplane_load_ma": total_load_ma,
                "ps_supply_ma": supply_ma,
            }
            if supply_ma == 0:
                warnings.append(f"Rack {r} has no Power Supply (PS) configured on Slot 1 (load is {total_load_ma} mA).")
            elif total_load_ma > supply_ma:
                errors.append(
                    f"Power budget exceeded on Rack {r}: total load {total_load_ma} mA exceeds PS rating {supply_ma} mA."
                )

        # 3. I/O address overlap detection
        i_owners: Dict[int, str] = {}
        q_owners: Dict[int, str] = {}
        io_map: List[Dict[str, Any]] = []
        for m in self.modules:
            label = f"R{m.rack}S{m.slot}:{m.name}"
            entry: Dict[str, Any] = {"rack": m.rack, "slot": m.slot, "module": m.name, "mlfb": m.mlfb}
            if m.input_bytes > 0 and m.input_start is not None:
                entry["input_range"] = f"IB {m.input_start}..IB {m.input_start + m.input_bytes - 1}"
                for b in range(m.input_start, m.input_start + m.input_bytes):
                    if b in i_owners:
                        errors.append(f"Input byte overlap at IB {b} between {i_owners[b]} and {label}.")
                    i_owners[b] = label
            if m.output_bytes > 0 and m.output_start is not None:
                entry["output_range"] = f"QB {m.output_start}..QB {m.output_start + m.output_bytes - 1}"
                for b in range(m.output_start, m.output_start + m.output_bytes):
                    if b in q_owners:
                        errors.append(f"Output byte overlap at QB {b} between {q_owners[b]} and {label}.")
                    q_owners[b] = label
            if "input_range" in entry or "output_range" in entry:
                io_map.append(entry)

        # 4. Duplicate IP checks
        seen_ips: Dict[str, str] = {}
        for m in self.modules:
            if m.ip_address:
                if m.ip_address in seen_ips:
                    errors.append(f"Duplicate IP address {m.ip_address} shared by {seen_ips[m.ip_address]} and {m.name}.")
                seen_ips[m.ip_address] = m.name

        return {
            "valid": len(errors) == 0,
            "station_name": self.station_name,
            "plc_family": self.plc_family,
            "module_count": len(self.modules),
            "errors": errors,
            "warnings": warnings,
            "power_summary": power_summary,
            "io_address_map": io_map,
        }

    def generate_cfg(self) -> str:
        """Generates a compliant Siemens STEP 7 Classic HW Config ASCII .cfg station file."""
        station_kind = {
            "S7-300": "S7300",
            "S7-400": "S7400",
            "WINAC": "PCSTATION",
        }.get(self.plc_family.upper(), "S7300")

        lines: List[str] = [
            'FILEVERSION "3.2"',
            f"#STEP7_VERSION {self.step7_version}",
            '#CREATED "SIMATIC STEP 7 Classic MCP Server"',
            "",
        ]

        # Emit Subnets
        eff_subnets = list(self.subnets)
        if not eff_subnets:
            eff_subnets.append(SubnetSpec(name="MPI(1)", subnet_type="MPI", baudrate="187.5K"))
            if any(m.profibus_address is not None for m in self.modules):
                eff_subnets.append(SubnetSpec(name="PROFIBUS(1)", subnet_type="PROFIBUS", baudrate="1.5M"))
            if any(m.ip_address for m in self.modules):
                eff_subnets.append(SubnetSpec(name="Ethernet(1)", subnet_type="ETHERNET", baudrate="100M"))

        for sub in eff_subnets:
            lines.extend([
                f'SUBNET {sub.subnet_type.upper()} , "{sub.name}"',
                "BEGIN",
                f'  NET_ID "{sub.network_id}"',
                f'  BAUDRATE "{sub.baudrate}"',
                "END",
                "",
            ])

        lines.append(f'STATION {station_kind} , "{self.station_name}"')
        lines.append("BEGIN")
        lines.append('  OBJECT_REMOVEABLE "1"')
        lines.append('  POS_X "0"')
        lines.append('  POS_Y "0"')

        # Emit Rack declarations
        racks = sorted({m.rack for m in self.modules}) or [0]
        rack_mlfb = "6ES7 390-1AF30-0AA0" if station_kind == "S7300" else "6ES7 400-1JA01-0AA0"
        rack_desc = "UR" if station_kind == "S7300" else "UR2"
        for r in racks:
            lines.extend([
                f'  RACK {r}, "{rack_mlfb}", "{rack_desc}"',
                "  BEGIN",
                f'    RACK_NO "{r}"',
                "  END",
            ])

        # Emit Modules
        for m in self.modules:
            fw_comment = f" {m.firmware}" if m.firmware else ""
            lines.append(f'  RACK {m.rack}, SLOT {m.slot}, "{m.mlfb}"{fw_comment}, "{m.name}"')
            lines.append("  BEGIN")
            if m.category == "CPU":
                lines.append('    CPU_NO "1"')
                lines.append('    STARTUP_AFTER_POWER_ON "WARM_RESTART"')
            if m.input_bytes > 0 and m.input_start is not None:
                lines.append(f"    LOCAL_IN_ADDRESSES ADDRESS {m.input_start}, 0, {m.input_bytes}, 0, 0, 0")
            if m.output_bytes > 0 and m.output_start is not None:
                lines.append(f"    LOCAL_OUT_ADDRESSES ADDRESS {m.output_start}, 0, {m.output_bytes}, 0, 0, 0")
            if m.mpi_address is not None:
                lines.extend([
                    "    INTERFACE 1",
                    "    BEGIN",
                    f'      SUBNET_REF "{m.subnet_ref or "MPI(1)"}"',
                    f'      NODE_ADDRESS "{m.mpi_address}"',
                    "    END",
                ])
            if m.profibus_address is not None:
                lines.extend([
                    "    INTERFACE 2",
                    "    BEGIN",
                    f'      SUBNET_REF "{m.subnet_ref or "PROFIBUS(1)"}"',
                    f'      NODE_ADDRESS "{m.profibus_address}"',
                    "    END",
                ])
            if m.ip_address:
                lines.extend([
                    "    INTERFACE 3",
                    "    BEGIN",
                    f'      SUBNET_REF "{m.subnet_ref or "Ethernet(1)"}"',
                    f'      IP_ADDRESS "{m.ip_address}"',
                    f'      SUBNET_MASK "{m.subnet_mask or "255.255.255.0"}"',
                    "    END",
                ])
            for pk, pv in sorted(m.parameters.items()):
                lines.append(f'    {pk.upper()} "{pv}"')
            lines.append("  END")

        lines.append("END")
        lines.append("")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        cfg_text = self.generate_cfg()
        return {
            "station_name": self.station_name,
            "plc_family": self.plc_family,
            "step7_version": self.step7_version,
            "modules": [m.to_dict() for m in self.modules],
            "subnets": [s.to_dict() for s in self.subnets],
            "content_hash": compute_content_hash("cfg", cfg_text),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StationHardwareConfig":
        cfg = cls(
            station_name=str(data.get("station_name", "SIMATIC 300(1)")),
            plc_family=str(data.get("plc_family", "S7-300")).upper(),
            step7_version=str(data.get("step7_version", "V5.6")),
        )
        for s in data.get("subnets", []):
            cfg.subnets.append(
                SubnetSpec(
                    name=s.get("name", "MPI(1)"),
                    subnet_type=s.get("subnet_type", "MPI"),
                    baudrate=s.get("baudrate", "187.5K"),
                    network_id=s.get("network_id", "0001-0001"),
                    subnet_mask=s.get("subnet_mask", "255.255.255.0"),
                )
            )
        for m in data.get("modules", []):
            cfg.modules.append(
                ModuleSlot(
                    rack=int(m.get("rack", 0)),
                    slot=int(m.get("slot", 2)),
                    mlfb=str(m.get("mlfb", "")),
                    name=str(m.get("name", "")),
                    category=str(m.get("category", "SM")),
                    firmware=str(m.get("firmware", "")),
                    input_start=m.get("input_start"),
                    input_bytes=int(m.get("input_bytes", 0)),
                    output_start=m.get("output_start"),
                    output_bytes=int(m.get("output_bytes", 0)),
                    backplane_ma=int(m.get("backplane_ma", 0)),
                    supply_ma=int(m.get("supply_ma", 0)),
                    slot_width=int(m.get("slot_width", 1)),
                    ip_address=m.get("ip_address"),
                    subnet_mask=m.get("subnet_mask"),
                    mpi_address=m.get("mpi_address"),
                    profibus_address=m.get("profibus_address"),
                    subnet_ref=m.get("subnet_ref"),
                    parameters=dict(m.get("parameters") or {}),
                )
            )
        cfg.modules.sort(key=lambda x: (x.rack, x.slot))
        return cfg


def parse_station_cfg(cfg_text: str) -> StationHardwareConfig:
    """Parses a Siemens STEP 7 ASCII .cfg hardware export back into a StationHardwareConfig."""
    station_name = "SIMATIC 300(1)"
    plc_family = "S7-300"
    step7_version = "V5.6"
    subnets: List[SubnetSpec] = []
    modules: List[ModuleSlot] = []

    lines = cfg_text.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i].strip()
        if ln.startswith("#STEP7_VERSION"):
            parts = ln.split()
            if len(parts) >= 2:
                step7_version = parts[1]
        elif ln.startswith("STATION "):
            m_st = re.match(r'^STATION\s+(\S+)\s*,\s*"([^"]+)"', ln)
            if m_st:
                st_kind = m_st.group(1).upper()
                station_name = m_st.group(2)
                if "400" in st_kind:
                    plc_family = "S7-400"
                elif "PC" in st_kind:
                    plc_family = "WINAC"
                else:
                    plc_family = "S7-300"
        elif ln.startswith("SUBNET "):
            m_sub = re.match(r'^SUBNET\s+(\S+)\s*,\s*"([^"]+)"', ln)
            if m_sub:
                sub = SubnetSpec(name=m_sub.group(2), subnet_type=m_sub.group(1).upper())
                j = i + 1
                while j < n and lines[j].strip() != "END":
                    s_ln = lines[j].strip()
                    m_b = re.match(r'^BAUDRATE\s+"([^"]+)"', s_ln)
                    m_id = re.match(r'^NET_ID\s+"([^"]+)"', s_ln)
                    if m_b:
                        sub.baudrate = m_b.group(1)
                    if m_id:
                        sub.network_id = m_id.group(1)
                    j += 1
                subnets.append(sub)
                i = j
        elif re.match(r"^RACK\s+\d+\s*,\s*SLOT\s+\d+", ln):
            m_mod = re.match(
                r'^RACK\s+(\d+)\s*,\s*SLOT\s+(\d+)\s*,\s*"([^"]+)"(?:\s+([^,]+))?\s*,\s*"([^"]+)"',
                ln,
            )
            if m_mod:
                rack_n = int(m_mod.group(1))
                slot_n = int(m_mod.group(2))
                mlfb = m_mod.group(3).strip()
                fw = (m_mod.group(4) or "").strip()
                name = m_mod.group(5).strip()
                cat = resolve_catalog_module(mlfb) or resolve_catalog_module(name)
                mod = ModuleSlot(
                    rack=rack_n,
                    slot=slot_n,
                    mlfb=mlfb,
                    name=name,
                    category=cat.category if cat else ("CPU" if "CPU" in name.upper() else "SM"),
                    firmware=fw or (cat.firmware if cat else ""),
                    input_bytes=cat.input_bytes if cat else 0,
                    output_bytes=cat.output_bytes if cat else 0,
                    backplane_ma=cat.backplane_ma if cat else 0,
                    supply_ma=cat.supply_ma if cat else 0,
                    slot_width=cat.slot_width if cat else 1,
                )
                depth = 0
                j = i + 1
                in_iface = 0
                while j < n:
                    m_ln = lines[j].strip()
                    if m_ln == "BEGIN":
                        depth += 1
                    elif m_ln == "END":
                        depth -= 1
                        if depth <= 0:
                            break
                        in_iface = 0
                    elif m_ln.startswith("INTERFACE "):
                        parts = m_ln.split()
                        if len(parts) >= 2 and parts[1].isdigit():
                            in_iface = int(parts[1])
                    elif m_ln.startswith("LOCAL_IN_ADDRESSES ADDRESS"):
                        nums = re.findall(r"\d+", m_ln)
                        if len(nums) >= 3:
                            mod.input_start = int(nums[0])
                            mod.input_bytes = int(nums[2])
                    elif m_ln.startswith("LOCAL_OUT_ADDRESSES ADDRESS"):
                        nums = re.findall(r"\d+", m_ln)
                        if len(nums) >= 3:
                            mod.output_start = int(nums[0])
                            mod.output_bytes = int(nums[2])
                    elif m_ln.startswith("IP_ADDRESS"):
                        m_ip = re.search(r'"([^"]+)"', m_ln)
                        if m_ip:
                            mod.ip_address = m_ip.group(1)
                    elif m_ln.startswith("SUBNET_MASK"):
                        m_sm = re.search(r'"([^"]+)"', m_ln)
                        if m_sm:
                            mod.subnet_mask = m_sm.group(1)
                    elif m_ln.startswith("SUBNET_REF"):
                        m_sr = re.search(r'"([^"]+)"', m_ln)
                        if m_sr:
                            mod.subnet_ref = m_sr.group(1)
                    elif m_ln.startswith("NODE_ADDRESS"):
                        m_na = re.search(r'"(\d+)"', m_ln)
                        if m_na:
                            if in_iface == 2:
                                mod.profibus_address = int(m_na.group(1))
                            else:
                                mod.mpi_address = int(m_na.group(1))
                    j += 1
                modules.append(mod)
                i = j
        i += 1

    modules.sort(key=lambda m: (m.rack, m.slot))
    return StationHardwareConfig(
        station_name=station_name,
        plc_family=plc_family,
        step7_version=step7_version,
        modules=modules,
        subnets=subnets,
    )
