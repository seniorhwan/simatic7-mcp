"""
Persistent Sub-15ms 32-Bit COM & S7-PLCSIM Worker Daemon for Siemens STEP 7 Classic.

Eliminates the ~1,920 ms SysWOW64 PowerShell cold-spawn tax by maintaining a single
long-lived 32-bit STA COM worker communicating via newline-delimited JSON over stdio pipes.

Transport preference:
  1. Native 32-bit C++ worker binary (native/s7_com_bridge.exe) if compiled and present.
  2. Zero-dependency persistent 32-bit SysWOW64 PowerShell COM daemon (works out-of-the-box
     on every Windows STEP 7 Classic workstation with no compiler required).

Supports:
  - Simatic.Simatic (S7ABATCX.DLL) in UnattendedServerMode:
      ping, list_projects, create_project, browse_project_tree, list_blocks,
      export_block, compile_source, copy_block, delete_block,
      import_station_cfg, export_station_cfg, export_symbols, import_symbols,
      download_blocks, upload_station, open_block
  - S7wspsmx.S7ProSim.1 (S7-PLCSIM):
      plcsim_connect, plcsim_disconnect, plcsim_get_state, plcsim_set_state,
      plcsim_set_scan_mode, plcsim_step_scan, plcsim_read, plcsim_write,
      plcsim_batch_read, plcsim_batch_write, plcsim_save_plc, plcsim_load_plc
"""

from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


_PERSISTENT_PS32_DAEMON_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$script:S7 = $null
$script:PSim = $null
$script:PSimConnected = $false

function Get-S7 {
  if ($null -eq $script:S7) {
    $script:S7 = New-Object -ComObject "Simatic.Simatic"
    try { $script:S7.UnattendedServerMode = $true } catch {}
  }
  return $script:S7
}

function Get-PSim {
  if ($null -eq $script:PSim) {
    $script:PSim = New-Object -ComObject "S7wspsmx.S7ProSim.1"
  }
  return $script:PSim
}

function Get-PgPcInterface {
  $info = @{
    detected = $false
    access_point = 'S7ONLINE'
    device_name = ''
    registry_path = ''
    interface_type = 'unknown'
    is_plcsim = $false
    is_tcpip = $false
    is_mpi_profibus = $false
  }
  $regPaths = @(
    'HKLM:\SOFTWARE\WOW6432Node\Siemens\SINEC\LogNames\S7ONLINE',
    'HKLM:\SOFTWARE\Siemens\SINEC\LogNames\S7ONLINE'
  )
  foreach ($rp in $regPaths) {
    try {
      if (Test-Path $rp) {
        $props = Get-ItemProperty -Path $rp -ErrorAction Stop
        $dev = ''
        if ($props.PSObject.Properties['DeviceName'] -and $props.DeviceName) {
          $dev = [string]$props.DeviceName
        } elseif ($props.PSObject.Properties['(default)'] -and $props.'(default)') {
          $dev = [string]$props.'(default)'
        } elseif ($props.PSObject.Properties['LogName'] -and $props.LogName) {
          $dev = [string]$props.LogName
        }
        if ($dev -ne '') {
          $info.detected = $true
          $info.device_name = $dev
          $info.registry_path = $rp
          $up = $dev.ToUpper()
          if ($up -match 'PLCSIM') {
            $info.is_plcsim = $true
            $info.interface_type = 'plcsim'
          } elseif ($up -match 'TCPIP|TCP/IP|ISO|PROFINET|ETH|NIC') {
            $info.is_tcpip = $true
            $info.interface_type = 'tcpip_iso'
          } elseif ($up -match 'MPI|PROFIBUS|PC ADAPTER|CP5|CP56|CP57') {
            $info.is_mpi_profibus = $true
            $info.interface_type = 'mpi_profibus'
          }
          break
        }
      }
    } catch {}
  }
  return $info
}

function Find-ProjectAndProgram([string]$targetProj) {
  $s7 = Get-S7
  $matchedProj = $null
  $matchedPrg = $null
  $matchedStation = $null
  $srcContainer = $null
  $blkContainer = $null

  foreach ($p in $s7.Projects) {
    try {
      $pType = [int]$p.Type
      $isUser = ($pType -eq 1122305 -or $pType -eq 1)
      if ($targetProj -ne '' -and $p.Name -ine $targetProj) { continue }
      if ($targetProj -eq '' -and -not $isUser) { continue }

      foreach ($candidatePrg in $p.Programs) {
        $foundSrc = $null
        $foundBlk = $null
        foreach ($c in $candidatePrg.Next) {
          if ($c.ConcreteType -eq 1122308 -or $c.Name -ieq 'Sources' -or $c.Name -ieq 'Quellen') {
            $foundSrc = $c
          } elseif ($c.ConcreteType -eq 1138689 -or $c.Name -ieq 'Blocks' -or $c.Name -ieq 'Bausteine') {
            $foundBlk = $c
          }
        }
        if ($null -ne $foundBlk -or $null -ne $foundSrc) {
          $matchedProj = $p
          $matchedPrg = $candidatePrg
          $srcContainer = $foundSrc
          $blkContainer = $foundBlk
          try {
            if ($null -ne $candidatePrg.Parent -and $null -ne $candidatePrg.Parent.Parent) {
              $matchedStation = $candidatePrg.Parent.Parent
            }
          } catch {}
          if ($null -eq $matchedStation) {
            try {
              foreach ($st in $p.Stations) {
                $matchedStation = $st
                break
              }
            } catch {}
          }
          break
        }
      }
      if ($null -ne $matchedProj) { break }
    } catch {}
  }
  return @{
    project = $matchedProj
    station = $matchedStation
    program = $matchedPrg
    sources = $srcContainer
    blocks  = $blkContainer
  }
}

function Find-UserProject([string]$targetProj) {
  $s7 = Get-S7
  foreach ($p in $s7.Projects) {
    try {
      $pType = [int]$p.Type
      $isUser = ($pType -eq 1122305 -or $pType -eq 1)
      if ($targetProj -ne '' -and $p.Name -ine $targetProj) { continue }
      if ($targetProj -eq '' -and -not $isUser) { continue }
      return $p
    } catch {}
  }
  return $null
}

