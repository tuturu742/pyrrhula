import { useEffect, useMemo, useState } from "react";
import { MarkerType, Handle, Position, type Node, type Edge, type NodeProps, type NodeChange } from "@xyflow/react";
import type { ProcessDefinitionDSL } from "./dsl";
import { autoLayout, loadSavedLayout, saveLayout, type NodePosition } from "./layout";
import { FlowCanvas } from "./FlowCanvas";

export interface FieldError {
  fieldPath: string;
  message: string;
}

interface PhaseNodeData extends Record<string, unknown> {
  phaseKey: string;
  labelKey: string;
  isInitial: boolean;
  actorSummary: string;
  hasError: boolean;
  errorMessages: string[];
  selected: boolean;
}

function PhaseNode({ data }: NodeProps & { data: PhaseNodeData }) {
  const borderColor = data.hasError
    ? "border-destructive"
    : data.selected
      ? "border-primary"
      : "border-border";
  return (
    <div
      className={`min-w-48 rounded-md border-2 bg-background px-3 py-2 shadow-sm ${borderColor}`}
    >
      <Handle type="target" position={Position.Left} />
      <div className="flex items-center gap-1.5">
        {data.isInitial && (
          <span
            title="Initial phase"
            className="h-2 w-2 shrink-0 rounded-full bg-primary"
          />
        )}
        <div className="truncate text-sm font-medium">{data.phaseKey}</div>
      </div>
      <div className="truncate text-xs text-muted-foreground">{data.labelKey || "(no label_key)"}</div>
      <div className="truncate text-xs text-muted-foreground">{data.actorSummary}</div>
      {data.hasError && (
        <div className="mt-1 text-xs text-destructive">{data.errorMessages[0]}</div>
      )}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const NODE_TYPES = { phase: PhaseNode };

function summarizeActors(phase: ProcessDefinitionDSL["phases"][string]): string {
  if (phase.actors.length === 0) return "(no actors)";
  return phase.actors
    .map((a) => a.persona_type ?? a.human_participant ?? a.any_of?.join("/") ?? "(implicit)")
    .join(", ");
}

interface ProcessCanvasProps {
  definition: ProcessDefinitionDSL;
  workspaceId: string | null;
  defKey: string;
  selectedPhaseKey: string | null;
  onSelectPhase: (phaseKey: string) => void;
  fieldErrors: FieldError[];
}

export function ProcessCanvas({
  definition,
  workspaceId,
  defKey,
  selectedPhaseKey,
  onSelectPhase,
  fieldErrors,
}: ProcessCanvasProps) {
  const [positions, setPositions] = useState<Record<string, NodePosition>>(() => {
    return loadSavedLayout(workspaceId, defKey) ?? autoLayout(definition);
  });

  useEffect(() => {
    const saved = loadSavedLayout(workspaceId, defKey);
    if (saved) {
      setPositions((prev) => ({ ...autoLayout(definition), ...saved, ...prev }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId, defKey]);

  // Phases added/removed since the last layout need a position; existing phases keep
  // whatever the author dragged them to.
  useEffect(() => {
    setPositions((prev) => {
      const autoPositions = autoLayout(definition);
      let changed = false;
      const next = { ...prev };
      for (const key of Object.keys(definition.phases)) {
        if (!(key in next)) {
          next[key] = autoPositions[key];
          changed = true;
        }
      }
      for (const key of Object.keys(next)) {
        if (!(key in definition.phases)) {
          delete next[key];
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [definition]);

  const errorsByPhase = useMemo(() => {
    const map = new Map<string, string[]>();
    for (const err of fieldErrors) {
      const match = /^phases\.([^.[]+)/.exec(err.fieldPath);
      if (match) {
        const list = map.get(match[1]) ?? [];
        list.push(err.message);
        map.set(match[1], list);
      }
    }
    return map;
  }, [fieldErrors]);

  const nodes: Node[] = useMemo(
    () =>
      Object.entries(definition.phases).map(([phaseKey, phase]) => ({
        id: phaseKey,
        type: "phase",
        position: positions[phaseKey] ?? { x: 0, y: 0 },
        data: {
          phaseKey,
          labelKey: phase.label_key,
          isInitial: phaseKey === definition.initial_phase,
          actorSummary: summarizeActors(phase),
          hasError: errorsByPhase.has(phaseKey),
          errorMessages: errorsByPhase.get(phaseKey) ?? [],
          selected: phaseKey === selectedPhaseKey,
        } satisfies PhaseNodeData,
      })),
    [definition, positions, errorsByPhase, selectedPhaseKey],
  );

  const edges: Edge[] = useMemo(() => {
    const result: Edge[] = [];
    for (const [phaseKey, phase] of Object.entries(definition.phases)) {
      if (phase.on_complete) {
        result.push({
          id: `${phaseKey}->on_complete->${phase.on_complete}`,
          source: phaseKey,
          target: phase.on_complete,
          label: "on_complete",
          markerEnd: { type: MarkerType.ArrowClosed },
          style: { stroke: "var(--muted-foreground)" },
        });
      }
      (phase.gates ?? []).forEach((gate, i) => {
        const isTimeout = gate.on?.startsWith("timeout(");
        const isElse = gate.else === true;
        const label = gate.on ? `on: ${gate.on}` : gate.when ? `when: ${gate.when}` : "else";
        result.push({
          id: `${phaseKey}->gate${i}->${gate.to}`,
          source: phaseKey,
          target: gate.to,
          label: label.length > 28 ? `${label.slice(0, 28)}…` : label,
          markerEnd: { type: MarkerType.ArrowClosed },
          style: isTimeout
            ? { stroke: "#d97706", strokeDasharray: "4 3" }
            : isElse
              ? { stroke: "var(--muted-foreground)", strokeDasharray: "2 4" }
              : { stroke: "#2563eb" },
        });
      });
      if (phase.await) {
        result.push({
          id: `${phaseKey}->await_timeout->${phase.await.on_timeout}`,
          source: phaseKey,
          target: phase.await.on_timeout,
          label: `await timeout: ${phase.await.timeout}`,
          markerEnd: { type: MarkerType.ArrowClosed },
          style: { stroke: "#c026d3", strokeDasharray: "6 2" },
        });
      }
    }
    return result;
  }, [definition]);

  function handleNodesChange(changes: NodeChange[]) {
    setPositions((prev) => {
      const next = { ...prev };
      for (const change of changes) {
        if (change.type === "position" && change.position) {
          next[change.id] = change.position;
        }
      }
      return next;
    });
  }

  function handleNodeDragStop() {
    saveLayout(workspaceId, defKey, positions);
  }

  return (
    <FlowCanvas
      nodes={nodes}
      edges={edges}
      nodeTypes={NODE_TYPES}
      onNodesChange={handleNodesChange}
      onNodeDragStop={handleNodeDragStop}
      onNodeClick={onSelectPhase}
    />
  );
}
