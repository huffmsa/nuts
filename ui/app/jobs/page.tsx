"use client";

import { useState } from "react";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { StatusBadge } from "@/components/status-badge";
import {
  getAllJobs,
  cancelPendingJob,
  cancelScheduledJob,
  requestCancelRunningJob,
  type UnifiedJob,
  type JobStatus,
} from "@/lib/api";

const statusOptions: { value: JobStatus | "all"; label: string }[] = [
  { value: "all", label: "All Statuses" },
  { value: "scheduled", label: "Scheduled" },
  { value: "pending", label: "Pending" },
  { value: "running", label: "Running" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
];

function formatTimestamp(ts?: string): string {
  if (!ts) return "-";
  return new Date(ts).toLocaleString();
}

export default function JobsPage() {
  const [statusFilter, setStatusFilter] = useState<JobStatus | "all">("all");
  const { data: jobs, error, isLoading, mutate } = useSWR("jobs", getAllJobs, {
    refreshInterval: 5000,
  });

  const filteredJobs = jobs?.filter(
    (job) => statusFilter === "all" || job.status === statusFilter
  );

  async function handleCancel(job: UnifiedJob) {
    try {
      if (job.status === "pending") {
        await cancelPendingJob(job.name);
      } else if (job.status === "scheduled") {
        await cancelScheduledJob(job.name);
      } else if (job.status === "running") {
        await requestCancelRunningJob(job.name);
      }
      mutate();
    } catch (err) {
      console.error("Failed to cancel job:", err);
    }
  }

  if (error) {
    return (
      <div className="text-destructive">
        Failed to load jobs: {error.message}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Jobs</h1>
        <Select
          value={statusFilter}
          onValueChange={(v) => setStatusFilter(v as JobStatus | "all")}
        >
          <SelectTrigger className="w-[180px]">
            <SelectValue placeholder="Filter by status" />
          </SelectTrigger>
          <SelectContent>
            {statusOptions.map((opt) => (
              <SelectItem key={opt.value} value={opt.value}>
                {opt.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {isLoading ? (
        <div className="text-muted-foreground">Loading...</div>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Time</TableHead>
              <TableHead>Worker</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {filteredJobs?.length === 0 ? (
              <TableRow>
                <TableCell colSpan={5} className="text-center text-muted-foreground">
                  No jobs found
                </TableCell>
              </TableRow>
            ) : (
              filteredJobs?.map((job, idx) => (
                <TableRow key={`${job.name}-${job.status}-${idx}`}>
                  <TableCell>
                    <Link
                      href={`/jobs/${encodeURIComponent(job.name)}`}
                      className="font-medium hover:underline"
                    >
                      {job.name}
                    </Link>
                  </TableCell>
                  <TableCell>
                    <StatusBadge status={job.status} />
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {job.status === "scheduled" && formatTimestamp(job.next_run)}
                    {job.status === "running" && formatTimestamp(job.started_at)}
                    {(job.status === "completed" || job.status === "failed") && "-"}
                    {job.status === "pending" && "-"}
                  </TableCell>
                  <TableCell className="text-muted-foreground font-mono text-sm">
                    {job.worker_id ? job.worker_id.slice(0, 8) + "..." : "-"}
                  </TableCell>
                  <TableCell className="text-right">
                    {(job.status === "pending" ||
                      job.status === "scheduled" ||
                      job.status === "running") && (
                      <Button
                        variant="destructive"
                        size="sm"
                        onClick={() => handleCancel(job)}
                      >
                        Cancel
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
