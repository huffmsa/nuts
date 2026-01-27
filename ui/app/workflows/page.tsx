"use client";

import useSWR from "swr";
import Link from "next/link";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/status-badge";
import {
  getWorkflows,
  triggerWorkflow,
  cancelScheduledWorkflow,
  type WorkflowStatus,
  type ScheduledWorkflow,
} from "@/lib/api";

function isWorkflowStatus(w: WorkflowStatus | ScheduledWorkflow): w is WorkflowStatus {
  return "jobs" in w;
}

function formatTimestamp(ts?: string | null): string {
  if (!ts) return "-";
  return new Date(ts).toLocaleString();
}

function getProgress(workflow: WorkflowStatus): string {
  if (!workflow.jobs || workflow.jobs.length === 0) return "-";
  const completed = workflow.jobs.filter((j) => j.status === "completed").length;
  return `${completed}/${workflow.jobs.length}`;
}

export default function WorkflowsPage() {
  const { data: workflows, error, isLoading, mutate } = useSWR(
    "workflows",
    getWorkflows,
    { refreshInterval: 5000 }
  );

  async function handleTrigger(name: string) {
    try {
      await triggerWorkflow(name);
      mutate();
    } catch (err) {
      console.error("Failed to trigger workflow:", err);
    }
  }

  async function handleCancel(name: string) {
    try {
      await cancelScheduledWorkflow(name);
      mutate();
    } catch (err) {
      console.error("Failed to cancel workflow:", err);
    }
  }

  if (error) {
    return (
      <div className="text-destructive">
        Failed to load workflows: {error.message}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold">Workflows</h1>

      {isLoading ? (
        <div className="text-muted-foreground">Loading...</div>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Progress</TableHead>
              <TableHead>Next Run</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {workflows?.length === 0 ? (
              <TableRow>
                <TableCell colSpan={5} className="text-center text-muted-foreground">
                  No workflows found
                </TableCell>
              </TableRow>
            ) : (
              workflows?.map((workflow) => {
                const status = isWorkflowStatus(workflow)
                  ? workflow.status || "scheduled"
                  : "scheduled";
                const progress = isWorkflowStatus(workflow)
                  ? getProgress(workflow)
                  : "-";
                const nextRun = workflow.next_run;

                return (
                  <TableRow key={workflow.name}>
                    <TableCell>
                      <Link
                        href={`/workflows/${encodeURIComponent(workflow.name)}`}
                        className="font-medium hover:underline"
                      >
                        {workflow.name}
                      </Link>
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={status} />
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {progress}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {formatTimestamp(nextRun)}
                    </TableCell>
                    <TableCell className="text-right space-x-2">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => handleTrigger(workflow.name)}
                      >
                        Trigger
                      </Button>
                      {status === "scheduled" && (
                        <Button
                          variant="destructive"
                          size="sm"
                          onClick={() => handleCancel(workflow.name)}
                        >
                          Cancel
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
