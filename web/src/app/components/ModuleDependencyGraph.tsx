"use client";

import { useCallback, useEffect, useMemo, useRef } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  useEdgesState,
  useNodesState,
  type AriaLabelConfig,
  type Connection,
  type Edge,
  type Node,
  type NodeProps,
  type NodeTypes,
  type OnNodesChange,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { LockKeyhole } from "lucide-react";
import { ModulePixel } from "@/app/components/ModulePixel";
import type { Module } from "@/app/types/types";

interface ModuleGraphNodeData extends Record<string, unknown> {
  module: Module;
  tone: string;
  selectionLabel: string;
  locked: boolean;
  /** Whether this edge/connection is a Draft-only addition not persisted. */
  draft?: boolean;
}

type ModuleGraphNode = Node<ModuleGraphNodeData, "moduleNode">;

const NODE_WIDTH = 184;
const NODE_GAP_X = 14;
const LAYER_GAP_Y = 240;

function toneForStage(stage: string): string {
  const good = ["selected", "revised"];
  const live = ["researching", "comparing", "deciding", "verifying", "draft"];
  if (good.includes(stage)) return "tone-good";
  if (live.includes(stage)) return "tone-live";
  return "tone-muted";
}

interface BuiltGraph {
  nodes: ModuleGraphNode[];
  edges: Edge[];
  /** Canonical edge keys from the snapshot: "source->target". */
  canonicalEdges: Set<string>;
}

function buildGraph(modules: Module[]): BuiltGraph {
  const byId = new Map(modules.map((module) => [module.id, module]));
  const depthOf = new Map<string, number>();

  const depth = (id: string, stack: Set<string>): number => {
    const cached = depthOf.get(id);
    if (cached !== undefined) return cached;
    if (stack.has(id)) return 0;
    const module = byId.get(id);
    if (!module) return 0;
    stack.add(id);
    const deps = module.dependency_ids.filter((depId) => byId.has(depId));
    const next =
      deps.length > 0
        ? Math.max(...deps.map((depId) => depth(depId, stack))) + 1
        : 0;
    stack.delete(id);
    depthOf.set(id, next);
    return next;
  };

  const layers = new Map<number, Module[]>();
  for (const module of modules) {
    const layer = depth(module.id, new Set());
    const list = layers.get(layer) ?? [];
    list.push(module);
    layers.set(layer, list);
  }

  const nodes: ModuleGraphNode[] = [];
  for (const [layer, layerModules] of [...layers.entries()].sort(
    (a, b) => a[0] - b[0]
  )) {
    const sorted = [...layerModules].sort((a, b) => a.key.localeCompare(b.key));
    const totalWidth =
      sorted.length * NODE_WIDTH + Math.max(0, sorted.length - 1) * NODE_GAP_X;
    sorted.forEach((module, index) => {
      nodes.push({
        id: module.id,
        type: "moduleNode",
        position: {
          x: index * (NODE_WIDTH + NODE_GAP_X) - totalWidth / 2 + NODE_WIDTH / 2,
          y: layer * LAYER_GAP_Y,
        },
        data: {
          module,
          tone: toneForStage(module.stage),
          selectionLabel: "未选型",
          locked: false,
        },
        ariaRole: "button",
        sourcePosition: Position.Bottom,
        targetPosition: Position.Top,
        focusable: true,
      });
    });
  }

  const canonicalEdges = new Set<string>();
  const edges: Edge[] = [];
  for (const module of modules) {
    for (const depId of module.dependency_ids) {
      if (!byId.has(depId)) continue;
      const key = `${depId}->${module.id}`;
      canonicalEdges.add(key);
      edges.push({
        id: `edge-${key}`,
        source: depId,
        target: module.id,
        markerEnd: {
          type: MarkerType.ArrowClosed,
          width: 16,
          height: 16,
          color: "#4a3120",
        },
        style: { stroke: "#4a3120", strokeWidth: 2 },
      });
    }
  }

  return { nodes, edges, canonicalEdges };
}

function ModuleGraphNodeView({
  data,
  selected,
}: NodeProps<ModuleGraphNode>) {
  const { module, tone, selectionLabel, locked } = data;
  return (
    <div
      className={selected ? "crafting-graph-node is-open" : "crafting-graph-node"}
    >
      <Handle className="crafting-graph-handle" position={Position.Top} type="target" />
      <span className="crafting-graph-node-pixel">
        <ModulePixel seed={module.key} tone={tone} size={9} />
        {locked ? (
          <LockKeyhole aria-label="已锁定" className="crafting-lock-badge" />
        ) : null}
      </span>
      <span className="crafting-graph-node-copy">
        <code>{module.key}</code>
        <strong>{module.name}</strong>
        <span>{selectionLabel}</span>
      </span>
      <Handle
        className="crafting-graph-handle"
        position={Position.Bottom}
        type="source"
      />
    </div>
  );
}

