"""
Autocrat — Process Controller Plugin
List, kill, start, monitor, and inspect system processes.
"""

import os
import subprocess
from typing import Any, Dict
from nexus.core.plugin import NexusPlugin

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


class ProcessControllerPlugin(NexusPlugin):
    name = "process_controller"
    description = "List, kill, start, and monitor system processes"
    icon = "⚙️"

    def setup(self):
        self.register_command("list", self.list_processes,
                              "List top processes by CPU/memory", "list processes", ["ps", "processes"])
        self.register_command("kill", self.kill_process,
                              "Kill a process by name or PID", "kill <name|pid>", ["stop", "end"])
        self.register_command("start", self.start_process,
                              "Launch an executable", "start <path>", ["launch", "run process"])
        self.register_command("monitor", self.monitor_process,
                              "Show live stats for a process", "monitor <name>")
        self.register_command("tree", self.process_tree,
                              "Show process tree for a PID", "tree <pid>")

    def list_processes(self, **kwargs):
        if not HAS_PSUTIL:
            return {"success": False, "error": "psutil not installed"}

        procs = []
        for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_info', 'status']):
            try:
                info = p.info
                mem_mb = round(info['memory_info'].rss / (1024 * 1024), 1) if info['memory_info'] else 0
                procs.append({
                    "pid": info['pid'],
                    "name": info['name'],
                    "cpu": info['cpu_percent'],
                    "memory_mb": mem_mb,
                    "status": info['status'],
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # Sort by CPU, then memory
        procs.sort(key=lambda x: (x['cpu'] or 0, x['memory_mb']), reverse=True)
        top = procs[:30]
        return {"success": True, "result": top, "total_processes": len(procs)}

    def kill_process(self, target: str = "", **kwargs):
        if not HAS_PSUTIL:
            return {"success": False, "error": "psutil not installed"}

        killed = []
        target_str = str(target)

        # Try as PID first
        if target_str.isdigit():
            try:
                p = psutil.Process(int(target_str))
                name = p.name()
                p.kill()
                killed.append(f"{name} (PID {target_str})")
            except psutil.NoSuchProcess:
                return {"success": False, "error": f"No process with PID {target_str}"}
            except psutil.AccessDenied:
                return {"success": False, "error": f"Access denied for PID {target_str}"}
        else:
            # Kill by name (partial match)
            for p in psutil.process_iter(['pid', 'name']):
                try:
                    if target_str.lower() in p.info['name'].lower():
                        p.kill()
                        killed.append(f"{p.info['name']} (PID {p.info['pid']})")
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

        if killed:
            return {"success": True, "result": f"Killed: {', '.join(killed)}", "count": len(killed)}
        return {"success": False, "error": f"No matching process found for '{target}'"}

    def start_process(self, path: str = "", **kwargs):
        try:
            proc = subprocess.Popen(path, shell=True)
            return {"success": True, "result": f"Started: {path} (PID {proc.pid})"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def monitor_process(self, target: str = "", **kwargs):
        if not HAS_PSUTIL:
            return {"success": False, "error": "psutil not installed"}

        target_str = str(target)
        procs = []

        for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_info', 'create_time', 'num_threads']):
            try:
                if target_str.lower() in p.info['name'].lower() or target_str == str(p.info['pid']):
                    mem = p.info['memory_info']
                    procs.append({
                        "pid": p.info['pid'],
                        "name": p.info['name'],
                        "cpu_percent": p.info['cpu_percent'],
                        "memory_mb": round(mem.rss / (1024 * 1024), 1) if mem else 0,
                        "memory_vms_mb": round(mem.vms / (1024 * 1024), 1) if mem else 0,
                        "threads": p.info['num_threads'],
                        "status": p.status(),
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if procs:
            return {"success": True, "result": procs}
        return {"success": False, "error": f"Process '{target}' not found"}

    def process_tree(self, pid: int = 0, **kwargs):
        if not HAS_PSUTIL:
            return {"success": False, "error": "psutil not installed"}

        try:
            parent = psutil.Process(int(pid))
            tree = {"pid": parent.pid, "name": parent.name(), "children": []}

            for child in parent.children(recursive=True):
                try:
                    tree["children"].append({
                        "pid": child.pid,
                        "name": child.name(),
                        "status": child.status(),
                    })
                except psutil.NoSuchProcess:
                    continue

            return {"success": True, "result": tree}
        except psutil.NoSuchProcess:
            return {"success": False, "error": f"No process with PID {pid}"}
