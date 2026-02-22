"""
NEXUS OS — Task Scheduler Plugin
Cron-like scheduled task execution using APScheduler.
"""

import uuid
from typing import Any, Dict, List
from nexus.core.plugin import NexusPlugin

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    from apscheduler.triggers.cron import CronTrigger
    HAS_APSCHEDULER = True
except ImportError:
    HAS_APSCHEDULER = False


class TaskSchedulerPlugin(NexusPlugin):
    name = "task_scheduler"
    description = "Schedule commands to run at intervals or specific times"
    icon = "⏰"

    def setup(self):
        self._engine = None  # Will be set by engine after load
        self._scheduler = None
        self._jobs: Dict[str, Dict] = {}

        if HAS_APSCHEDULER:
            self._scheduler = BackgroundScheduler()
            self._scheduler.start()

        self.register_command("schedule_interval", self.schedule_interval,
                              "Schedule a command at regular intervals",
                              "schedule <command> every <interval>", ["repeat"])
        self.register_command("schedule_at", self.schedule_at,
                              "Schedule a command at a specific time",
                              "schedule <command> at <time>", ["cron"])
        self.register_command("list", self.list_schedules,
                              "List all scheduled tasks", "list schedules", ["schedules"])
        self.register_command("cancel", self.cancel_schedule,
                              "Cancel a scheduled task", "cancel schedule <id>", ["unschedule"])

    def set_engine(self, engine):
        """Set reference to the engine for executing scheduled commands."""
        self._engine = engine

    def _execute_scheduled(self, command: str, job_id: str):
        """Callback for scheduled tasks."""
        if self._engine:
            self.log.info(f"Scheduled execution [{job_id}]: {command}")
            self._engine.execute(command)

    def schedule_interval(self, command: str = "", interval: str = "", **kwargs):
        if not HAS_APSCHEDULER or not self._scheduler:
            return {"success": False, "error": "APScheduler not installed"}
        if not command or not interval:
            return {"success": False, "error": "Need both command and interval"}

        # Parse interval like "5 minutes", "1 hour", "30 seconds"
        parts = interval.strip().split()
        try:
            value = int(parts[0])
            unit = parts[1].lower().rstrip('s') if len(parts) > 1 else "minute"
        except (ValueError, IndexError):
            return {"success": False, "error": f"Can't parse interval: '{interval}'. Use: '5 minutes', '1 hour', etc."}

        trigger_kwargs = {}
        if unit in ("second", "sec"):
            trigger_kwargs["seconds"] = value
        elif unit in ("minute", "min"):
            trigger_kwargs["minutes"] = value
        elif unit in ("hour", "hr"):
            trigger_kwargs["hours"] = value
        else:
            return {"success": False, "error": f"Unknown time unit: '{unit}'"}

        job_id = f"nexus_{uuid.uuid4().hex[:8]}"
        self._scheduler.add_job(
            self._execute_scheduled,
            IntervalTrigger(**trigger_kwargs),
            args=[command, job_id],
            id=job_id,
        )

        self._jobs[job_id] = {
            "id": job_id,
            "command": command,
            "type": "interval",
            "interval": interval,
        }

        return {
            "success": True,
            "result": f"Scheduled '{command}' every {interval}",
            "job_id": job_id,
        }

    def schedule_at(self, command: str = "", time: str = "", **kwargs):
        if not HAS_APSCHEDULER or not self._scheduler:
            return {"success": False, "error": "APScheduler not installed"}
        if not command or not time:
            return {"success": False, "error": "Need both command and time"}

        # Parse time like "09:00", "14:30"
        try:
            parts = time.strip().split(":")
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            return {"success": False, "error": f"Can't parse time: '{time}'. Use HH:MM format."}

        job_id = f"nexus_{uuid.uuid4().hex[:8]}"
        self._scheduler.add_job(
            self._execute_scheduled,
            CronTrigger(hour=hour, minute=minute),
            args=[command, job_id],
            id=job_id,
        )

        self._jobs[job_id] = {
            "id": job_id,
            "command": command,
            "type": "daily",
            "time": f"{hour:02d}:{minute:02d}",
        }

        return {
            "success": True,
            "result": f"Scheduled '{command}' daily at {hour:02d}:{minute:02d}",
            "job_id": job_id,
        }

    def list_schedules(self, **kwargs):
        if not self._jobs:
            return {"success": True, "result": "(no scheduled tasks)"}
        return {"success": True, "result": list(self._jobs.values()), "count": len(self._jobs)}

    def cancel_schedule(self, job_id: str = "", **kwargs):
        if not HAS_APSCHEDULER or not self._scheduler:
            return {"success": False, "error": "APScheduler not installed"}

        job_id = str(job_id)
        if job_id in self._jobs:
            try:
                self._scheduler.remove_job(job_id)
            except Exception:
                pass
            del self._jobs[job_id]
            return {"success": True, "result": f"Cancelled schedule: {job_id}"}
        return {"success": False, "error": f"Schedule '{job_id}' not found"}
