"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getClient } from "@/lib/api";
import type { ProjectEvent } from "@/app/types/types";

const RECONNECT_DELAY = 1_500;
const CURSOR_POLL_DELAY = 1_500;

function sequenceFromId(id: string): number {
  const value = Number(id.split(":").at(-1));
  return Number.isFinite(value) ? value : 0;
}

function mergeEvents(current: ProjectEvent[], incoming: ProjectEvent[]): ProjectEvent[] {
  const byId = new Map(current.map((event) => [event.id, event]));
  for (const event of incoming) byId.set(event.id, event);
  return [...byId.values()].toSorted((left, right) => left.sequence - right.sequence);
}

export function useEventStream(
  projectId: string | null,
  onEvent?: (event: ProjectEvent) => void,
) {
  const [connected, setConnected] = useState(false);
  const [events, setEvents] = useState<ProjectEvent[]>([]);
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  useEffect(() => {
    if (!projectId) {
      setEvents([]);
      setConnected(false);
      return;
    }

    const controller = new AbortController();
    const storageKey = `aidison-event-cursor:${projectId}`;
    let cursor = localStorage.getItem(storageKey) || "";

    const accept = (event: ProjectEvent) => {
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
        type?: string;
        payload?: Record<string, unknown>;
      };
      accept({
        id,
        sequence: sequenceFromId(id),
        type: decoded.type || eventType,
        payload: decoded.payload || {},
        created_at: new Date().toISOString(),
      });
    };

    const run = async () => {
      // Rebuild the visible timeline from durable history on every page load.
      // The cursor is then advanced before opening the live stream, so refreshes
      // never lose audit context and reconnects do not duplicate events.
      const initial = await getClient().getEvents(projectId, 0);
      if (controller.signal.aborted) return;
      if (initial.length) {
        cursor = initial.at(-1)?.id || cursor;
        localStorage.setItem(storageKey, cursor);
        setEvents((current) => mergeEvents(current, initial));
      }

      while (!controller.signal.aborted) {
        try {
          const headers: HeadersInit = cursor ? { "Last-Event-ID": cursor } : {};
          const response = await fetch(getClient().createEventStreamUrl(projectId), {
            headers,
            signal: controller.signal,
          });
          if (!response.ok || !response.body) throw new Error(`SSE HTTP ${response.status}`);
          setConnected(true);
          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";
          while (!controller.signal.aborted) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true }).replaceAll("\r\n", "\n");
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
          setConnected(false);
        }
        await new Promise((resolve) => setTimeout(resolve, RECONNECT_DELAY));
      }
    };

    const pollCursor = async () => {
      while (!controller.signal.aborted) {
        try {
          const updates = await getClient().getEvents(projectId, sequenceFromId(cursor));
          for (const event of updates) accept(event);
        } catch (error) {
          if (controller.signal.aborted) return;
          console.warn("Aidison event cursor poll retrying", error);
        }
        await new Promise((resolve) => setTimeout(resolve, CURSOR_POLL_DELAY));
      }
    };

    void run();
    void pollCursor();
    return () => controller.abort();
  }, [projectId]);

  const clear = useCallback(() => setEvents([]), []);
  return { connected, events, clear };
}
