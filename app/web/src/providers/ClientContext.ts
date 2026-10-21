"use client";

import { createContext, useContext } from "react";
import type { Client } from "@langchain/langgraph-sdk";

export interface ClientContextValue {
  client: Client;
}

export const ClientContext = createContext<ClientContextValue | null>(null);

export function useClient(): Client {
  const context = useContext(ClientContext);

  if (!context) {
    throw new Error("useClient must be used within a ClientProvider");
  }
  return context.client;
}
