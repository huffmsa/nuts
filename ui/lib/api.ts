const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

// Types matching the FastAPI models
export interface PendingJob {
  name: string;
  params: unknown[];
}

export interface RunningJob {
  name: string;
  worker_id: string;
  started_at: string;
  params: unknown[];
}

export interface CompletedJob {
  name: string;
  success: boolean;
  error: string | null;
  workflow_name: string | null;
}

export interface ScheduledJob {
  name: string;
  next_run: string;
}

export interface WorkflowJobStatus {
  name: string;
  status: string | null;
  requires: string[] | null;
  error: string | null;
}

export interface WorkflowStatus {
  name: string;
  schedule: string;
  status: string | null;
  error: string | null;
  jobs: WorkflowJobStatus[];
  next_run: string | null;
}

export interface ScheduledWorkflow {
  name: string;
  next_run: string;
}

export interface SuccessResponse {
  success: boolean;
  message: string;
}

// Unified job type for the UI
export type JobStatus = "scheduled" | "pending" | "running" | "completed" | "failed";

export interface UnifiedJob {
  name: string;
  status: JobStatus;
  next_run?: string;
  started_at?: string;
  worker_id?: string;
  success?: boolean;
  error?: string | null;
  workflow_name?: string | null;
  params?: unknown[];
}

// API client functions
async function fetchApi<T>(endpoint: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${endpoint}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...options?.headers,
    },
  });

  if (!res.ok) {
    const error = await res.json().catch(() => ({ detail: "Unknown error" }));
    throw new Error(error.detail || `API error: ${res.status}`);
  }

  return res.json();
}

// Jobs API
export async function getPendingJobs(): Promise<PendingJob[]> {
  return fetchApi("/api/jobs/pending");
}

export async function getRunningJobs(): Promise<RunningJob[]> {
  return fetchApi("/api/jobs/running");
}

export async function getCompletedJobs(): Promise<CompletedJob[]> {
  return fetchApi("/api/jobs/completed");
}

export async function getScheduledJobs(): Promise<ScheduledJob[]> {
  return fetchApi("/api/jobs/scheduled");
}

export async function getAllJobs(): Promise<UnifiedJob[]> {
  const [pending, running, completed, scheduled] = await Promise.all([
    getPendingJobs(),
    getRunningJobs(),
    getCompletedJobs(),
    getScheduledJobs(),
  ]);

  const jobs: UnifiedJob[] = [
    ...scheduled.map((j) => ({
      name: j.name,
      status: "scheduled" as const,
      next_run: j.next_run,
    })),
    ...pending.map((j) => ({
      name: j.name,
      status: "pending" as const,
      params: j.params,
    })),
    ...running.map((j) => ({
      name: j.name,
      status: "running" as const,
      started_at: j.started_at,
      worker_id: j.worker_id,
      params: j.params,
    })),
    ...completed.map((j) => ({
      name: j.name,
      status: (j.success ? "completed" : "failed") as JobStatus,
      success: j.success,
      error: j.error,
      workflow_name: j.workflow_name,
    })),
  ];

  return jobs;
}

export async function enqueueJob(name: string, params: unknown[] = []): Promise<SuccessResponse> {
  return fetchApi("/api/jobs", {
    method: "POST",
    body: JSON.stringify({ name, params }),
  });
}

export async function scheduleJob(name: string, run_at: string): Promise<SuccessResponse> {
  return fetchApi("/api/jobs/schedule", {
    method: "POST",
    body: JSON.stringify({ name, run_at, params: [] }),
  });
}

export async function cancelPendingJob(name: string): Promise<SuccessResponse> {
  return fetchApi(`/api/jobs/pending/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

export async function cancelScheduledJob(name: string): Promise<SuccessResponse> {
  return fetchApi(`/api/jobs/scheduled/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

export async function requestCancelRunningJob(name: string): Promise<SuccessResponse> {
  return fetchApi(`/api/jobs/running/${encodeURIComponent(name)}/cancel`, {
    method: "POST",
  });
}

// Workflows API
export async function getWorkflows(): Promise<(WorkflowStatus | ScheduledWorkflow)[]> {
  return fetchApi("/api/workflows");
}

export async function getWorkflow(name: string): Promise<WorkflowStatus> {
  return fetchApi(`/api/workflows/${encodeURIComponent(name)}`);
}

export async function triggerWorkflow(name: string): Promise<SuccessResponse> {
  return fetchApi(`/api/workflows/${encodeURIComponent(name)}/trigger`, {
    method: "POST",
  });
}

export async function cancelScheduledWorkflow(name: string): Promise<SuccessResponse> {
  return fetchApi(`/api/workflows/${encodeURIComponent(name)}/scheduled`, {
    method: "DELETE",
  });
}

export async function rescheduleWorkflow(name: string, run_at: string): Promise<SuccessResponse> {
  return fetchApi(`/api/workflows/${encodeURIComponent(name)}/reschedule`, {
    method: "POST",
    body: JSON.stringify({ run_at }),
  });
}
