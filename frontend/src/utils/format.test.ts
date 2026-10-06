import { describe, expect, it } from "vitest";
import { deadlineStatus, formatDateTime } from "./format";

describe("formatDateTime", () => {
  it("renders null, undefined and blank values as an em dash", () => {
    expect(formatDateTime(null)).toBe("—");
    expect(formatDateTime(undefined)).toBe("—");
    expect(formatDateTime("   ")).toBe("—");
  });

  it("shows the stored wall time, dropping the ISO separator and offset", () => {
    expect(formatDateTime("2026-10-21T11:00:00+05:00")).toBe(
      "2026-10-21 11:00:00",
    );
    // Python str(datetime) uses a space instead of "T"; both occur.
    expect(formatDateTime("2026-10-21 11:00:00+05:00")).toBe(
      "2026-10-21 11:00:00",
    );
    expect(formatDateTime("2026-10-21T06:00:00Z")).toBe(
      "2026-10-21 06:00:00",
    );
  });

  it("returns unparseable values untouched instead of guessing", () => {
    expect(formatDateTime("not a date")).toBe("not a date");
  });
});

describe("deadlineStatus", () => {
  const now = new Date("2026-10-06T12:00:00Z");

  it("is muted for missing or unparseable deadlines", () => {
    expect(deadlineStatus(null, now)).toEqual({
      label: "No deadline",
      tone: "muted",
    });
    expect(deadlineStatus("whenever", now).tone).toBe("muted");
  });

  it("is danger once the deadline has passed", () => {
    expect(deadlineStatus("2026-10-05T11:00:00Z", now)).toEqual({
      label: "Closed",
      tone: "danger",
    });
  });

  it("is warn within a week, counting partial days up", () => {
    // 2026-10-06T12:00Z -> 2026-10-09T11:00Z is 2d23h; ceil gives 3.
    expect(deadlineStatus("2026-10-09T11:00:00Z", now)).toEqual({
      label: "3 days left",
      tone: "warn",
    });
  });

  it("is ok beyond a week", () => {
    // 44d23h away; ceil gives 45.
    expect(deadlineStatus("2026-11-20T11:00:00Z", now)).toEqual({
      label: "45 days left",
      tone: "ok",
    });
  });
});