const NODE_TYPES: NodeTypes = { moduleNode: ModuleGraphNodeView };

const ARIA_LABEL_CONFIG: Partial<AriaLabelConfig> = {
  "node.a11yDescription.default":
    "按回车或空格键选中该模块。选中后可用方向键在模块间移动焦点。按 Delete 键删除节点。",
  "node.a11yDescription.keyboardDisabled":
    "按回车或空格键选中该模块。选中后可用方向键在模块间移动焦点。",
  "controls.ariaLabel": "依赖图控制面板",
  "controls.zoomIn.ariaLabel": "放大",
  "controls.zoomOut.ariaLabel": "缩小",
  "controls.fitView.ariaLabel": "适应视图",
  "controls.interactive.ariaLabel": "切换拖拽交互",
};

export interface ModuleDependencyGraphProps {
  modules: Module[];
  lockedModuleIds: ReadonlySet<string>;
  selectedNames: ReadonlyMap<string, string>;
  selectedModuleId: string | null;
  onSelectModule: (moduleId: string | null) => void;
  /** Draft-only edges added by the user (keyed as "source->target"). */
  draftEdges: ReadonlySet<string>;
  /** Draft-only edges removed by the user. */
  removedEdges: ReadonlySet<string>;
  /** Called when user connects two nodes (creates a draft edge). */
  onDraftEdgeAdd?: (source: string, target: string) => void;
  /** Called when user deletes a draft edge. */
  onDraftEdgeRemove?: (edgeKey: string) => void;
}

