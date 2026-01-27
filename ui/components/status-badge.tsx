import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

type Status = "scheduled" | "pending" | "running" | "completed" | "failed" | "active" | string | null;

const statusConfig: Record<string, { variant: "default" | "secondary" | "destructive" | "outline"; className?: string }> = {
  scheduled: { variant: "outline", className: "border-blue-500 text-blue-600" },
  pending: { variant: "secondary", className: "bg-yellow-100 text-yellow-800" },
  running: { variant: "default", className: "bg-blue-500" },
  active: { variant: "default", className: "bg-blue-500" },
  completed: { variant: "secondary", className: "bg-green-100 text-green-800" },
  failed: { variant: "destructive" },
};

interface StatusBadgeProps {
  status: Status;
}

export function StatusBadge({ status }: StatusBadgeProps) {
  const normalizedStatus = status?.toLowerCase() || "unknown";
  const config = statusConfig[normalizedStatus] || { variant: "outline" as const };

  return (
    <Badge variant={config.variant} className={cn("capitalize", config.className)}>
      {status || "unknown"}
    </Badge>
  );
}
