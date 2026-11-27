import test from "node:test";
import assert from "node:assert/strict";
import {
  classifyWatchdogHeadroom,
  computeAnalogScaleBounds,
  filterVatRows,
  formatSimTime,
  shortSha256,
} from "./formatters";

test("formatSimTime formats milliseconds deterministically as MM:SS.mmm", () => {
  assert.equal(formatSimTime(0), "00:00.000");
  assert.equal(formatSimTime(2450), "00:02.450");
  assert.equal(formatSimTime(65100), "01:05.100");
  assert.equal(formatSimTime(-50), "00:00.000");
});

test("shortSha256 extracts compact 12-char digest from prefixed hash", () => {
  assert.equal(shortSha256("s7:sha256:abcdef1234567890"), "abcdef123456");
  assert.equal(shortSha256(""), "—");
});

test("classifyWatchdogHeadroom maps >=50% NOMINAL, 20-49% WARNING, <20% CRITICAL", () => {
  assert.equal(classifyWatchdogHeadroom(82.4).status, "NOMINAL");
  assert.equal(classifyWatchdogHeadroom(50.0).status, "NOMINAL");
  assert.equal(classifyWatchdogHeadroom(35.0).status, "WARNING");
  assert.equal(classifyWatchdogHeadroom(12.0).status, "CRITICAL");
});

test("computeAnalogScaleBounds handles empty, flat, and spanned arrays cleanly", () => {
  assert.deepEqual(computeAnalogScaleBounds([]), { min: 0, max: 100 });
  const flat = computeAnalogScaleBounds([50, 50, 50]);
  assert.ok(flat.min <= 0 && flat.max >= 100);
  const spanned = computeAnalogScaleBounds([20, 60, 80]);
  assert.ok(spanned.min < 20 && spanned.max > 80);
});

test("filterVatRows filters case-insensitively by address, symbol, or comment", () => {
  const rows = [
    { address: "I0.0", symbol: "ESTOP_OK", comment: "Emergency Stop NC" },
    { address: "PQW256", symbol: "VALVE_OUT", comment: "Control valve analog" },
  ];
  assert.equal(filterVatRows(rows, "estop").length, 1);
  assert.equal(filterVatRows(rows, "pqw").length, 1);
  assert.equal(filterVatRows(rows, "analog").length, 1);
  assert.equal(filterVatRows(rows, "").length, 2);
});
