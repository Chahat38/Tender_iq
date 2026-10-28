import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { SearchBar, predictSuggestions } from "./SearchBar";

// RTL cannot auto-register cleanup when vitest globals are off.
afterEach(() => {
  cleanup();
});

const POOL = [
  "IT & Telecom",
  "Security & Surveillance",
  "Software Consultancy",
  "Research & Development",
  "consultant services",
];

describe("predictSuggestions", () => {
  it("needs at least two typed characters", () => {
    expect(predictSuggestions("s", POOL)).toEqual([]);
    expect(predictSuggestions("  ", POOL)).toEqual([]);
  });

  it("ranks prefix matches above substring matches", () => {
    expect(predictSuggestions("sec", POOL)).toEqual([
      "Security & Surveillance",
    ]);
    expect(predictSuggestions("consult", POOL)).toEqual([
      "consultant services",
      "Software Consultancy",
    ]);
  });

  it("never suggests the exact query back", () => {
    expect(predictSuggestions("IT & Telecom", POOL)).toEqual([]);
  });

  it("caps the list at eight predictions", () => {
    const pool = Array.from({ length: 20 }, (_, index) => `service ${index}`);
    expect(predictSuggestions("service", pool)).toHaveLength(8);
  });
});

describe("SearchBar", () => {
  it("offers predictions while typing and submits the picked one", () => {
    const onSubmit = vi.fn();
    const onValueChange = vi.fn();
    render(
      <SearchBar
        value="sec"
        active={false}
        suggestions={POOL}
        onValueChange={onValueChange}
        onSubmit={onSubmit}
        onClear={vi.fn()}
      />,
    );

    const input = screen.getByRole("combobox");
    fireEvent.focus(input);
    const option = screen.getByRole("option", {
      name: "Security & Surveillance",
    });
    fireEvent.click(option);

    expect(onValueChange).toHaveBeenCalledWith("Security & Surveillance");
    expect(onSubmit).toHaveBeenCalledWith("Security & Surveillance");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("shows no dropdown before two characters are typed", () => {
    render(
      <SearchBar
        value="s"
        active={false}
        suggestions={POOL}
        onValueChange={vi.fn()}
        onSubmit={vi.fn()}
        onClear={vi.fn()}
      />,
    );
    fireEvent.focus(screen.getByRole("combobox"));
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("moves the highlight with the arrow keys and submits it", () => {
    const onSubmit = vi.fn();
    const onValueChange = vi.fn();
    render(
      <SearchBar
        value="consult"
        active={false}
        suggestions={POOL}
        onValueChange={onValueChange}
        onSubmit={onSubmit}
        onClear={vi.fn()}
      />,
    );
    const input = screen.getByRole("combobox");
    fireEvent.focus(input);
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowDown" });

    const highlighted = screen.getAllByRole("option")[1];
    expect(highlighted.getAttribute("aria-selected")).toBe("true");

    fireEvent.keyDown(input, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledWith("Software Consultancy");
  });

  it("keeps the plain submit behaviour when no prediction is highlighted", () => {
    const onSubmit = vi.fn();
    render(
      <SearchBar
        value="roads in lahore"
        active={false}
        suggestions={POOL}
        onValueChange={vi.fn()}
        onSubmit={onSubmit}
        onClear={vi.fn()}
      />,
    );
    fireEvent.submit(screen.getByRole("search"));
    expect(onSubmit).toHaveBeenCalledWith("roads in lahore");
  });
});
