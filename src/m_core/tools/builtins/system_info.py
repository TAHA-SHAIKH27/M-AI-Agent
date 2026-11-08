"""System information tool - read-only system information."""
import platform
import psutil
import uuid
import structlog
from typing import Dict, Any, Optional

from ..schemas import ToolExecutionContext, ToolResult
from ..base import BaseTool

logger = structlog.get_logger(__name__)


class SystemInfoTool(BaseTool):
    """Read-only system information tool."""
    
    manifest = {
        "name": "system_info",
        "display_name": "System Information",
        "description": "Get read-only system information (OS, CPU, memory, disk, network). No side effects.",
        "category": "system",
        "risk_level": "low",
        "input_schema": {
            "type": "object",
            "properties": {
                "include_cpu": {"type": "boolean", "default": True, "description": "Include CPU information"},
                "include_memory": {"type": "boolean", "default": True, "description": "Include memory information"},
                "include_disk": {"type": "boolean", "default": True, "description": "Include disk information"},
                "include_network": {"type": "boolean", "default": True, "description": "Include network information"},
                "include_processes": {"type": "boolean", "default": False, "description": "Include top processes"},
            },
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "os": {"type": "object"},
                "cpu": {"type": "object"},
                "memory": {"type": "object"},
                "disk": {"type": "array"},
                "network": {"type": "object"},
                "processes": {"type": "array"},
            },
        },
        "required_permissions": [],
        "optional_permissions": ["system:info"],
        "risk_level": "low",
        "risk_description": "Read-only system information gathering. No side effects.",
        "requires_approval": False,
        "timeout_seconds": 10,
        "max_retries": 1,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["system", "info", "read-only", "diagnostics"],
    }
    
    def __init__(self):
        super().__init__()
    
    async def _execute_impl(self, context) -> dict:
        """Execute the system info tool."""
        args = context.arguments
        
        include_cpu = args.get("include_cpu", True)
        include_memory = args.get("include_memory", True)
        include_disk = args.get("include_disk", True)
        include_network = args.get("include_network", True)
        include_processes = args.get("include_processes", False)
        
        result = {}
        
        # OS Information
        result["os"] = {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python_version": platform.python_version(),
            "hostname": platform.node(),
        }
        
        # CPU Information
        if include_cpu:
            cpu_freq = psutil.cpu_freq()
            result["cpu"] = {
                "physical_cores": psutil.cpu_count(logical=False),
                "logical_cores": psutil.cpu_count(logical=True),
                "current_freq_mhz": cpu_freq.current if cpu_freq else None,
                "min_freq_mhz": cpu_freq.min if cpu_freq else None,
                "max_freq_mhz": cpu_freq.max if cpu_freq else None,
                "usage_percent": psutil.cpu_percent(interval=0.1),
                "per_core_usage": psutil.cpu_percent(interval=0.1, percpu=True),
            }
        
        # Memory Information
        if include_memory:
            vm = psutil.virtual_memory()
            sm = psutil.swap_memory()
            result["memory"] = {
                "total_gb": round(vm.total / (1024**3), 2),
                "available_gb": round(vm.available / (1024**3), 2),
                "used_gb": round(vm.used / (1024**3), 2),
                "percent": vm.percent,
                "swap_total_gb": round(sm.total / (1024**3), 2),
                "swap_used_gb": round(sm.used / (1024**3), 2),
                "swap_percent": sm.percent,
            }
        
        # Disk Information
        if include_disk:
            partitions = psutil.disk_partitions()
            disk_info = []
            for partition in partitions:
                try:
                    usage = psutil.disk_usage(partition.mountpoint)
                    disk_info.append({
                        "device": partition.device,
                        "mountpoint": partition.mountpoint,
                        "fstype": partition.fstype,
                        "total_gb": round(usage.total / (1024**3), 2),
                        "used_gb": round(usage.used / (1024**3), 2),
                        "free_gb": round(usage.free / (1024**3), 2),
                        "percent": round((usage.used / usage.total) * 100, 1),
                    })
                except PermissionError:
                    continue
            result["disk"] = disk_info
        
        # Network Information
        if include_network:
            net_io = psutil.net_io_counters()
            net_ifaces = psutil.net_if_addrs()
            result["network"] = {
                "bytes_sent": net_io.bytes_sent,
                "bytes_recv": net_io.bytes_recv,
                "packets_sent": net_io.packets_sent,
                "packets_recv": net_io.packets_recv,
                "interfaces": {
                    name: [
                        {"family": str(addr.family), "address": addr.address}
                        for addr in addrs
                    ]
                    for name, addrs in net_ifaces.items()
                },
            }
        
        # Top Processes
        if include_processes:
            processes = []
            for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'status']):
                try:
                    proc_info = proc.info
                    if proc_info['cpu_percent'] is not None and proc_info['cpu_percent'] > 0:
                        processes.append(proc_info)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            
            # Sort by CPU usage
            processes.sort(key=lambda x: x.get('cpu_percent', 0), reverse=True)
            result["processes"] = processes[:20]
        
        return {
            "success": True,
            "result": result,
            "error": None,
        }