import { useTheme } from "@/components/ThemeProvider";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  type Node,
  type Edge,
  type NodeTypes,
  type NodeChange,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

/**
 * The generic React Flow shell: background/controls/minimap chrome, node/edge
 * rendering, drag-position tracking, click-to-select -- everything about "a graph of
 * labelled nodes with directed, labelled edges" that has nothing to do with process
 * phases specifically. `ProcessCanvas` (phases/gates) and `FsmEditor` (the states/
 * transitions) both render *through* this one module rather than each standing up their
 * own `<ReactFlow>` tree, so the two graph editors this codebase has stay visually and
 * behaviourally identical by construction, not by convention.
 */
export interface FlowCanvasProps {
  nodes: Node[];
  edges: Edge[];
  nodeTypes: NodeTypes;
  onNodesChange: (changes: NodeChange[]) => void;
  onNodeDragStop: () => void;
  onNodeClick: (nodeId: string) => void;
}

export function FlowCanvas({
  nodes,
  edges,
  nodeTypes,
  onNodesChange,
  onNodeDragStop,
  onNodeClick,
}: FlowCanvasProps) {
  const { resolved } = useTheme();
  return (
    <div className="h-[560px] w-full rounded-md border border-border">
      <ReactFlow
            colorMode={resolved}
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onNodeDragStop={onNodeDragStop}
        onNodeClick={(_e, node) => onNodeClick(node.id)}
        fitView
      >
        <Background />
        <Controls />
        <MiniMap pannable zoomable />
      </ReactFlow>
    </div>
  );
}
