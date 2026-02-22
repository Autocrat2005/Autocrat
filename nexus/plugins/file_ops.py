"""
NEXUS OS — File Operations Plugin
Search, move, copy, delete, organize, watch, and analyze files.
"""

import os
import shutil
import time
import threading
from pathlib import Path
from typing import Any, Dict, List
from collections import defaultdict
from nexus.core.plugin import NexusPlugin

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False


class FileWatchHandler(FileSystemEventHandler):
    """Collects file system events."""
    def __init__(self):
        self.events = []
        self.max_events = 100

    def on_any_event(self, event):
        if not event.is_directory:
            self.events.append({
                "type": event.event_type,
                "path": event.src_path,
                "time": time.strftime("%H:%M:%S"),
            })
            if len(self.events) > self.max_events:
                self.events = self.events[-self.max_events:]


class FileOpsPlugin(NexusPlugin):
    name = "file_ops"
    description = "Search, move, copy, delete, organize, watch, and analyze files"
    icon = "📁"

    def setup(self):
        self._watchers: Dict[str, Any] = {}

        self.register_command("find", self.find_files,
                              "Recursive file search", "find <pattern> [in <dir>]", ["search"])
        self.register_command("move", self.move_file,
                              "Move a file or directory", "move <src> to <dst>", ["mv"])
        self.register_command("copy", self.copy_file,
                              "Copy a file or directory", "copy <src> to <dst>", ["cp"])
        self.register_command("delete", self.delete_file,
                              "Delete a file or directory", "delete <path>", ["rm", "remove"])
        self.register_command("organize", self.organize_dir,
                              "Auto-sort files by extension", "organize <dir>")
        self.register_command("watch", self.watch_dir,
                              "Monitor a directory for changes", "watch <dir>")
        self.register_command("size", self.get_size,
                              "Get size of file/directory", "size <path>", ["du"])
        self.register_command("tree", self.dir_tree,
                              "Show directory tree", "tree <dir>")

    def find_files(self, pattern: str = "*", directory: str = ".", **kwargs):
        """Recursive file search with glob pattern."""
        search_dir = Path(directory).resolve()
        if not search_dir.exists():
            return {"success": False, "error": f"Directory not found: {directory}"}

        results = []
        pattern_lower = pattern.lower()

        try:
            for item in search_dir.rglob("*"):
                if pattern_lower in item.name.lower():
                    try:
                        stat = item.stat()
                        results.append({
                            "path": str(item),
                            "name": item.name,
                            "type": "dir" if item.is_dir() else "file",
                            "size": self._human_size(stat.st_size) if item.is_file() else "-",
                            "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
                        })
                    except (PermissionError, OSError):
                        continue
                if len(results) >= 100:
                    break
        except PermissionError:
            pass

        return {"success": True, "result": results, "count": len(results)}

    def move_file(self, src: str = "", dst: str = "", **kwargs):
        try:
            shutil.move(src, dst)
            return {"success": True, "result": f"Moved: {src} → {dst}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def copy_file(self, src: str = "", dst: str = "", **kwargs):
        try:
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            return {"success": True, "result": f"Copied: {src} → {dst}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def delete_file(self, path: str = "", **kwargs):
        try:
            p = Path(path)
            if p.is_dir():
                shutil.rmtree(path)
            elif p.exists():
                p.unlink()
            else:
                return {"success": False, "error": f"Not found: {path}"}
            return {"success": True, "result": f"Deleted: {path}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def organize_dir(self, directory: str = ".", **kwargs):
        """Auto-sort files into subdirectories by extension."""
        dir_path = Path(directory).resolve()
        if not dir_path.exists():
            return {"success": False, "error": f"Directory not found: {directory}"}

        ext_map = {
            "Images": [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp", ".ico"],
            "Documents": [".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".xls", ".xlsx", ".ppt", ".pptx", ".csv"],
            "Videos": [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm"],
            "Audio": [".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a"],
            "Archives": [".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"],
            "Code": [".py", ".js", ".ts", ".html", ".css", ".cpp", ".c", ".java", ".go", ".rs", ".json", ".yaml", ".yml", ".xml"],
            "Executables": [".exe", ".msi", ".bat", ".cmd", ".ps1", ".sh"],
        }

        moved = defaultdict(int)

        for item in dir_path.iterdir():
            if item.is_file():
                ext = item.suffix.lower()
                target_folder = "Other"
                for folder, extensions in ext_map.items():
                    if ext in extensions:
                        target_folder = folder
                        break

                target_dir = dir_path / target_folder
                target_dir.mkdir(exist_ok=True)

                dest = target_dir / item.name
                if dest.exists():
                    stem = item.stem
                    dest = target_dir / f"{stem}_{int(time.time())}{ext}"

                try:
                    shutil.move(str(item), str(dest))
                    moved[target_folder] += 1
                except Exception:
                    continue

        total = sum(moved.values())
        return {
            "success": True,
            "result": f"Organized {total} files into {len(moved)} folders",
            "breakdown": dict(moved),
        }

    def watch_dir(self, directory: str = ".", **kwargs):
        if not HAS_WATCHDOG:
            return {"success": False, "error": "watchdog not installed"}

        dir_path = str(Path(directory).resolve())

        if dir_path in self._watchers:
            # Return collected events and stop
            observer, handler = self._watchers[dir_path]
            events = handler.events.copy()
            observer.stop()
            del self._watchers[dir_path]
            return {"success": True, "result": events, "status": "stopped"}

        handler = FileWatchHandler()
        observer = Observer()
        observer.schedule(handler, dir_path, recursive=True)
        observer.daemon = True
        observer.start()
        self._watchers[dir_path] = (observer, handler)

        return {"success": True, "result": f"Watching: {dir_path}", "hint": "Run 'watch' again to see events and stop"}

    def get_size(self, path: str = ".", **kwargs):
        p = Path(path)
        if not p.exists():
            return {"success": False, "error": f"Not found: {path}"}

        if p.is_file():
            size = p.stat().st_size
        else:
            size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())

        return {
            "success": True,
            "result": {
                "path": str(p.resolve()),
                "size": self._human_size(size),
                "bytes": size,
            },
        }

    def dir_tree(self, directory: str = ".", **kwargs):
        """Generate a directory tree view."""
        p = Path(directory).resolve()
        if not p.exists():
            return {"success": False, "error": f"Not found: {directory}"}

        lines = [str(p)]
        self._build_tree(p, lines, "", max_depth=3, current_depth=0)
        return {"success": True, "result": "\n".join(lines)}

    def _build_tree(self, path: Path, lines: list, prefix: str, max_depth: int, current_depth: int):
        if current_depth >= max_depth:
            return
        try:
            entries = sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except PermissionError:
            return

        entries = entries[:50]  # Cap per level
        for i, entry in enumerate(entries):
            is_last = i == len(entries) - 1
            connector = "└── " if is_last else "├── "
            size_str = f" ({self._human_size(entry.stat().st_size)})" if entry.is_file() else ""
            lines.append(f"{prefix}{connector}{entry.name}{size_str}")

            if entry.is_dir():
                extension = "    " if is_last else "│   "
                self._build_tree(entry, lines, prefix + extension, max_depth, current_depth + 1)

    @staticmethod
    def _human_size(size: int) -> str:
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if abs(size) < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} PB"