function Handle-Command($req) {
  $cmd = [string]$req.cmd

  if ($cmd -eq 'ping') {
    return @{ ok = $true; worker = 'ps32_persistent'; pid = $PID }
  }
  if ($cmd -eq 'get_pgpc_interface') {
    $pgpc = Get-PgPcInterface
    return @{ ok = $true; pgpc_interface = $pgpc }
  }
  if ($cmd -eq 'shutdown') {
    return @{ ok = $true; shutting_down = $true }
  }

  if ($cmd -eq 'list_projects') {
    $s7 = Get-S7
    $projs = @()
    foreach ($p in $s7.Projects) {
      try {
        $pType = [int]$p.Type
        $projs += @{
          name = [string]$p.Name
          type = $pType
          is_user_project = [bool]($pType -eq 1122305 -or $pType -eq 1)
          project_dir = [string]$p.LogPath
        }
      } catch {}
    }
    return @{ ok = $true; projects = $projs }
  }

  if ($cmd -eq 'create_project') {
    $s7 = Get-S7
    $pName = [string]$req.project_name
    $pDir = if ($req.project_dir) { [string]$req.project_dir } elseif ($req.project_path) { [string]$req.project_path } else { '' }
    if (-not $pName) { return @{ ok = $false; error = 'project_name is required.' } }
    foreach ($existing in $s7.Projects) {
      try {
        if ($existing.Name -ieq $pName) {
          return @{
            ok = $false
            failure_category = 'already_exists'
            error = "Project '$pName' already exists in STEP 7."
            project_name = [string]$existing.Name
            project_dir = [string]$existing.LogPath
          }
        }
      } catch {}
    }
    if ($pDir) {
      $proj = $s7.Projects.Add($pName, $pDir, 1)
    } else {
      $proj = $s7.Projects.Add($pName)
    }
    $createdStation = ''
    $stationError = $null
    if ($req.create_station -and $req.station_name) {
      $stType = ([string]$req.station_type).ToUpper()
      # S7StationType enum: S7300Station=30, S7400Station=31, S7PCStation=32
      $stCode = if ($stType -match '400') { 31 } elseif ($stType -match 'WIN|PC') { 32 } else { 30 }
      try {
        $st = $proj.Stations.Add([string]$req.station_name, $stCode)
        $createdStation = [string]$st.Name
      } catch {
        $stationError = $_.Exception.Message
      }
    }
    return @{
      ok = $true
      project_name = [string]$proj.Name
      project_dir = [string]$proj.LogPath
      station_name = $createdStation
      station_error = $stationError
    }
  }

  if ($cmd -eq 'browse_project_tree') {
    $s7 = Get-S7
    $targetProj = [string]$req.project_name
    $tree = @()
    foreach ($p in $s7.Projects) {
      try {
        $pType = [int]$p.Type
        if ($targetProj -ne '' -and $p.Name -ine $targetProj) { continue }
        if ($targetProj -eq '' -and -not ($pType -eq 1122305 -or $pType -eq 1)) { continue }
        $stations = @()
        try {
          foreach ($st in $p.Stations) {
            $stations += @{ name = [string]$st.Name; type = [string]$st.Type }
          }
        } catch {}
        $programs = @()
        try {
          foreach ($prg in $p.Programs) {
            $containers = @()
            foreach ($c in $prg.Next) {
              $itemCount = 0
              try { $itemCount = $c.Next.Count } catch {}
              $containers += @{ name = [string]$c.Name; concrete_type = [int]$c.ConcreteType; item_count = $itemCount }
            }
            $hasSyms = $false
            try { $hasSyms = ($null -ne $prg.SymbolTable) } catch {}
            $programs += @{ name = [string]$prg.Name; symbol_table_available = $hasSyms; containers = $containers }
          }
        } catch {}
        $tree += @{
          name = [string]$p.Name
          project_dir = [string]$p.LogPath
          stations = $stations
          programs = $programs
        }
        if ($targetProj -ne '') { break }
      } catch {}
    }
    $firstName = if ($tree.Count -gt 0) { $tree[0].name } else { $targetProj }
    return @{ ok = $true; project_name = $firstName; projects = $tree }
  }

  if ($cmd -eq 'list_blocks') {
    $ctx = Find-ProjectAndProgram ([string]$req.project_name)
    if ($null -eq $ctx.project -or $null -eq $ctx.program) {
      return @{ ok = $false; error = "Project not found in STEP 7 COM catalog: $($req.project_name)" }
    }
    $blocks = @()
    $sources = @()
    if ($null -ne $ctx.blocks) {
      foreach ($b in $ctx.blocks.Next) {
        try {
          $bName = [string]$b.Name
          $cType = [int]$b.ConcreteType
          $bLang = [string]$b.Language
          if ($cType -eq 1138947 -or $bName -match '^\s*DB\s*\d+$') {
            $bLang = 'DB'
          } elseif ($cType -eq 1138955 -or $bName -match '^\s*SDB\s*\d*$') {
            $bLang = 'SDB'
          } elseif ($bName -match '^\s*UDT\s*\d+$') {
            $bLang = 'UDT'
          }
          $blocks += @{
            name = $bName
            concrete_type = $cType
            language = $bLang
            author = [string]$b.Creator
            symbolic_name = [string]$b.SymbolicName
            size = [int]$b.Size
          }
        } catch {}
      }
    }
    if ($null -ne $ctx.sources) {
      foreach ($src in $ctx.sources.Next) {
        try {
          $sources += @{ name = [string]$src.Name; file_path = [string]$src.FilePath }
        } catch {}
      }
    }
    return @{
      ok = $true
      project_name = [string]$ctx.project.Name
      project_dir = [string]$ctx.project.LogPath
      program_name = [string]$ctx.program.Name
      blocks = $blocks
      sources = $sources
    }
  }

  if ($cmd -eq 'export_block') {
    $ctx = Find-ProjectAndProgram ([string]$req.project_name)
    if ($null -eq $ctx.blocks) {
      return @{ ok = $false; error = "Blocks container not found for project '$($req.project_name)'." }
    }
    $blkClean = ([string]$req.block_name) -replace '\s+', ''
    $outFile = [string]$req.output_path
    $found = $null
    foreach ($b in $ctx.blocks.Next) {
      if (($b.Name -replace '\s+', '') -ieq $blkClean) { $found = $b; break }
    }
    if ($null -eq $found) {
      return @{ ok = $false; error = "Block '$blkClean' not found in project '$($ctx.project.Name)'." }
    }
    $null = $found.GenerateSource($outFile, 0)
    Get-Process S7kafapx -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -eq 0 } | Stop-Process -Force -ErrorAction SilentlyContinue
    return @{
      ok = [bool](Test-Path -LiteralPath $outFile)
      project_name = [string]$ctx.project.Name
      block_name = [string]$found.Name
      output_path = $outFile
    }
  }

  if ($cmd -eq 'compile_source') {
    $ctx = Find-ProjectAndProgram ([string]$req.project_name)
    if ($null -eq $ctx.sources) {
      return @{ ok = $false; error = "Sources container not found for project '$($req.project_name)'." }
    }
    $srcPath = [string]$req.source_path
    $srcName = [string]$req.source_name
    $ext = [System.IO.Path]::GetExtension($srcPath).ToLower()
    $srcType = if ($req.source_type) {
      [int]$req.source_type
    } elseif ($ext -eq '.scl') {
      66
    } elseif ($ext -eq '.gr7') {
      67
    } else {
      65
    }
    $altType = if ($srcType -eq 66) { 1122310 } elseif ($srcType -eq 67) { 1122311 } else { 1122309 }

    $targetItem = $null
    foreach ($it in $ctx.sources.Next) {
      if ($it.Name -ieq $srcName) { $targetItem = $it; break }
    }
    if ($null -eq $targetItem) {
      try { $targetItem = $ctx.sources.Next.Add($srcName, $srcType, $srcPath) }
      catch {
        try { $targetItem = $ctx.sources.Next.Add($srcName, $altType, $srcPath) }
        catch { $targetItem = $ctx.sources.Next.Add($srcName, 1122309, $srcPath) }
      }
    }
    if ($targetItem.FilePath -and (Test-Path -LiteralPath (Split-Path -Parent $targetItem.FilePath))) {
      Copy-Item -LiteralPath $srcPath -Destination $targetItem.FilePath -Force
    }
    $null = $targetItem.Compile()
    Get-Process S7kafapx -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -eq 0 } | Stop-Process -Force -ErrorAction SilentlyContinue
    return @{
      ok = $true
      compile_code = 0
      source_type = $srcType
      project_name = [string]$ctx.project.Name
      project_dir = [string]$ctx.project.LogPath
      source_name = [string]$targetItem.Name
      internal_file = [string]$targetItem.FilePath
    }
  }

  if ($cmd -eq 'delete_block') {
    $ctx = Find-ProjectAndProgram ([string]$req.project_name)
    if ($null -eq $ctx.blocks) { return @{ ok = $false; error = 'Blocks container not found.' } }
    $blkClean = ([string]$req.block_name) -replace '\s+', ''
    foreach ($b in $ctx.blocks.Next) {
      if (($b.Name -replace '\s+', '') -ieq $blkClean) {
        $null = $b.Remove()
        return @{ ok = $true; project_name = [string]$ctx.project.Name; deleted = $blkClean }
      }
    }
    return @{ ok = $false; error = "Block '$blkClean' not found." }
  }

  if ($cmd -eq 'copy_block') {
    $srcProjName = if ($req.source_project) { [string]$req.source_project } else { [string]$req.project_name }
    $dstProjName = if ($req.target_project) { [string]$req.target_project } else { $srcProjName }
    $srcBlkRaw = if ($req.source_block) { [string]$req.source_block } else { [string]$req.block_name }
    $dstBlkRaw = if ($req.target_block) { [string]$req.target_block } else { $srcBlkRaw }
    $srcCtx = Find-ProjectAndProgram $srcProjName
    $dstCtx = Find-ProjectAndProgram $dstProjName
    if ($null -eq $srcCtx.blocks -or $null -eq $dstCtx.blocks) {
      return @{ ok = $false; error = 'Source or target Blocks container not found.' }
    }
    $blkClean = ($srcBlkRaw -replace '\s+', '').ToUpper()
    $dstClean = ($dstBlkRaw -replace '\s+', '').ToUpper()
    $srcObj = $null
    foreach ($b in $srcCtx.blocks.Next) {
      if (($b.Name -replace '\s+', '') -ieq $blkClean) { $srcObj = $b; break }
    }
    if ($null -eq $srcObj) {
      return @{ ok = $false; error = "Block '$blkClean' not found in source project '$($srcCtx.project.Name)'." }
    }
    # Safe non-modal copy via GenerateSource + header rewrite + Compile (avoids SIMATIC Manager overwrite modal hang)
    if ($null -ne $dstCtx.sources) {
      $tmpAwl = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), "mcp_copy_$([guid]::NewGuid().ToString('N')).awl")
      try {
        $null = $srcObj.GenerateSource($tmpAwl, 0)
        Get-Process S7kafapx -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -eq 0 } | Stop-Process -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $tmpAwl) {
          $srcText = [System.IO.File]::ReadAllText($tmpAwl, [System.Text.Encoding]::Default)
          if ($dstClean -ne '' -and $dstClean -ine $blkClean) {
            $mDst = [regex]::Match($dstClean, '^(OB|FC|FB|DB|UDT)(\d+)$')
            if ($mDst.Success) {
              $dstKind = $mDst.Groups[1].Value
              $dstNum = $mDst.Groups[2].Value
              $dstSpaced = "$dstKind $dstNum"
              $srcText = [regex]::Replace(
                $srcText,
                '(?im)^(\s*(?:ORGANIZATION_BLOCK|FUNCTION_BLOCK|FUNCTION|DATA_BLOCK|TYPE)\s+)(?:"[^"]+"|(?:OB|FC|FB|DB|UDT)\s*\d+)',
                "`${1}$dstSpaced",
                1
              )
            }
          }
          [System.IO.File]::WriteAllText($tmpAwl, $srcText, [System.Text.Encoding]::Default)
          $tmpSrcName = "MCP_COPY_$dstClean"
          $srcItem = $null
          foreach ($it in $dstCtx.sources.Next) {
            if ($it.Name -ieq $tmpSrcName) { $srcItem = $it; break }
          }
          if ($null -eq $srcItem) {
            try { $srcItem = $dstCtx.sources.Next.Add($tmpSrcName, 65, $tmpAwl) }
            catch { $srcItem = $dstCtx.sources.Next.Add($tmpSrcName, 1122309, $tmpAwl) }
          }
          if ($srcItem.FilePath -and (Test-Path -LiteralPath (Split-Path -Parent $srcItem.FilePath))) {
            Copy-Item -LiteralPath $tmpAwl -Destination $srcItem.FilePath -Force
          }
          $null = $srcItem.Compile()
          try { $null = $srcItem.Remove() } catch {}
          Get-Process S7kafapx -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -eq 0 } | Stop-Process -Force -ErrorAction SilentlyContinue
          return @{
            ok = $true
            method = 'generate_source_and_compile'
            source_block = $blkClean
            target_block = $dstClean
            source_project = [string]$srcCtx.project.Name
            target_project = [string]$dstCtx.project.Name
          }
        }
      } finally {
        if (Test-Path -LiteralPath $tmpAwl) { Remove-Item -LiteralPath $tmpAwl -Force -ErrorAction SilentlyContinue }
      }
    }
    # Fallback only when cross-project copy with no Sources container
    $copiedObj = $srcObj.Copy($dstCtx.blocks)
    if ($dstClean -ne '' -and $dstClean -ine $blkClean -and $null -ne $copiedObj) {
      try { $copiedObj.Name = $dstClean } catch {}
    }
    return @{
      ok = $true
      method = 'com_copy'
      source_block = $blkClean
      target_block = $dstClean
      source_project = [string]$srcCtx.project.Name
      target_project = [string]$dstCtx.project.Name
    }
  }

  if ($cmd -eq 'import_station_cfg') {
    $targetProj = [string]$req.project_name
    $cfgPath = [string]$req.cfg_path
    $proj = Find-UserProject $targetProj
    if ($null -eq $proj) { return @{ ok = $false; error = "User project '$targetProj' not found." } }
    $st = $proj.Stations.Import($cfgPath)
    $progs = @()
    try { foreach ($prg in $proj.Programs) { $progs += [string]$prg.Name } } catch {}
    return @{
      ok = $true
      project_name = [string]$proj.Name
      station_name = [string]$st.Name
      programs = $progs
      cfg_path = $cfgPath
    }
  }

  if ($cmd -eq 'export_station_cfg') {
    $targetProj = [string]$req.project_name
    $stationName = [string]$req.station_name
    $cfgPath = if ($req.cfg_path) { [string]$req.cfg_path } else { [string]$req.output_path }
    if (-not $cfgPath) { return @{ ok = $false; error = 'cfg_path / output_path is required.' } }
    $proj = Find-UserProject $targetProj
    if ($null -eq $proj) { return @{ ok = $false; error = "User project '$targetProj' not found." } }
    $st = $null
    foreach ($cand in $proj.Stations) {
      if ($stationName -eq '' -or $cand.Name -ieq $stationName) { $st = $cand; break }
    }
    if ($null -eq $st) { return @{ ok = $false; error = "Station '$stationName' not found." } }
    $null = $st.Export($cfgPath)
    return @{ ok = [bool](Test-Path -LiteralPath $cfgPath); project_name = [string]$proj.Name; station_name = [string]$st.Name; cfg_path = $cfgPath }
  }

  if ($cmd -eq 'export_symbols') {
    $ctx = Find-ProjectAndProgram ([string]$req.project_name)
    if ($null -eq $ctx.program -or $null -eq $ctx.program.SymbolTable) {
      return @{ ok = $false; error = 'SymbolTable not found in STEP 7 project.' }
    }
    $outFile = [string]$req.output_path
    try {
      $null = $ctx.program.SymbolTable.Export($outFile)
    } catch {
      $null = $ctx.program.SymbolTable.Export($outFile, 0)
    }
    return @{ ok = [bool](Test-Path -LiteralPath $outFile); project_name = [string]$ctx.project.Name; output_path = $outFile }
  }

  if ($cmd -eq 'import_symbols') {
    $ctx = Find-ProjectAndProgram ([string]$req.project_name)
    if ($null -eq $ctx.program -or $null -eq $ctx.program.SymbolTable) {
      return @{ ok = $false; error = 'SymbolTable not found in STEP 7 project.' }
    }
    $inFile = [string]$req.input_path
    try {
      $null = $ctx.program.SymbolTable.Import($inFile, 1)
    } catch {
      $null = $ctx.program.SymbolTable.Import($inFile)
    }
    return @{ ok = $true; project_name = [string]$ctx.project.Name }
  }

  if ($cmd -eq 'download_blocks') {
    $pgpc = Get-PgPcInterface
    $ctx = Find-ProjectAndProgram ([string]$req.project_name)
    if ($null -eq $ctx.program) {
      return @{
        ok = $false
        error = 'Program not found for download.'
        pgpc_interface = $pgpc
      }
    }
    # Siemens STEP 7 Command Interface S7OverwriteMode enum:
    #   S7AskUser = 0 (BLOCKS headless COM with a modal dialog!)
    #   S7OverwriteAll = 1
    #   S7DoNotOverwrite = 2
    $mode = if ($null -eq $req.overwrite -or [bool]$req.overwrite) { 1 } else { 2 }
    $targetScope = if ($req.download_target) { ([string]$req.download_target).Trim().ToLower() } else { 'blocks' }
    $reqBlocks = @()
    if ($null -ne $req.blocks) {
      foreach ($rb in $req.blocks) {
        $c = ([string]$rb) -replace '\s+', ''
        if ($c -ne '') { $reqBlocks += $c.ToUpper() }
      }
    }

    $hint = if ($pgpc.is_plcsim) {
      "Windows PG/PC Interface S7ONLINE is currently set to '$($pgpc.device_name)' (PLCSIM). For a physical PLC, open 'Set PG/PC Interface' in Windows Control Panel and set S7ONLINE to TCP/IP (Auto) or your MPI/PROFIBUS PC Adapter."
    } else {
      "Verify: (1) S7ONLINE PG/PC Interface matches your physical PLC cable/NIC (currently '$($pgpc.device_name)'), (2) Hardware Config station IP/MPI address matches the CPU, (3) CPU keyswitch is in RUN-P or STOP (not RUN-only), and (4) blocks are compiled."
    }

    if ($targetScope -eq 'station') {
      if ($null -eq $ctx.station) {
        return @{
          ok = $false
          scope = 'station'
          error = 'No SIMATIC Station found in project for full station (HW + blocks) download. Import hardware configuration first or use download_target="blocks".'
          pgpc_interface = $pgpc
          troubleshooting_hint = $hint
        }
      }
      try {
        $null = $ctx.station.Download($mode)
        return @{
          ok = $true
          scope = 'station'
          method = 'station.Download'
          project_name = [string]$ctx.project.Name
          station_name = [string]$ctx.station.Name
          program_name = [string]$ctx.program.Name
          overwrite_mode = $mode
          pgpc_interface = $pgpc
        }
      } catch {
        return @{
          ok = $false
          scope = 'station'
          project_name = [string]$ctx.project.Name
          station_name = [string]$ctx.station.Name
          error = "Station download failed: $($_.Exception.Message)"
          pgpc_interface = $pgpc
          troubleshooting_hint = $hint
        }
      }
    }

    if ($reqBlocks.Count -eq 0 -or ($reqBlocks -contains '*') -or ($reqBlocks -contains 'ALL')) {
      $attemptErrors = @()
      if ($targetScope -eq 'blocks' -and $null -ne $ctx.blocks) {
        try {
          $null = $ctx.blocks.Download($mode)
          return @{
            ok = $true
            scope = 'all_blocks'
            method = 'blocks.Download'
            project_name = [string]$ctx.project.Name
            program_name = [string]$ctx.program.Name
            overwrite_mode = $mode
            pgpc_interface = $pgpc
          }
        } catch {
          $attemptErrors += "blocks.Download: $($_.Exception.Message)"
        }
      }
      try {
        $null = $ctx.program.Download($mode)
        return @{
          ok = $true
          scope = 'full_program'
          method = 'program.Download'
          project_name = [string]$ctx.project.Name
          program_name = [string]$ctx.program.Name
          overwrite_mode = $mode
          pgpc_interface = $pgpc
          fallback_notes = $attemptErrors
        }
      } catch {
        $attemptErrors += "program.Download: $($_.Exception.Message)"
      }
      # Final fallback: per-block download so partial progress & exact failing block are reported
      if ($null -ne $ctx.blocks) {
        $dlOk = @()
        $dlFail = @()
        foreach ($b in $ctx.blocks.Next) {
          $bName = [string]$b.Name
          if ($bName -match '^(SFC|SFB)\d+$') { continue }
          try {
            $null = $b.Download($mode)
            $dlOk += $bName
          } catch {
            $dlFail += @{ block = $bName; error = $_.Exception.Message }
          }
        }
        if ($dlOk.Count -gt 0 -and $dlFail.Count -eq 0) {
          return @{
            ok = $true
            scope = 'all_blocks_iterated'
            method = 'per_block.Download'
            project_name = [string]$ctx.project.Name
            program_name = [string]$ctx.program.Name
            downloaded_blocks = $dlOk
            overwrite_mode = $mode
            pgpc_interface = $pgpc
            fallback_notes = $attemptErrors
          }
        }
        return @{
          ok = $false
          scope = 'all_blocks_iterated'
          project_name = [string]$ctx.project.Name
          program_name = [string]$ctx.program.Name
          downloaded_blocks = $dlOk
          failed_blocks = $dlFail
          error = ($attemptErrors -join ' | ')
          pgpc_interface = $pgpc
          troubleshooting_hint = $hint
        }
      }
      return @{
        ok = $false
        scope = 'full_program'
        project_name = [string]$ctx.project.Name
        program_name = [string]$ctx.program.Name
        error = ($attemptErrors -join ' | ')
        pgpc_interface = $pgpc
        troubleshooting_hint = $hint
      }
    }

    if ($null -eq $ctx.blocks) {
      return @{
        ok = $false
        error = 'Blocks container not found for selective download.'
        pgpc_interface = $pgpc
      }
    }
    $downloaded = @()
    $missing = @()
    $failed = @()
    foreach ($want in $reqBlocks) {
      $hit = $null
      foreach ($b in $ctx.blocks.Next) {
        if (($b.Name -replace '\s+', '') -ieq $want) { $hit = $b; break }
      }
      if ($null -ne $hit) {
        try {
          $null = $hit.Download($mode)
          $downloaded += [string]$hit.Name
        } catch {
          $failed += @{ block = [string]$hit.Name; error = $_.Exception.Message }
        }
      } else {
        $missing += $want
      }
    }
    $errParts = @()
    if ($missing.Count -gt 0) { $errParts += "Blocks not found: $($missing -join ', ')" }
    if ($failed.Count -gt 0) {
      $fNames = ($failed | ForEach-Object { "$($_.block): $($_.error)" }) -join '; '
      $errParts += "COM download errors: $fNames"
    }
    return @{
      ok = ($downloaded.Count -gt 0 -and $missing.Count -eq 0 -and $failed.Count -eq 0)
      scope = 'selective_blocks'
      project_name = [string]$ctx.project.Name
      program_name = [string]$ctx.program.Name
      overwrite_mode = $mode
      downloaded_blocks = $downloaded
      missing_blocks = $missing
      failed_blocks = $failed
      pgpc_interface = $pgpc
      troubleshooting_hint = if ($errParts.Count -gt 0) { $hint } else { $null }
      error = if ($errParts.Count -gt 0) { $errParts -join ' | ' } else { $null }
    }
  }

  if ($cmd -eq 'upload_station') {
    $targetProj = [string]$req.project_name
    $proj = Find-UserProject $targetProj
    if ($null -eq $proj) { return @{ ok = $false; error = "User project '$targetProj' not found." } }
    $rack = if ($null -ne $req.rack) { [int]$req.rack } else { 0 }
    $slot = if ($null -ne $req.slot) { [int]$req.slot } else { 2 }
    $addr = if ($req.address) { [string]$req.address } else { '2' }
    $subType = if ($req.subnet_type) { [string]$req.subnet_type } else { 'MPI' }
    $rc = $proj.UploadStationEx($rack, $slot, $addr, $subType, 0)
    return @{
      ok = ($rc -eq 0)
      return_code = $rc
      project_name = [string]$proj.Name
      rack = $rack
      slot = $slot
      address = $addr
      subnet_type = $subType
    }
  }

  if ($cmd -eq 'open_block') {
    $ctx = Find-ProjectAndProgram ([string]$req.project_name)
    if ($null -eq $ctx.blocks) { return @{ ok = $false; error = 'Blocks container not found.' } }
    $blkClean = ([string]$req.block_name) -replace '\s+', ''
    foreach ($b in $ctx.blocks.Next) {
      if (($b.Name -replace '\s+', '') -ieq $blkClean) {
        $b.Edit()
        return @{ ok = $true; project_name = [string]$ctx.project.Name; block_name = [string]$b.Name }
      }
    }
    return @{ ok = $false; error = "Block '$blkClean' not found." }
  }

  # ================= S7-PLCSIM (S7ProSim COM) =================
  if ($cmd -eq 'plcsim_connect') {
    $psim = Get-PSim
    $psim.Connect()
    $script:PSimConnected = $true
    return @{ ok = $true; connected = $true; cpu_state = [string]$psim.GetState(); scan_mode = [string]$psim.GetScanMode() }
  }

  if ($cmd -eq 'plcsim_disconnect') {
    $psim = Get-PSim
    $psim.Disconnect()
    $script:PSimConnected = $false
    return @{ ok = $true; connected = $false }
  }

  if ($cmd -eq 'plcsim_get_state') {
    $psim = Get-PSim
    if (-not $script:PSimConnected) { try { $psim.Connect(); $script:PSimConnected = $true } catch {} }
    return @{ ok = $true; cpu_state = [string]$psim.GetState(); scan_mode = [string]$psim.GetScanMode() }
  }

  if ($cmd -eq 'plcsim_set_state') {
    $psim = Get-PSim
    if (-not $script:PSimConnected) { try { $psim.Connect(); $script:PSimConnected = $true } catch {} }
    $st = ([string]$req.state).ToUpper()
    $psim.SetState($st)
    return @{ ok = $true; cpu_state = [string]$psim.GetState() }
  }

  if ($cmd -eq 'plcsim_set_scan_mode') {
    $psim = Get-PSim
    if (-not $script:PSimConnected) { try { $psim.Connect(); $script:PSimConnected = $true } catch {} }
    $rawMode = if ($req.scan_mode) { [string]$req.scan_mode } else { [string]$req.mode }
    $modeVal = if ($rawMode.ToUpper() -match 'SINGLE') { 0 } else { 1 }
    $psim.SetScanMode($modeVal)
    return @{ ok = $true; scan_mode = [string]$psim.GetScanMode() }
  }

  if ($cmd -eq 'plcsim_step_scan') {
    $psim = Get-PSim
    if (-not $script:PSimConnected) { try { $psim.Connect(); $script:PSimConnected = $true } catch {} }
    $count = [Math]::Max(1, [int]$req.cycles)
    for ($k = 0; $k -lt $count; $k++) {
      $psim.ExecuteSingleScan()
    }
    return @{ ok = $true; executed_cycles = $count; cpu_state = [string]$psim.GetState() }
  }

  if ($cmd -eq 'plcsim_save_plc') {
    $psim = Get-PSim
    $psim.SavePLC([string]$req.file_path)
    return @{ ok = $true; file_path = [string]$req.file_path }
  }

  if ($cmd -eq 'plcsim_load_plc') {
    $psim = Get-PSim
    $psim.LoadPLC([string]$req.file_path)
    return @{ ok = $true; file_path = [string]$req.file_path; cpu_state = [string]$psim.GetState() }
  }

  if ($cmd -eq 'plcsim_read') {
    $psim = Get-PSim
    if (-not $script:PSimConnected) { try { $psim.Connect(); $script:PSimConnected = $true } catch {} }
    $area = ([string]$req.area).ToUpper()
    $byteIdx = [int]$req.byte_offset
    $bitIdx = [int]$req.bit_offset
    $dbNum = [int]$req.db_number
    $dsize = ([string]$req.data_size).ToUpper()
    $dtypeConst = switch ($dsize) {
      'BIT'   { 1 }
      'BYTE'  { 2 }
      'WORD'  { 3 }
      'INT'   { 3 }
      default { 4 }
    }
    $v = $null
    if ($area -eq 'I') {
      $psim.ReadInputPoint($byteIdx, $bitIdx, $dtypeConst, [ref]$v)
    } elseif ($area -eq 'Q') {
      $psim.ReadOutputPoint($byteIdx, $bitIdx, $dtypeConst, [ref]$v)
    } elseif ($area -eq 'M') {
      $psim.ReadFlagValue($byteIdx, $bitIdx, $dtypeConst, [ref]$v)
    } elseif ($area -eq 'DB') {
      $psim.ReadDataBlockValue($dbNum, $byteIdx, $bitIdx, $dtypeConst, [ref]$v)
    } else {
      return @{ ok = $false; error = "Unsupported PLCSIM area '$area'." }
    }
    if ($dsize -eq 'REAL' -and $null -ne $v) {
      $bytes = [BitConverter]::GetBytes([int]$v)
      $v = [Math]::Round([BitConverter]::ToSingle($bytes, 0), 6)
    }
    return @{ ok = $true; value = $v; cpu_state = [string]$psim.GetState() }
  }

  if ($cmd -eq 'plcsim_write') {
    $psim = Get-PSim
    if (-not $script:PSimConnected) { try { $psim.Connect(); $script:PSimConnected = $true } catch {} }
    $area = ([string]$req.area).ToUpper()
    $byteIdx = [int]$req.byte_offset
    $bitIdx = [int]$req.bit_offset
    $dbNum = [int]$req.db_number
    $dsize = ([string]$req.data_size).ToUpper()
    if ($dsize -eq 'BIT') {
      $valObj = [object]([bool]$req.value)
    } elseif ($dsize -eq 'REAL') {
      $fBytes = [BitConverter]::GetBytes([single]$req.value)
      $valObj = [object]([BitConverter]::ToInt32($fBytes, 0))
    } elseif ($dsize -eq 'BYTE') {
      $valObj = [object]([byte]$req.value)
    } elseif ($dsize -eq 'WORD' -or $dsize -eq 'INT') {
      $valObj = [object]([int16]$req.value)
    } else {
      $valObj = [object]([int32]$req.value)
    }
    if ($area -eq 'I') {
      $psim.WriteInputPoint($byteIdx, $bitIdx, [ref]$valObj)
    } elseif ($area -eq 'M') {
      $psim.WriteFlagValue($byteIdx, $bitIdx, [ref]$valObj)
    } elseif ($area -eq 'DB') {
      $psim.WriteDataBlockValue($dbNum, $byteIdx, $bitIdx, [ref]$valObj)
    } else {
      return @{ ok = $false; error = "PLCSIM S7ProSim cannot write directly to area '$area'." }
    }
    return @{ ok = $true; cpu_state = [string]$psim.GetState() }
  }

  if ($cmd -eq 'plcsim_batch_read') {
    $results = @()
    foreach ($it in $req.items) {
      $subReq = [PSCustomObject]@{
        cmd = 'plcsim_read'
        area = $it.area
        byte_offset = $it.byte_offset
        bit_offset = $it.bit_offset
        db_number = $it.db_number
        data_size = $it.data_size
      }
      $r = Handle-Command $subReq
      $results += @{
        address = [string]$it.address
        symbol = $it.symbol
        ok = [bool]$r.ok
        value = $r.value
        error = $r.error
      }
    }
    return @{ ok = $true; items = $results }
  }

  if ($cmd -eq 'plcsim_batch_write') {
    $results = @()
    foreach ($it in $req.items) {
      $subReq = [PSCustomObject]@{
        cmd = 'plcsim_write'
        area = $it.area
        byte_offset = $it.byte_offset
        bit_offset = $it.bit_offset
        db_number = $it.db_number
        data_size = $it.data_size
        value = $it.value
      }
      $r = Handle-Command $subReq
      $results += @{
        address = [string]$it.address
        symbol = $it.symbol
        ok = [bool]$r.ok
        written_value = $it.value
        error = $r.error
      }
    }
    return @{ ok = $true; items = $results }
  }

  return @{ ok = $false; error = "Unknown COM worker command '$cmd'." }
}

