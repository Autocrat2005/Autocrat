"""
Autocrat — System Info Plugin
CPU, RAM, disk, battery, network stats, uptime.
"""

import platform
import time
from datetime import datetime
from typing import Any, Dict
from nexus.core.plugin import NexusPlugin

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


class SystemInfoPlugin(NexusPlugin):
    name = "system_info"
    description = "System overview — CPU, RAM, disk, battery, network, uptime"
    icon = "💻"

    def setup(self):
        self.register_command("full", self.full_info,
                              "Complete system overview", "sysinfo", ["system", "info"])
        self.register_command("cpu", self.cpu_info,
                              "CPU usage and details", "cpu")
        self.register_command("memory", self.memory_info,
                              "RAM usage", "memory", ["ram"])
        self.register_command("disk", self.disk_info,
                              "Disk usage for all drives", "disk", ["storage"])
        self.register_command("battery", self.battery_info,
                              "Battery status", "battery", ["power"])
        self.register_command("network", self.network_info,
                              "Network interface stats", "network", ["net"])
        self.register_command("uptime", self.uptime_info,
                              "System uptime", "uptime")

    def _check(self):
        if not HAS_PSUTIL:
            return {"success": False, "error": "psutil not installed"}
        return None

    def full_info(self, **kwargs):
        err = self._check()
        if err:
            return err

        cpu = self._get_cpu()
        mem = self._get_memory()
        disks = self._get_disks()
        net = self._get_network()
        bat = self._get_battery()

        return {
            "success": True,
            "result": {
                "system": {
                    "os": f"{platform.system()} {platform.release()}",
                    "version": platform.version(),
                    "machine": platform.machine(),
                    "processor": platform.processor(),
                    "hostname": platform.node(),
                    "python": platform.python_version(),
                },
                "cpu": cpu,
                "memory": mem,
                "disks": disks,
                "network": net,
                "battery": bat,
                "uptime": self._get_uptime(),
            },
        }

    def cpu_info(self, **kwargs):
        err = self._check()
        if err:
            return err
        return {"success": True, "result": self._get_cpu()}

    def memory_info(self, **kwargs):
        err = self._check()
        if err:
            return err
        return {"success": True, "result": self._get_memory()}

    def disk_info(self, **kwargs):
        err = self._check()
        if err:
            return err
        return {"success": True, "result": self._get_disks()}

    def battery_info(self, **kwargs):
        err = self._check()
        if err:
            return err
        return {"success": True, "result": self._get_battery()}

    def network_info(self, **kwargs):
        err = self._check()
        if err:
            return err
        return {"success": True, "result": self._get_network()}

    def uptime_info(self, **kwargs):
        err = self._check()
        if err:
            return err
        return {"success": True, "result": self._get_uptime()}

    def _get_cpu(self):
        freq = psutil.cpu_freq()
        return {
            "usage_percent": psutil.cpu_percent(interval=0.5),
            "per_core": psutil.cpu_percent(interval=0.1, percpu=True),
            "cores_physical": psutil.cpu_count(logical=False),
            "cores_logical": psutil.cpu_count(logical=True),
            "frequency_mhz": round(freq.current, 0) if freq else None,
            "frequency_max_mhz": round(freq.max, 0) if freq else None,
        }

    def _get_memory(self):
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        return {
            "total_gb": round(mem.total / (1024 ** 3), 2),
            "used_gb": round(mem.used / (1024 ** 3), 2),
            "available_gb": round(mem.available / (1024 ** 3), 2),
            "usage_percent": mem.percent,
            "swap_total_gb": round(swap.total / (1024 ** 3), 2),
            "swap_used_gb": round(swap.used / (1024 ** 3), 2),
            "swap_percent": swap.percent,
        }

    def _get_disks(self):
        disks = []
        for part in psutil.disk_partitions():
            try:
                usage = psutil.disk_usage(part.mountpoint)
                disks.append({
                    "drive": part.device,
                    "mountpoint": part.mountpoint,
                    "filesystem": part.fstype,
                    "total_gb": round(usage.total / (1024 ** 3), 2),
                    "used_gb": round(usage.used / (1024 ** 3), 2),
                    "free_gb": round(usage.free / (1024 ** 3), 2),
                    "usage_percent": usage.percent,
                })
            except (PermissionError, OSError):
                continue
        return disks

    def _get_battery(self):
        bat = psutil.sensors_battery()
        if bat:
            secs = bat.secsleft
            time_left = "N/A"
            if secs != psutil.POWER_TIME_UNLIMITED and secs != psutil.POWER_TIME_UNKNOWN and secs > 0:
                hours = secs // 3600
                mins = (secs % 3600) // 60
                time_left = f"{hours}h {mins}m"

            return {
                "percent": bat.percent,
                "plugged_in": bat.power_plugged,
                "time_remaining": time_left,
            }
        return {"status": "No battery detected (desktop)"}

    def _get_network(self):
        stats = psutil.net_io_counters()
        interfaces = []
        addrs = psutil.net_if_addrs()
        for name, addr_list in addrs.items():
            for addr in addr_list:
                if addr.family.name == 'AF_INET':
                    interfaces.append({
                        "name": name,
                        "ip": addr.address,
                        "netmask": addr.netmask,
                    })

        return {
            "bytes_sent": self._human_size(stats.bytes_sent),
            "bytes_recv": self._human_size(stats.bytes_recv),
            "packets_sent": stats.packets_sent,
            "packets_recv": stats.packets_recv,
            "interfaces": interfaces,
        }

    def _get_uptime(self):
        boot = datetime.fromtimestamp(psutil.boot_time())
        uptime = datetime.now() - boot
        hours = int(uptime.total_seconds() // 3600)
        minutes = int((uptime.total_seconds() % 3600) // 60)
        return {
            "boot_time": boot.strftime("%Y-%m-%d %H:%M:%S"),
            "uptime": f"{hours}h {minutes}m",
            "uptime_seconds": int(uptime.total_seconds()),
        }

    @staticmethod
    def _human_size(size: int) -> str:
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if abs(size) < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} PB"
