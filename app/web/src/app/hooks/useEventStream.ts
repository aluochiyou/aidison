"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getClient } from "@/lib/api";
import type { ProjectEvent } from "@/app/types/types";

const RECONNECT_DELAY = 1_500;
const CURSOR_POLL_DELAY = 1_500;
const EVENT_HISTORY_PAGE_SIZE = 500;
const MAX_VISIBLE_EVENT_HISTORY = 500;
const PROJECT_EVENT_SCHEMA_VERSION = "project-event.v1";
const SUPPORTED_EVENT_SCHEMA_VERSIONS = new Set([0, 1]);

function isCompatibleEvent(event: ProjectEvent): boolean {
  return (
    event.schema_version === PROJECT_EVENT_SCHEMA_VERSION &&
    SUPPORTED_EVENT_SCHEMA_VERSIONS.has(event.event_schema_version)
  );
}

function sequenceFromId(id: string): number {
  const value = Number(id.split(":").at(-1));
  return Number.isFinite(value) ? value : 0;
}

function mergeEvents(
  current: ProjectEvent[],
  incoming: ProjectEvent[]
): ProjectEvent[] {
  const byId = new Map(current.map((event) => [event.id, event]));
  for (const event of incoming) byId.set(event.id, event);
  return [...byId.values()]
    .toSorted((left, right) => left.sequence - right.sequence)
    .slice(-MAX_VISIBLE_EVENT_HISTORY);
}

export function useEventStream(
  projectId: string | null,
  onEvent?: (event: ProjectEvent) => void
) {
  const [connected, setConnected] = useState(false);
  const [events, setEvents] = useState<ProjectEvent[]>([]);
  const [requiresSnapshotRefresh, setRequiresSnapshotRefresh] = useState(false);
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  useEffect(() => {
    if (!projectId) {
      setEvents([]);
      setConnected(false);
      setRequiresSnapshotRefresh(false);
      return;
    }

    const controller = new AbortController();
    const storageKey = `aidison-event-cursor:${projectId}`;
    let cursor = localStorage.getItem(storageKey) || "";
    let streamConnected = false;

    const accept = (event: ProjectEvent) => {
      if (!isCompatibleEvent(event)) {
        // Do not locally derive Project state from a contract this client does
        // not understand. The caller can refetch the compatible Snapshot.
        setRequiresSnapshotRefresh(true);
        return;
      }
      cursor = event.id;
      localStorage.setItem(storageKey, cursor);
      setEvents((current) => mergeEvents(current, [event]));
      onEventRef.current?.(event);
    };

    const parseFrame = (frame: string) => {
      if (!frame.trim() || frame.startsWith(":")) return;
      let id = "";
      let eventType = "message";
      const data: string[] = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith("id:")) id = line.slice(3).trim();
        else if (line.startsWith("event:")) eventType = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trim());
      }
      if (!id || data.length === 0) return;
      const decoded = JSON.parse(data.join("\n")) as {
        schema_version?: string;
        id?: string;
        sequence?: number;
        type?: string;
        payload?: Record<string, unknown>;
        event_id?: string;
        event_schema_version?: number;
        aggregate_type?: string | null;
        aggregate_id?: string | null;
        aggregate_version?: number | null;
        occurred_at?: string;
        payload_hash?: string | null;
        correlation_id?: string | null;
        causation_id?: string | null;
        actor?: string | null;
        source_component?: string | null;
        artifact_refs?: string[];
        created_at?: string;
      };
      if (decoded.schema_version !== PROJECT_EVENT_SCHEMA_VERSION) {
        setRequiresSnapshotRefresh(true);
        return;
      }
      accept({
        schema_version: decoded.schema_version,
        id: decoded.id || id,
        sequence: decoded.sequence ?? sequenceFromId(id),
        type: decoded.type || eventType,
        payload: decoded.payload || {},
        event_id: decoded.event_id || decoded.id || id,
        event_schema_version: decoded.event_schema_version ?? 0,
        aggregate_type: decoded.aggregate_type ?? null,
        aggregate_id: decoded.aggregate_id ?? null,
        aggregate_version: decoded.aggregate_version ?? null,
        occurred_at: decoded.occurred_at || decoded.created_at || "",
        payload_hash: decoded.payload_hash ?? null,
        correlation_id: decoded.correlation_id ?? null,
        causation_id: decoded.causation_id ?? null,
        actor: decoded.actor ?? null,
        source_component: decoded.source_component ?? null,
        artifact_refs: decoded.artifact_refs ?? [],
        created_at: decoded.created_at || "",
      });
    };

    const run = async () => {
      // Rebuild the visible timeline from durable history on every page load.
      // The cursor is then advanced before opening the live stream, so refreshes
      // never lose audit context and reconnects do not duplicate events.
      let historyCursor = 0;
      let history: ProjectEvent[] = [];
      while (!controller.signal.aborted) {
        const page = await getClient().getEvents(
          projectId,
          historyCursor,
          EVENT_HISTORY_PAGE_SIZE
        );
        if (!page.length) break;
        history = mergeEvents(history, page);
        historyCursor = page.at(-1)?.sequence ?? historyCursor;
        if (page.length < EVENT_HISTORY_PAGE_SIZE) break;
      }
      if (controller.signal.aborted) return;
      const compatibleHistory = history.filter(
        (event) => isCompatibleEvent(event)
      );
      if (compatibleHistory.length !== history.length) setRequiresSnapshotRefresh(true);
      if (compatibleHistory.length) {
        cursor = compatibleHistory.at(-1)?.id || cursor;
        localStorage.setItem(storageKey, cursor);
        setEvents((current) => mergeEvents(current, compatibleHistory));
      }

      while (!controller.signal.aborted) {
        try {
          const headers: HeadersInit = cursor
            ? { "Last-Event-ID": cursor }
            : {};
          const response = await fetch(
            getClient().createEventStreamUrl(projectId),
            {
              headers,
              signal: controller.signal,
            }
          );
          if (!response.ok || !response.body)
            throw new Error(`SSE HTTP ${response.status}`);
          streamConnected = true;
          setConnected(true);
          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";
          while (!controller.signal.aborted) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder
              .decode(value, { stream: true })
              .replaceAll("\r\n", "\n");
            let boundary = buffer.indexOf("\n\n");
            while (boundary >= 0) {
              parseFrame(buffer.slice(0, boundary));
              buffer = buffer.slice(boundary + 2);
              boundary = buffer.indexOf("\n\n");
            }
          }
        } catch (error) {
          if (controller.signal.aborted) return;
          console.warn("Aidison event stream reconnecting", error);
        } finally {
          streamConnected = false;
          setConnected(false);
        }
        await new Promise((resolve) => setTimeout(resolve, RECONNECT_DELAY));
      }
    };

    const pollCursor = async () => {
      while (!controller.signal.aborted) {
        // SSE is authoritative while healthy. Polling at the same time doubles
        // the durable-event traffic for every open console, so retain polling
        // only as the fallback for environments where streaming is unavailable.
        if (!streamConnected) {
          try {
            const updates = await getClient().getEvents(
              projectId,
              sequenceFromId(cursor)
            );
            for (const event of updates) accept(event);
          } catch (error) {
            if (controller.signal.aborted) return;
            console.warn("Aidison event cursor poll retrying", error);
          }
        }
        await new Promise((resolve) => setTimeout(resolve, CURSOR_POLL_DELAY));
      }
    };

    void run();
    void pollCursor();
    return () => controller.abort();
  }, [projectId]);

  const clear = useCallback(() => setEvents([]), []);
  return { connected, events, clear, requiresSnapshotRefresh };
}
