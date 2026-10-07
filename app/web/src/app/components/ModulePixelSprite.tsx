"use client";

import type { CSSProperties } from "react";

/**
 * ModulePixelSprite — deterministic SVG sprite asset.
 *
 * Each module gets a unique 16×16 pixel sprite rendered as inline SVG.
 * Unlike ModulePixel (which uses CSS grid cells), this component produces
 * a self-contained `<svg>` element suitable for use as a badge, icon, or
 * asset export.  No runtime network requests are made.
 *
 * The sprites use a 3-color palette per tone plus a uniform dark background,
 * and the 16×16 grid is generated deterministically from the seed hash.
 */

interface ModulePixelSpriteProps {
  seed: string;
  tone: string;
  /** Rendered pixel size in CSS pixels (default 64 = 16×4). */
  size?: number;
  /** Optional inline style overrides. */
  style?: CSSProperties;
  className?: string;
}

const TONES: Record<
  string,
  { bg: string; frame: string; fill: string; accent: string }
> = {
  "tone-good": {
    bg: "#1a2114",
    frame: "#2f4d26",
    fill: "#5e8d43",
    accent: "#7bc456",
  },
  "tone-live": {
    bg: "#1f1812",
    frame: "#5c3420",
    fill: "#b85f35",
    accent: "#e08a4c",
  },
  "tone-muted": {
    bg: "#14181c",
    frame: "#3a4450",
    fill: "#6b7684",
    accent: "#8a94a0",
  },
};

/**
 * Deterministic RNG seeded with a string.
 * Returns an iterator yielding integers in [0, 255].
 */
function* rng(seed: string): Generator<number> {
  let a = 0x9e3779b9;
  let b = 0x7f4a7c13;
  for (const ch of seed) {
    a = (a ^ ch.charCodeAt(0)) * 0x45d9f3b;
    b = (b ^ ch.charCodeAt(0)) * 0x6c078965;
  }
  // Warm up
  for (let i = 0; i < 5; i++) {
    a ^= a << 13;
    a ^= a >> 17;
    a ^= a << 5;
    b ^= b << 15;
    b ^= b >> 13;
    b ^= b << 7;
  }
  // Mixstream output
  let s0 = (a + b) | 0;
  let s1 = (a ^ (b << 7)) | 0;
  while (true) {
    s0 ^= s0 << 13;
    s0 ^= s0 >> 17;
    s0 ^= s0 << 5;
    s1 ^= s1 << 15;
    s1 ^= s1 >> 13;
    s1 ^= s1 << 7;
    yield ((s0 + s1) & 0xff) >>> 0;
  }
}

export function ModulePixelSprite({
  seed,
  tone,
  size = 64,
  style,
  className,
}: ModulePixelSpriteProps) {
  const palette = TONES[tone] ?? TONES["tone-muted"];
  const gen = rng(seed);
  const pixelSize = 4; // each pixel occupies 4×4 viewBox units
  const vbSize = 16 * pixelSize; // 64

  const rects: string[] = [];

  // Background
  rects.push(
    `<rect x="0" y="0" width="${vbSize}" height="${vbSize}" fill="${palette.bg}"/>`
  );

  for (let row = 0; row < 16; row++) {
    for (let col = 0; col < 16; col++) {
      const isFrame =
        row <= 0 || col <= 0 || row >= 15 || col >= 15;
      const isInnerFrame =
        !isFrame &&
        (row <= 1 || col <= 1 || row >= 14 || col >= 14);

      let color: string | null = null;

      if (isFrame) {
        color = palette.frame;
      } else if (isInnerFrame) {
        // Inner frame gets frame color too
        color = palette.frame;
      } else {
        // Central 12×12 pattern
        const val = gen.next().value!;
        if (val < 80) {
          color = palette.fill;
        } else if (val < 104) {
          color = palette.accent;
        }
        // else stays bg (transparent/empty)
      }

      if (color) {
        const x = col * pixelSize;
        const y = row * pixelSize;
        rects.push(
          `<rect x="${x}" y="${y}" width="${pixelSize}" height="${pixelSize}" fill="${color}"/>`
        );
      }
    }
  }

  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${vbSize} ${vbSize}" width="${size}" height="${size}" shape-rendering="crispEdges">${rects.join("")}</svg>`;

  return (
    <span
      aria-hidden="true"
      className={className}
      dangerouslySetInnerHTML={{ __html: svg }}
      style={{
        display: "inline-block",
        width: size,
        height: size,
        imageRendering: "pixelated",
        ...style,
      }}
    />
  );
}