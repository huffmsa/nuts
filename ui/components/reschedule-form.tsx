"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

interface RescheduleFormProps {
  onSubmit: (datetime: string) => Promise<void>;
  label?: string;
}

export function RescheduleForm({ onSubmit, label = "Reschedule" }: RescheduleFormProps) {
  const [datetime, setDatetime] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!datetime) return;

    setIsSubmitting(true);
    setError(null);

    try {
      const isoDatetime = new Date(datetime).toISOString();
      await onSubmit(isoDatetime);
      setDatetime("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to reschedule");
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3">
      <div className="flex gap-2">
        <Input
          type="datetime-local"
          value={datetime}
          onChange={(e) => setDatetime(e.target.value)}
          className="w-auto"
          min={new Date().toISOString().slice(0, 16)}
        />
        <Button type="submit" disabled={!datetime || isSubmitting}>
          {isSubmitting ? "Scheduling..." : label}
        </Button>
      </div>
      {error && <p className="text-sm text-destructive">{error}</p>}
    </form>
  );
}
