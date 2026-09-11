export type SessionStatus = "active" | "paused";
export type WorkflowStatus = "pending" | "running" | "paused" | "completed" | "failed" | "cancelled";

export interface Session {
  id: string;
  user_id: string | null;
  status: SessionStatus;
  created_at: string;
  updated_at: string;
}

export interface Message {
  id: string;
  session_id: string;
  role: string;
  content: string;
  agent_run_id: string | null;
  status: string;
  created_at: string;
}

export interface Workflow {
  id: string;
  session_id: string | null;
  agent_run_id: string | null;
  status: WorkflowStatus;
  current_step: string | null;
  checkpoint: {
    status?: string;
    current_step?: string | null;
    completed_steps?: string[];
    updated_at?: string;
  } | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface Agent {
  id: string;
  name: string;
  role: string;
  model: string;
  provider: string;
  temperature: number;
  status: string;
}

export interface Provider {
  id: string;
  name: string;
  model: string;
  base_url: string | null;
  status: string;
  temperature: number;
}

export interface Tool {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  status: string;
}

export interface Metric {
  id?: number;
  metric_name: string;
  value: number;
  labels: Record<string, unknown>;
  recorded_at: string;
}

export interface DataPage<T> {
  items: T[];
  page: number;
  page_size: number;
  total: number;
  availability: "available" | "not_integrated";
}

export interface ToolCall {
  id: string;
  run_id: string;
  workflow_run_id: string | null;
  tool_name: string;
  input: Record<string, unknown>;
  output: unknown;
  status: "running" | "succeeded" | "failed";
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface MessageAccepted {
  message_id: string;
  session_id: string;
  agent_run_id: string;
  workflow_id: string;
  status: string;
}
