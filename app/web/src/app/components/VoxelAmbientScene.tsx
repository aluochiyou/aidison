"use client";

/** Original, code-rendered voxel ambience. It does not depend on game assets. */
export function VoxelAmbientScene({ compact = false }: { compact?: boolean }) {
  return (
    <div
      aria-hidden="true"
      className={`voxel-ambient ${compact ? "is-compact" : ""}`}
    >
      <span className="voxel-cloud cloud-a" />
      <span className="voxel-cloud cloud-b" />
      <span className="voxel-drone">
        <i />
        <b />
      </span>
      <span className="voxel-builder">
        <i className="head" />
        <i className="body" />
        <i className="arm" />
        <i className="leg left" />
        <i className="leg right" />
      </span>
      <span className="voxel-sheep">
        <i className="body" />
        <i className="face" />
        <i className="leg left" />
        <i className="leg right" />
      </span>
      <span className="voxel-fox">
        <i className="tail" />
        <i className="body" />
        <i className="head" />
      </span>
      <span className="voxel-ground" />
    </div>
  );
}