[Console]::Out.WriteLine((@{ ok = $true; ready = $true; pid = $PID } | ConvertTo-Json -Compress))
[Console]::Out.Flush()

while ($true) {
  $line = [Console]::In.ReadLine()
  if ($null -eq $line) { break }
  $trimmed = $line.Trim()
  if (-not $trimmed) { continue }
  try {
    $req = $trimmed | ConvertFrom-Json
    $resp = Handle-Command $req
    if ($resp -is [System.Array]) {
      $dictResp = $resp | Where-Object { $_ -is [hashtable] } | Select-Object -Last 1
      if ($null -ne $dictResp) { $resp = $dictResp }
    }
    $jsonOut = ($resp | ConvertTo-Json -Compress -Depth 8)
    [Console]::Out.WriteLine($jsonOut)
    [Console]::Out.Flush()
    if ($resp -is [hashtable] -and $resp.shutting_down) { break }
  } catch {
    $errOut = (@{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json -Compress)
    [Console]::Out.WriteLine($errOut)
    [Console]::Out.Flush()
  }
}
"""


class PersistentComWorker:
    """
    Thread-safe persistent 32-bit COM & S7ProSim worker manager.
    Maintains a single child process over stdin/stdout JSON-RPC lines with automatic
    restart-on-crash, timeout enforcement, and latency telemetry.
    """

    PS32_CANDIDATES = (
        Path(r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe"),
        Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"),
    )

    def __init__(
        self,
        native_exe_path: Optional[Path] = None,
        mock_handler: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        prefer_native: bool = True,
    ) -> None:
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._temp_script_path: Optional[Path] = None
        self._fallback_ps_worker: Optional["PersistentComWorker"] = None
        self._mock_handler = mock_handler
        self._prefer_native = prefer_native
        self._native_exe = native_exe_path or (Path(__file__).resolve().parents[1] / "native" / "s7_com_bridge.exe")
        self._ps32_exe: Optional[Path] = next((p for p in self.PS32_CANDIDATES if p.is_file()), None)
        self._backend_name: str = "unavailable"
        self._request_count: int = 0
        self._restart_count: int = 0
        atexit.register(self.stop)

    @property
    def backend_name(self) -> str:
        if self._mock_handler is not None:
            return "mock_worker"
        if self._prefer_native and sys.platform == "win32" and self._native_exe.is_file():
            return "native_cpp_x86"
        if sys.platform == "win32" and self._ps32_exe is not None:
            return "ps32_persistent_daemon"
        return "unavailable"

    def is_available(self) -> bool:
        return self.backend_name != "unavailable"

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            alive = self._proc is not None and self._proc.poll() is None
            return {
                "backend": self.backend_name,
                "available": self.is_available(),
                "running": alive or (self._mock_handler is not None),
                "pid": self._proc.pid if alive and self._proc else None,
                "request_count": self._request_count,
                "restart_count": self._restart_count,
                "native_bridge_present": self._native_exe.is_file(),
                "hybrid_fallback_active": self._fallback_ps_worker is not None,
            }

    def _start_locked(self) -> Tuple[bool, Optional[str]]:
        if self._proc is not None and self._proc.poll() is None:
            return True, None
        if not self.is_available():
            return False, "32-bit Windows COM host is not available on this platform."

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0
        if self._prefer_native and self._native_exe.is_file() and sys.platform == "win32":
            cmd = [str(self._native_exe)]
            self._backend_name = "native_cpp_x86"
        else:
            assert self._ps32_exe is not None
            # Write daemon script to a temp .ps1 file to avoid Windows 32,767-char CreateProcess limit ([WinError 206])
            fd, tmp_name = tempfile.mkstemp(prefix="s7_com_worker_", suffix=".ps1")
            os.close(fd)
            script_path = Path(tmp_name)
            script_path.write_text(_PERSISTENT_PS32_DAEMON_SCRIPT, encoding="utf-8-sig")
            self._temp_script_path = script_path
            cmd = [
                str(self._ps32_exe),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
            ]
            self._backend_name = "ps32_persistent_daemon"

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
            ready_line = self._readline_with_timeout(self._proc, timeout=12.0)
            if not ready_line:
                self._kill_locked()
                return False, "Persistent 32-bit COM worker did not send ready banner."
            banner = json.loads(ready_line)
            if not isinstance(banner, dict) or not banner.get("ok"):
                self._kill_locked()
                return False, f"COM worker failed startup: {banner}"
            return True, None
        except Exception as exc:
            self._kill_locked()
            return False, f"Failed to spawn persistent 32-bit COM worker: {exc}"

    @staticmethod
    def _readline_with_timeout(proc: subprocess.Popen, timeout: float) -> Optional[str]:
        result: List[Optional[str]] = [None]

        def _reader() -> None:
            try:
                if proc.stdout:
                    result[0] = proc.stdout.readline()
            except Exception:
                result[0] = None

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            return None
        return result[0]

    def _kill_locked(self) -> None:
        if self._proc is not None:
            try:
                if self._proc.poll() is None:
                    self._proc.terminate()
                    try:
                        self._proc.wait(timeout=1.5)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()
            except OSError:
                pass
            finally:
                self._proc = None
        if self._temp_script_path is not None:
            try:
                self._temp_script_path.unlink(missing_ok=True)
            except OSError:
                pass
            finally:
                self._temp_script_path = None

    def stop(self) -> None:
        with self._lock:
            if self._fallback_ps_worker is not None:
                try:
                    self._fallback_ps_worker.stop()
                except Exception:
                    pass
                self._fallback_ps_worker = None
            if self._proc is not None and self._proc.poll() is None:
                try:
                    if self._proc.stdin:
                        self._proc.stdin.write(json.dumps({"cmd": "shutdown"}) + "\n")
                        self._proc.stdin.flush()
                    self._proc.wait(timeout=1.5)
                except Exception:
                    pass
            self._kill_locked()

    def close(self) -> None:
        self.stop()

    def call(
        self,
        cmd: str,
        params: Optional[Dict[str, Any]] = None,
        timeout_s: float = 20.0,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"cmd": cmd}
        if params:
            payload.update(params)
        return self.request(payload, timeout=timeout_s)

    def _delegate_to_ps32_fallback(self, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        if self._fallback_ps_worker is None:
            self._fallback_ps_worker = PersistentComWorker(
                native_exe_path=self._native_exe,
                prefer_native=False,
            )
        return self._fallback_ps_worker.request(payload, timeout=timeout)

    def request(self, payload: Dict[str, Any], timeout: float = 20.0) -> Dict[str, Any]:
        """Executes a single serialized command against the persistent 32-bit COM worker."""
        t0 = time.perf_counter()
        with self._lock:
            self._request_count += 1
            if self._mock_handler is not None:
                res = dict(self._mock_handler(payload))
                if res.get("fallback_required") and self._fallback_ps_worker is not None:
                    return self._fallback_ps_worker.request(payload, timeout=timeout)
                res.setdefault("latency_ms", round((time.perf_counter() - t0) * 1000.0, 2))
                return res

            for attempt in range(2):
                ok_start, start_err = self._start_locked()
                if not ok_start or self._proc is None or not self._proc.stdin:
                    return {
                        "ok": False,
                        "error": start_err or "32-bit COM worker unavailable.",
                        "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                    }
                try:
                    self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                    self._proc.stdin.flush()
                    line = self._readline_with_timeout(self._proc, timeout=timeout)
                    if line is None:
                        self._kill_locked()
                        self._restart_count += 1
                        return {
                            "ok": False,
                            "timed_out": True,
                            "error": f"32-bit COM command '{payload.get('cmd')}' timed out after {timeout}s.",
                            "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                        }
                    if not line.strip():
                        # Worker exited unexpectedly; restart once
                        self._kill_locked()
                        self._restart_count += 1
                        if attempt == 0:
                            continue
                        return {
                            "ok": False,
                            "error": "Persistent 32-bit COM worker exited without response.",
                            "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                        }
                    parsed = json.loads(line.strip())
                    if isinstance(parsed, list):
                        dict_items = [item for item in parsed if isinstance(item, dict)]
                        parsed = dict_items[-1] if dict_items else {
                            "ok": False,
                            "error": f"Unexpected list response from 32-bit COM worker: {parsed!r}",
                        }
                    if not isinstance(parsed, dict):
                        parsed = {
                            "ok": False,
                            "error": f"Unexpected non-object JSON response from 32-bit COM worker: {type(parsed).__name__}",
                        }
                    if parsed.get("fallback_required") and self._backend_name == "native_cpp_x86" and self._ps32_exe is not None:
                        return self._delegate_to_ps32_fallback(payload, timeout=timeout)
                    parsed["latency_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
                    return parsed
                except Exception as exc:
                    self._kill_locked()
                    self._restart_count += 1
                    if attempt == 0:
                        continue
                    return {
                        "ok": False,
                        "error": f"32-bit COM worker communication failed: {exc}",
                        "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                    }

            return {
                "ok": False,
                "error": "Persistent 32-bit COM worker failed after retry.",
                "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
            }
