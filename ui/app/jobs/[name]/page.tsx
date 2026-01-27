"use client";

import { use } from "react";
import useSWR from "swr";
import Link from "next/link";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/status-badge";
import { RescheduleForm } from "@/components/reschedule-form";
import {
  getAllJobs,
  scheduleJob,
  cancelPendingJob,
  cancelScheduledJob,
  requestCancelRunningJob,
  type UnifiedJob,
} from "@/lib/api";

function formatTimestamp(ts?: string): string {
  if (!ts) return "-";
  return new Date(ts).toLocaleString();
}

export default function JobDetailsPage({
  params,
}: {
  params: Promise<{ name: string }>;
}) {
  const { name } = use(params);
  const decodedName = decodeURIComponent(name);

  const { data: jobs, error, isLoading, mutate } = useSWR("jobs", getAllJobs, {
    refreshInterval: 5000,
  });

  const jobInstances = jobs?.filter((j) => j.name === decodedName) || [];
  const currentJob = jobInstances[0];

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

  async function handleReschedule(datetime: string) {
    await scheduleJob(decodedName, datetime);
    mutate();
  }

  if (error) {
    return (
      <div className="text-destructive">
        Failed to load job: {error.message}
      </div>
    );
  }

  if (isLoading) {
    return <div className="text-muted-foreground">Loading...</div>;
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-2">
        <Link href="/jobs" className="text-muted-foreground hover:text-foreground">
          Jobs
        </Link>
        <span className="text-muted-foreground">/</span>
        <span className="font-medium">{decodedName}</span>
      </div>

      <div className="grid gap-6 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Job Details</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div>
              <div className="text-sm text-muted-foreground">Name</div>
              <div className="font-medium">{decodedName}</div>
            </div>

            {currentJob ? (
              <>
                <div>
                  <div className="text-sm text-muted-foreground">Current Status</div>
                  <StatusBadge status={currentJob.status} />
                </div>

                {currentJob.status === "scheduled" && currentJob.next_run && (
                  <div>
                    <div className="text-sm text-muted-foreground">Next Run</div>
                    <div>{formatTimestamp(currentJob.next_run)}</div>
                  </div>
                )}

                {currentJob.status === "running" && (
                  <>
                    <div>
                      <div className="text-sm text-muted-foreground">Started At</div>
                      <div>{formatTimestamp(currentJob.started_at)}</div>
                    </div>
                    <div>
                      <div className="text-sm text-muted-foreground">Worker</div>
                      <div className="font-mono text-sm">{currentJob.worker_id}</div>
                    </div>
                  </>
                )}

                {currentJob.error && (
                  <div>
                    <div className="text-sm text-muted-foreground">Error</div>
                    <div className="text-destructive">{currentJob.error}</div>
                  </div>
                )}

                {(currentJob.status === "pending" ||
                  currentJob.status === "scheduled" ||
                  currentJob.status === "running") && (
                  <Button
                    variant="destructive"
                    onClick={() => handleCancel(currentJob)}
                  >
                    Cancel Job
                  </Button>
                )}
              </>
            ) : (
              <div className="text-muted-foreground">
                No active instances of this job
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Schedule Job</CardTitle>
          </CardHeader>
          <CardContent>
            <RescheduleForm onSubmit={handleReschedule} label="Schedule" />
          </CardContent>
        </Card>
      </div>

      {jobInstances.length > 1 && (
        <Card>
          <CardHeader>
            <CardTitle>All Instances</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-2">
              {jobInstances.map((job, idx) => (
                <div
                  key={`${job.status}-${idx}`}
                  className="flex items-center justify-between p-2 rounded border"
                >
                  <div className="flex items-center gap-3">
                    <StatusBadge status={job.status} />
                    <span className="text-sm text-muted-foreground">
                      {job.status === "scheduled" && formatTimestamp(job.next_run)}
                      {job.status === "running" && `Started ${formatTimestamp(job.started_at)}`}
                      {job.status === "completed" && "Completed"}
                      {job.status === "failed" && (job.error || "Failed")}
                      {job.status === "pending" && "Waiting"}
                    </span>
                  </div>
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
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
