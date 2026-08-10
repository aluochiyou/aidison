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
}

type ModuleGraphNode = Node<ModuleGraphNodeData, "moduleNode">;

const NODE_WIDTH = 184;
const NODE_GAP_X = 14;
// A pixel node contains an 8×8 sprite plus multi-line metadata.  Keep layers
// farther apart than the rendered card height so a downstream node never
// intercepts pointer events intended for its dependency.
const LAYER_GAP_Y = 236;

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

  const edges: Edge[] = [];
  for (const module of modules) {
    for (const depId of module.dependency_ids) {
      if (!byId.has(depId)) continue;
      edges.push({
        id: `edge-${depId}->${module.id}`,
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

  return { nodes, edges };
}

const NODE_TYPES: NodeTypes = { moduleNode: ModuleGraphNodeView };

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

const ARIA_LABEL_CONFIG: Partial<AriaLabelConfig> = {
  "node.a11yDescription.default":
    "按回车或空格键选中该模块。选中后可用方向键在模块间移动焦点。",
  "node.a11yDescription.keyboardDisabled":
    "按回车或空格键选中该模块。选中后可用方向键在模块间移动焦点。",
  "controls.ariaLabel": "依赖图控制面板",
  "controls.zoomIn.ariaLabel": "放大",
  "controls.zoomOut.ariaLabel": "缩小",
  "controls.fitView.ariaLabel": "适应视图",
  "controls.interactive.ariaLabel": "切换拖拽交互",
};

interface ModuleDependencyGraphProps {
  modules: Module[];
  lockedModuleIds: ReadonlySet<string>;
  selectedNames: ReadonlyMap<string, string>;
  selectedModuleId: string | null;
  onSelectModule: (moduleId: string | null) => void;
}

export function ModuleDependencyGraph({
  modules,
  lockedModuleIds,
  selectedNames,
  selectedModuleId,
  onSelectModule,
}: ModuleDependencyGraphProps) {
  const built = useMemo(() => buildGraph(modules), [modules]);
  const lastReported = useRef<string | null>(selectedModuleId);

  const [nodes, setNodes, onNodesChange] = useNodesState<ModuleGraphNode>(
    built.nodes
  );
  const [edges, , onEdgesChange] = useEdgesState(built.edges);

  useEffect(() => {
    setNodes((current) =>
      current.map((node) => {
        const module = built.nodes.find((item) => item.id === node.id);
        if (!module) return node;
        const locked = lockedModuleIds.has(node.id);
        const selectionLabel = selectedNames.get(node.id) ?? "未选型";
        const tone = toneForStage(module.data.module.stage);
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

  useEffect(() => {
    lastReported.current = selectedModuleId;
  }, [selectedModuleId]);

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
      // React Flow's selection-change event is not guaranteed when a node is
      // clicked through a custom pixel child.  Keep selection deterministic
      // for both pointer and keyboard-driven node activation.
      lastReported.current = node.id;
      setNodes((current) =>
        current.map((item) => ({ ...item, selected: item.id === node.id }))
      );
      onSelectModule(node.id);
    },
    [onSelectModule, setNodes]
  );

  return (
    <div className="crafting-graph">
      {modules.length ? (
        <ReactFlow
          aria-label="模块依赖图"
          ariaLabelConfig={ARIA_LABEL_CONFIG}
          colorMode="light"
          deleteKeyCode={null}
          edges={edges}
          edgesFocusable={false}
          fitView
          fitViewOptions={{ padding: 0.18, maxZoom: 1.1 }}
          maxZoom={1.6}
          minZoom={0.35}
          nodeOrigin={[0.5, 0.5]}
          nodeTypes={NODE_TYPES}
          nodes={nodes}
          nodesConnectable={false}
          nodesDraggable
          nodesFocusable
          onEdgesChange={onEdgesChange}
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
      ) : (
        <p className="crafting-empty crafting-graph-empty">
          尚无可视化模块；批准需求后这里会显示模块依赖图。
        </p>
      )}
    </div>
  );
}
