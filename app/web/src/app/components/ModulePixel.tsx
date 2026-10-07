"use client";

import type { CSSProperties, ReactNode } from "react";

/**
 * Module pixel pattern — deterministic 8×8 sprite generator.
 *
 * Each module's `seed` hashes into a unique 16-bit center pattern (rows 1–6,
 * cols 1–6) and an accent bitmask that is XORed with the tone palette to
 * produce visually distinct, reproducible sprites for every module.
 *
 * Palette reference:
 *   frame:    #241a12 (pixel-ink)  — always solid
 *   fill:     tone-dependent       — good=#5e8d43, live=#b85f35, muted=#7c8996
 *   accent:   lighter tint of fill — good=#73a82b, live=#cf8354, muted=#a0adb8
 *
 * The 8×8 grid is rendered with CSS `image-rendering: pixelated` for
 * crisp edges at any size.  No external images are fetched at runtime.
 */

interface ModulePixelProps {
  seed: string;
  tone: string;
  size?: number;
}

type TonePalette = {
  frame: string;
  fill: string;
  accent: string;
  empty: string;
};

const PALETTES: Record<string, TonePalette> = {
  "tone-good": {
    frame: "#241a12",
    fill: "#4a7a33",
    accent: "#73a82b",
    empty: "#1a1a12",
  },
  "tone-live": {
    frame: "#241a12",
    fill: "#b85f35",
    accent: "#e08a4c",
    empty: "#1a1a12",
  },
  "tone-muted": {
    frame: "#241a12",
    fill: "#5a6474",
    accent: "#8a94a0",
    empty: "#1a1a12",
  },
};

function hashSeed(seed: string): [number, number] {
  let a = 0xdeadbeef;
  let b = 0xcafebabe;
  for (let i = 0; i < seed.length; i++) {
    const char = seed.charCodeAt(i);
    a = ((a << 5) + a) ^ char;
    b = ((b << 7) + b) ^ char;
  }
  return [(a >>> 0) & 0xffff, (b >>> 0) & 0xffff];
}

/**
 * Returns the foreground bit (bool) for cell (row, col) given the 16-bit pattern.
 * Center rows 1–6, cols 1–6 map to the 6×6=36 cell interior.
 * bits 0–17 of `bitsA` encode col parity; bits 0–15 of `bitsB` encode row parity.
 */
function cellFilled(
  row: number,
  col: number,
  bitsA: number,
  bitsB: number,
  threshold: number
): boolean {
  // Edge cells are always frame (handled outside); center cells use pattern.
  if (row >= 1 && row <= 6 && col >= 1 && col <= 6) {
    const idx = (row - 1) * 6 + (col - 1);
    // Interleave bitsA low bits with bitsB low bits for more variation
    const a = (bitsA >> (idx % 16)) & 1;
    const b = (bitsB >> (idx % 16)) & 1;
    return a !== b; // XOR: ~50% fill with structured variation
  }
  return false;
}

export function ModulePixel({ seed, tone, size = 10 }: ModulePixelProps) {
  const palette = PALETTES[tone] ?? PALETTES["tone-muted"];
  const [bitsA, bitsB] = hashSeed(seed);
  const cells: ReactNode[] = [];
  for (let row = 0; row < 8; row += 1) {
    for (let col = 0; col < 8; col += 1) {
      const frame = row <= 0 || col <= 0 || row >= 7 || col >= 7;
      const filled = frame || cellFilled(row, col, bitsA, bitsB, 0);
      // Corner cells use accent color for visual interest
      const isCorner =
        (row <= 1 && col <= 1) ||
        (row <= 1 && col >= 6) ||
        (row >= 6 && col <= 1) ||
        (row >= 6 && col >= 6);
      const isInterior = filled && !frame;
      const color = frame
        ? palette.frame
        : isInterior && isCorner
          ? palette.accent
          : isInterior
            ? palette.fill
            : palette.empty;
      cells.push(
        <span
          aria-hidden="true"
          className="pixel-cell"
          data-filled={filled ? "true" : "false"}
          data-frame={frame ? "true" : "false"}
          data-tone={tone}
          key={row * 8 + col}
          style={{
            width: size,
            height: size,
            background: color,
          }}
        />
      );
    }
  }
  return (
    <span
      aria-hidden="true"
      className="module-pixel"
      role="img"
      style={{ "--px": `${size}px` } as CSSProperties}
    >
      {cells}
    </span>
  );
}
