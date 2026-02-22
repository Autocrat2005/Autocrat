"""
NEXUS OS — Heartbeat / Proactive Task System
Runs background tasks at configured intervals and pushes results to all channels.

Supports:
  - Periodic tasks (every N minutes)
  - Cron-like scheduling (e.g., "09:00" daily)
  - One-shot delayed tasks
  - Task results broadcast to Telegram / dashboard / VS Code via message bus
"""

import time
import threading
import schedule
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional
from nexus.core.logger import get_logger

log = get_logger("heartbeat")


class HeartbeatTask:
    """Definition of a recurring/scheduled task."""
    def __init__(self, name: str, command: str, interval_minutes: int = 0,
                 cron_time: str = "", enabled: bool = True, channels: List[str] = None):
        self.name = name
        self.command = command                 # NEXUS command string
        self.interval_minutes = interval_minutes  # 0 = use cron_time
        self.cron_time = cron_time             # "HH:MM" for daily schedule
        self.enabled = enabled
        self.channels = channels or []         # Empty = broadcast to all
        self.last_run: Optional[float] = None
        self.run_count = 0
        self.last_result: Optional[Dict] = None


class Heartbeat:
    """
    Background proactive task runner.
    
    Reads task definitions from config and executes them on schedule,
    pushing results through the message bus to all/specified channels.
    """

    def __init__(self, message_bus=None, engine=None):
        self.bus = message_bus
        self._engine = engine
        self.tasks: Dict[str, HeartbeatTask] = {}
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._custom_tasks: Dict[str, Callable] = {}

        # Built-in proactive tasks
        self._register_builtins()

    def _register_builtins(self):
        """Register built-in proactive task handlers."""
        self._custom_tasks["system_health"] = self._task_system_health
        self._custom_tasks["daily_summary"] = self._task_daily_summary
        self._custom_tasks["disk_alert"] = self._task_disk_alert
        self._custom_tasks["context_probe"] = self._task_context_probe
        self._custom_tasks["proactive_nudge"] = self._task_proactive_nudge

    def load_from_config(self, config_tasks: List[Dict]):
        """Load task definitions from nexus_config.yaml."""
        for t in config_tasks:
            name = t.get("name", "unnamed")
            task = HeartbeatTask(
                name=name,
                command=t.get("command", ""),
                interval_minutes=t.get("interval_minutes", 0),
                cron_time=t.get("cron_time", ""),
                enabled=t.get("enabled", True),
                channels=t.get("channels", []),
            )
            self.tasks[name] = task
            log.info(f"Loaded heartbeat task: {name}")

    def add_task(self, name: str, command: str, interval_minutes: int = 0,
                 cron_time: str = "", channels: List[str] = None) -> HeartbeatTask:
        """Add a task programmatically."""
        task = HeartbeatTask(
            name=name, command=command,
            interval_minutes=interval_minutes,
            cron_time=cron_time, channels=channels,
        )
        self.tasks[name] = task
        self._schedule_task(task)
        log.info(f"Added heartbeat task: {name}")
        return task

    def remove_task(self, name: str):
        """Remove a scheduled task."""
        self.tasks.pop(name, None)
        schedule.clear(name)
        log.info(f"Removed heartbeat task: {name}")

    def start(self):
        """Start the heartbeat loop in a background thread."""
        if self._running:
            return

        self._running = True

        # Schedule all loaded tasks
        for task in self.tasks.values():
            if task.enabled:
                self._schedule_task(task)

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        log.info(f"💓 Heartbeat started — {len(self.tasks)} tasks scheduled")

    def stop(self):
        """Stop the heartbeat."""
        self._running = False
        schedule.clear()
        log.info("Heartbeat stopped")

    def _schedule_task(self, task: HeartbeatTask):
        """Schedule a task using the schedule library."""
        if task.interval_minutes > 0:
            schedule.every(task.interval_minutes).minutes.do(
                self._execute_task, task
            ).tag(task.name)
        elif task.cron_time:
            schedule.every().day.at(task.cron_time).do(
                self._execute_task, task
            ).tag(task.name)

    def _execute_task(self, task: HeartbeatTask):
        """Execute a heartbeat task and push results."""
        if not task.enabled:
            return

        log.info(f"💓 Running heartbeat task: {task.name}")
        task.last_run = time.time()
        task.run_count += 1

        # Check if it's a custom built-in task
        if task.command in self._custom_tasks:
            try:
                result = self._custom_tasks[task.command]()
            except Exception as e:
                result = {"success": False, "error": str(e)}
        else:
            # Execute via message bus or engine
            if self.bus:
                msg = self.bus.send(
                    text=task.command,
                    source="heartbeat",
                    channel_id=task.name,
                    user="Heartbeat",
                )
                result = msg.result
            elif self._engine:
                try:
                    result = self._engine.execute(task.command)
                except Exception as e:
                    result = {"success": False, "error": str(e)}
            else:
                result = {"success": False, "error": "No engine connected"}

        task.last_result = result

        # Broadcast to channels
        self._push_result(task, result)

    def _push_result(self, task: HeartbeatTask, result: Dict):
        """Push task result to configured channels via message bus."""
        if not self.bus:
            return

        import json

        success = "✅" if result.get("success") else "❌"
        output = result.get("result", result.get("error", "No output"))
        if isinstance(output, (dict, list)):
            output = json.dumps(output, indent=2, default=str)[:2000]

        text = f"💓 Heartbeat: {task.name}\n{success} {output}"

        # If specific channels configured, notify them
        if task.channels:
            for channel in task.channels:
                if channel in self.bus._channels:
                    try:
                        msg = type('Msg', (), {
                            'text': task.command,
                            'source': 'heartbeat',
                            'result': {
                                **result,
                                'task_name': task.name,
                                'type': 'heartbeat',
                            }
                        })()
                        callback = self.bus._channels[channel]
                        import asyncio
                        if asyncio.iscoroutinefunction(callback):
                            try:
                                loop = asyncio.get_running_loop()
                                loop.create_task(callback(msg))
                            except RuntimeError:
                                asyncio.run(callback(msg))
                        else:
                            callback(msg)
                    except Exception as e:
                        log.warning(f"Heartbeat dispatch to {channel} failed: {e}")
        else:
            # Broadcast to all
            self.bus.broadcast(text, source="heartbeat")

    def _run_loop(self):
        """Background loop that checks the schedule."""
        while self._running:
            try:
                schedule.run_pending()
            except Exception as e:
                log.error(f"Heartbeat schedule error: {e}")
            time.sleep(10)  # Check every 10 seconds

    # ── Built-in Tasks ──────────────────────────────────────────────

    def _task_system_health(self) -> Dict:
        """Check system health (CPU, RAM, disk)."""
        try:
            import psutil
            cpu = psutil.cpu_percent(interval=1)
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("/")

            alerts = []
            if cpu > 80:
                alerts.append(f"⚠️ HIGH CPU: {cpu}%")
            if mem.percent > 85:
                alerts.append(f"⚠️ HIGH RAM: {mem.percent}%")
            if disk.percent > 90:
                alerts.append(f"⚠️ LOW DISK: {100-disk.percent:.0f}% free")

            return {
                "success": True,
                "result": {
                    "cpu": f"{cpu}%",
                    "ram": f"{mem.percent}% ({mem.used // (1024**3):.1f}/{mem.total // (1024**3):.1f} GB)",
                    "disk": f"{disk.percent}% used ({disk.free // (1024**3):.1f} GB free)",
                    "alerts": alerts or ["All systems normal ✅"],
                }
            }
        except ImportError:
            return {"success": False, "error": "psutil not available"}

    def _task_daily_summary(self) -> Dict:
        """Generate a daily summary (command history, system status)."""
        summary = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "heartbeat_tasks": len(self.tasks),
            "tasks_run_today": sum(
                1 for t in self.tasks.values()
                if t.last_run and (time.time() - t.last_run) < 86400
            ),
        }

        if self.bus:
            summary["bus_stats"] = self.bus.get_stats()
            summary["messages_today"] = len([
                m for m in self.bus._history
                if (time.time() - m.timestamp) < 86400
            ])

        return {"success": True, "result": summary}

    def _task_disk_alert(self) -> Dict:
        """Alert if disk space is low."""
        try:
            import psutil
            disk = psutil.disk_usage("/")
            free_gb = disk.free / (1024**3)
            if free_gb < 10:
                return {
                    "success": True,
                    "result": f"🚨 DISK ALERT: Only {free_gb:.1f} GB free!"
                }
            return {"success": True, "result": f"Disk OK: {free_gb:.1f} GB free"}
        except ImportError:
            return {"success": False, "error": "psutil not available"}

    def _task_context_probe(self) -> Dict:
        """Passive context snapshot (active window + load) for prediction."""
        if not self._engine or not hasattr(self._engine, "learner"):
            return {"success": False, "error": "Learner not available"}

        try:
            import psutil
            cpu = psutil.cpu_percent(interval=0.2)
            ram = psutil.virtual_memory().percent
        except Exception:
            cpu = None
            ram = None

        active_window = None
        try:
            import win32gui
            hwnd = win32gui.GetForegroundWindow()
            if hwnd:
                active_window = win32gui.GetWindowText(hwnd)
        except Exception:
            active_window = None

        self._engine.learner.record_context_snapshot(
            active_window=active_window or "",
            cpu_percent=cpu,
            ram_percent=ram,
            metadata={"source": "heartbeat"},
        )

        return {
            "success": True,
            "result": {
                "active_window": active_window,
                "cpu": cpu,
                "ram": ram,
            },
        }

    def _task_proactive_nudge(self) -> Dict:
        """Detect recurring patterns and push a proactive recommendation."""
        if not self._engine or not hasattr(self._engine, "learner"):
            return {"success": False, "error": "Learner not available"}

        nudges = self._engine.learner.get_proactive_nudges()
        if not nudges:
            return {"success": True, "result": "No recurring pattern detected yet"}

        top = nudges[0]
        action_hint = top.get("suggested_action") or "workflow_engine.run_workflow"
        msg = (
            f"🧠 Proactive suggestion: {top['message']} "
            f"Want me to run {action_hint} now?"
        )

        if self.bus:
            self.bus.broadcast(msg, source="heartbeat")

        return {
            "success": True,
            "result": {
                "nudge": msg,
                "top_pattern": top,
            },
        }

    def get_status(self) -> Dict:
        """Get heartbeat status and all task info."""
        tasks_info = {}
        for name, t in self.tasks.items():
            tasks_info[name] = {
                "command": t.command,
                "enabled": t.enabled,
                "interval_minutes": t.interval_minutes,
                "cron_time": t.cron_time,
                "run_count": t.run_count,
                "last_run": datetime.fromtimestamp(t.last_run).isoformat() if t.last_run else None,
                "last_success": t.last_result.get("success") if t.last_result else None,
                "channels": t.channels,
            }
        return {
            "running": self._running,
            "task_count": len(self.tasks),
            "tasks": tasks_info,
        }
