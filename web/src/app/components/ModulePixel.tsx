"use client";

import type { CSSProperties, ReactNode } from "react";

interface ModulePixelProps {
  seed: string;
  tone: string;
  size?: number;
}

export function ModulePixel({ seed, tone, size = 10 }: ModulePixelProps) {
  let hash = 0;
  for (const char of seed) {
    hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  }
  const cells: ReactNode[] = [];
  for (let row = 0; row < 8; row += 1) {
    for (let col = 0; col < 8; col += 1) {
      const index = row * 8 + col;
      const frame = row === 0 || col === 0 || row === 7 || col === 7;
      const bit = ((hash >> (index % 28)) & 3) !== 0;
      const filled = frame || bit;
      cells.push(
        <span
          aria-hidden="true"
          className="pixel-cell"
          data-filled={filled ? "true" : "false"}
          data-frame={frame ? "true" : "false"}
          data-tone={tone}
          key={index}
          style={{ width: size, height: size }}
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
