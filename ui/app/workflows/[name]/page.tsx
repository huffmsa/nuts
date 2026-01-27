"use client";

import { use } from "react";
import useSWR from "swr";
import Link from "next/link";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/status-badge";
import { RescheduleForm } from "@/components/reschedule-form";
import {
  getWorkflow,
  triggerWorkflow,
  rescheduleWorkflow,
  cancelScheduledWorkflow,
} from "@/lib/api";

function formatTimestamp(ts?: string | null): string {
  if (!ts) return "-";
  return new Date(ts).toLocaleString();
}

export default function WorkflowDetailsPage({
  params,
}: {
  params: Promise<{ name: string }>;
}) {
  const { name } = use(params);
  const decodedName = decodeURIComponent(name);

  const { data: workflow, error, isLoading, mutate } = useSWR(
    `workflow-${decodedName}`,
    () => getWorkflow(decodedName),
    { refreshInterval: 5000 }
  );

  async function handleTrigger() {
    try {
      await triggerWorkflow(decodedName);
      mutate();
    } catch (err) {
      console.error("Failed to trigger workflow:", err);
    }
  }

  async function handleCancel() {
    try {
      await cancelScheduledWorkflow(decodedName);
      mutate();
    } catch (err) {
      console.error("Failed to cancel workflow:", err);
    }
  }

  async function handleReschedule(datetime: string) {
    await rescheduleWorkflow(decodedName, datetime);
    mutate();
  }

  if (error) {
    return (
      <div className="text-destructive">
        Failed to load workflow: {error.message}
      </div>
    );
  }

  if (isLoading) {
    return <div className="text-muted-foreground">Loading...</div>;
  }

  if (!workflow) {
    return <div className="text-muted-foreground">Workflow not found</div>;
  }

  const completedCount = workflow.jobs?.filter((j) => j.status === "completed").length || 0;
  const totalCount = workflow.jobs?.length || 0;

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-2">
        <Link href="/workflows" className="text-muted-foreground hover:text-foreground">
          Workflows
        </Link>
        <span className="text-muted-foreground">/</span>
        <span className="font-medium">{decodedName}</span>
      </div>

      <div className="grid gap-6 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Workflow Details</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div>
              <div className="text-sm text-muted-foreground">Name</div>
              <div className="font-medium">{workflow.name}</div>
            </div>

            <div>
              <div className="text-sm text-muted-foreground">Status</div>
              <StatusBadge status={workflow.status || "scheduled"} />
            </div>

            {workflow.schedule && (
              <div>
                <div className="text-sm text-muted-foreground">Schedule</div>
                <div className="font-mono text-sm">{workflow.schedule}</div>
              </div>
            )}

            {workflow.next_run && (
              <div>
                <div className="text-sm text-muted-foreground">Next Run</div>
                <div>{formatTimestamp(workflow.next_run)}</div>
              </div>
            )}

            <div>
              <div className="text-sm text-muted-foreground">Progress</div>
              <div>{completedCount}/{totalCount} jobs completed</div>
            </div>

            {workflow.error && (
              <div>
                <div className="text-sm text-muted-foreground">Error</div>
                <div className="text-destructive">{workflow.error}</div>
              </div>
            )}

            <div className="flex gap-2">
              <Button onClick={handleTrigger}>Trigger Now</Button>
              {workflow.status === "scheduled" && (
                <Button variant="destructive" onClick={handleCancel}>
                  Cancel
                </Button>
              )}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Reschedule</CardTitle>
          </CardHeader>
          <CardContent>
            <RescheduleForm onSubmit={handleReschedule} />
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Jobs</CardTitle>
        </CardHeader>
        <CardContent>
          {workflow.jobs && workflow.jobs.length > 0 ? (
            <div className="space-y-3">
              {workflow.jobs.map((job) => (
                <div
                  key={job.name}
                  className="flex items-center justify-between p-3 rounded border"
                >
                  <div className="space-y-1">
                    <div className="font-medium">{job.name}</div>
                    {job.requires && job.requires.length > 0 && (
                      <div className="text-sm text-muted-foreground">
                        Requires: {job.requires.join(", ")}
                      </div>
                    )}
                    {job.error && (
                      <div className="text-sm text-destructive">{job.error}</div>
                    )}
                  </div>
                  <StatusBadge status={job.status} />
                </div>
              ))}
            </div>
          ) : (
            <div className="text-muted-foreground">No job information available</div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
