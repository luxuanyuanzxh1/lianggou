import { readFile } from "node:fs/promises";
import { join } from "node:path";

const STATUS_PATH = join(process.cwd(), "work", "data-scheduler", "status.json");

type SchedulerState = Record<string, unknown>;

async function readState(): Promise<SchedulerState> {
  try {
    const payload: unknown = JSON.parse(await readFile(STATUS_PATH, "utf8"));
    return payload && typeof payload === "object" && !Array.isArray(payload) ? payload as SchedulerState : {};
  } catch {
    return {};
  }
}

function scheduleValue(value: unknown): string {
  return typeof value === "string" && /^\d{2}:\d{2}$/.test(value)
    ? value
    : process.env.RUIPAN_DATA_UPDATE_TIME || "18:10";
}

function nextScheduledAt(schedule: string): string {
  const [hours, minutes] = schedule.split(":").map(Number);
  const next = new Date();
  next.setHours(hours, minutes, 0, 0);
  if (next.getDay() === 0 || next.getDay() === 6 || next <= new Date()) next.setDate(next.getDate() + 1);
  while (next.getDay() === 0 || next.getDay() === 6) next.setDate(next.getDate() + 1);
  return next.toISOString();
}

function pidAlive(value: unknown): boolean {
  const pid = Number(value);
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

export async function GET() {
  const state = await readState();
  const schedule = scheduleValue(state.schedule);
  const nextScheduled = typeof state.next_scheduled_at === "string" ? state.next_scheduled_at : nextScheduledAt(schedule);
  const active = pidAlive(state.pid);
  return Response.json({
    enabled: process.env.RUIPAN_DATA_SCHEDULER !== "0",
    active,
    status: typeof state.status === "string" ? state.status : "waiting",
    schedule,
    daemon_started_at: typeof state.daemon_started_at === "string" ? state.daemon_started_at : null,
    last_seen_at: typeof state.last_seen_at === "string" ? state.last_seen_at : null,
    last_started_at: typeof state.last_started_at === "string" ? state.last_started_at : null,
    last_finished_at: typeof state.last_finished_at === "string" ? state.last_finished_at : null,
    last_success_date: typeof state.last_success_date === "string" ? state.last_success_date : null,
    last_error: typeof state.last_error === "string" ? state.last_error : null,
    next_retry_at: typeof state.next_retry_at === "string" ? state.next_retry_at : null,
    next_scheduled_at: nextScheduled,
  }, { headers: { "Cache-Control": "no-store" } });
}