export function ModuleDependencyGraph({
  modules,
  lockedModuleIds,
  selectedNames,
  selectedModuleId,
  onSelectModule,
  draftEdges,
  removedEdges,
  onDraftEdgeAdd,
  onDraftEdgeRemove,
}: ModuleDependencyGraphProps) {
  const built = useMemo(() => buildGraph(modules), [modules]);
  const lastReported = useRef<string | null>(selectedModuleId);

  // Merge canonical + draft − removed edges for the displayed graph.
  const mergedEdges = useMemo(() => {
    const result: Edge[] = [];
    const seen = new Set<string>();

    // Canonical edges (skip removed)
    for (const e of built.edges) {
      const key = `${e.source}->${e.target}`;
      if (removedEdges.has(key)) continue;
      seen.add(key);
      result.push(e);
    }

    // Draft edges (only if not overlapping canonical)
    for (const draftKey of draftEdges) {
      if (seen.has(draftKey)) continue;
      if (removedEdges.has(draftKey)) continue;
      const [source, target] = draftKey.split("->");
      if (!source || !target) continue;
      // Only add if both nodes exist
      if (!built.nodes.some((n) => n.id === source)) continue;
      if (!built.nodes.some((n) => n.id === target)) continue;
      seen.add(draftKey);
      result.push({
        id: `edge-draft-${draftKey}`,
        source,
        target,
        markerEnd: {
          type: MarkerType.ArrowClosed,
          width: 16,
          height: 16,
          color: "#b85f35", // copper tone for draft edges
        },
        style: {
          stroke: "#b85f35",
          strokeWidth: 2,
          strokeDasharray: "6 3",
        },
        deletable: true,
      });
    }

    return result;
  }, [built.edges, built.nodes, draftEdges, removedEdges]);

  const [nodes, setNodes, onNodesChange] = useNodesState<ModuleGraphNode>(
    built.nodes
  );
  const [edges, setEdges, onEdgesChange] = useEdgesState(mergedEdges);

  // Sync nodes in when underlying data changes
  useEffect(() => {
    setNodes((current) =>
      current.map((node) => {
        const fresh = built.nodes.find((item) => item.id === node.id);
        if (!fresh) return node;
        const locked = lockedModuleIds.has(node.id);
        const selectionLabel = selectedNames.get(node.id) ?? "未选型";
        const tone = toneForStage(fresh.data.module.stage);
        const selected = node.id === selectedModuleId;
        if (
          node.data.locked === locked &&
          node.data.selectionLabel === selectionLabel &&
          node.data.tone === tone &&
          node.selected === selected
        ) {
          return node;
        }
        return {
          ...node,
          data: {
            ...node.data,
            locked,
            selectionLabel,
            tone,
          },
          selected,
        };
      })
    );
  }, [built.nodes, lockedModuleIds, selectedNames, selectedModuleId, setNodes]);

  // Sync edges when merged edges change
  useEffect(() => {
    lastReported.current = selectedModuleId;
  }, [selectedModuleId]);

  // Sync edges when merged result changes (draft add/remove)
  useEffect(() => {
    setEdges(mergedEdges);
  }, [mergedEdges, setEdges]);

  const handleNodesChange: OnNodesChange<ModuleGraphNode> = useCallback(
    (changes) => {
      for (const change of changes) {
        if (change.type === "select") {
          const next = change.selected ? change.id : null;
          if (next !== lastReported.current) {
            lastReported.current = next;
            onSelectModule(next);
          }
        }
      }
      onNodesChange(changes);
    },
    [onNodesChange, onSelectModule]
  );

  const handleNodeClick = useCallback(
    (_event: unknown, node: ModuleGraphNode) => {
      lastReported.current = node.id;
      setNodes((current) =>
        current.map((item) => ({ ...item, selected: item.id === node.id }))
      );
      onSelectModule(node.id);
    },
    [onSelectModule, setNodes]
  );

  // Connection handling for draft edge creation
  const handleConnect = useCallback(
    (connection: Connection) => {
      if (!connection.source || !connection.target) return;
      // Prevent self-loops
      if (connection.source === connection.target) return;
      const key = `${connection.source}->${connection.target}`;
      // Skip if already canonical
      if (built.canonicalEdges.has(key)) return;
      onDraftEdgeAdd?.(connection.source, connection.target);
    },
    [built.canonicalEdges, onDraftEdgeAdd]
  );

  // Edge click → delete (only draft edges)
  const handleEdgeClick = useCallback(
    (_event: unknown, edge: Edge) => {
      if (!edge.deletable) return;
      const key = `${edge.source}->${edge.target}`;
      onDraftEdgeRemove?.(key);
    },
    [onDraftEdgeRemove]
  );

  // Delete key handling on nodes (from graph view, removing deps)
  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent) => {
      if (event.key === "Delete") {
        // Delete selected node's draft connections — done via edge removal
        const selected = nodes.filter((n) => n.selected);
        if (selected.length) {
          // For now, deleting a node removes it from selection, not from data
          // since the graph is read-only for canonical data
        }
      }
    },
    [nodes]
  );

  if (!modules.length) {
    return (
      <div className="crafting-graph crafting-graph-empty">
        <p className="crafting-empty">尚无可视化模块；批准需求后这里会显示模块依赖图。</p>
      </div>
    );
  }

  return (
    <div className="crafting-graph" onKeyDown={handleKeyDown}>
      <ReactFlow
        aria-label="模块依赖图"
        ariaLabelConfig={ARIA_LABEL_CONFIG}
        colorMode="light"
        deleteKeyCode="Delete"
        edges={edges}
        edgesFocusable
        edgesReconnectable={false}
        fitView
        fitViewOptions={{ padding: 0.18, maxZoom: 1.1 }}
        maxZoom={1.6}
        minZoom={0.35}
        nodeOrigin={[0.5, 0.5]}
        nodeTypes={NODE_TYPES}
        nodes={nodes}
        nodesConnectable={true}
        nodesDraggable
        nodesFocusable
        onConnect={handleConnect}
        onEdgesChange={onEdgesChange}
        onEdgeClick={handleEdgeClick}
        onNodeClick={handleNodeClick}
        onNodesChange={handleNodesChange}
        panOnScroll
        proOptions={{ hideAttribution: true }}
        selectionOnDrag={false}
        zoomOnPinch
        zoomOnScroll
        zoomOnDoubleClick={false}
      >
        <Background
          color="rgba(74, 49, 32, 0.18)"
          gap={24}
          size={1.5}
          variant={BackgroundVariant.Lines}
        />
        <Controls position="bottom-right" showInteractive={false} />
      </ReactFlow>
      {draftEdges.size > 0 || removedEdges.size > 0 ? (
        <div className="crafting-graph-draft-notice">
          {draftEdges.size > 0 ? `新增 ${draftEdges.size} 条草案连线 · ` : ""}
          {removedEdges.size > 0 ? `暂移除 ${removedEdges.size} 条连线 · ` : ""}
          所有修改仅保存在前端草稿中
        </div>
      ) : null}
    </div>
  );
}