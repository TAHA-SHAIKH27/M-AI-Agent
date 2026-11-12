"""Platform detection utilities."""
import sys
import platform
import structlog
from typing import Optional

logger = structlog.get_logger(__name__)


def is_windows() -> bool:
    """Check if running on Windows."""
    return sys.platform == "win32"


def is_linux() -> bool:
    """Check if running on Linux."""
    return sys.platform.startswith("linux")


def is_macos() -> bool:
    """Check if running on macOS."""
    return sys.platform == "darwin"


def get_windows_version() -> Optional[str]:
    """Get Windows version string."""
    if not is_windows():
        return None
    
    try:
        import ctypes
        # Get version using RtlGetVersion (works on Windows 10+)
        class OSVERSIONINFOEXW(ctypes.Structure):
            _fields_ = [
                ("dwOSVersionInfoSize", ctypes.c_ulong),
                ("dwMajorVersion", ctypes.c_ulong),
                ("dwMinorVersion", ctypes.c_ulong),
                ("dwBuildNumber", ctypes.c_ulong),
                ("dwPlatformId", ctypes.c_ulong),
                ("szCSDVersion", ctypes.c_wchar * 128),
                ("wServicePackMajor", ctypes.c_ushort),
                ("wServicePackMinor", ctypes.c_ushort),
                ("wSuiteMask", ctypes.c_ushort),
                ("wProductType", ctypes.c_byte),
                ("wReserved", ctypes.c_byte),
            ]
        
        ntdll = ctypes.windll.ntdll
        osvi = OSVERSIONINFOEXW()
        osvi.dwOSVersionInfoSize = ctypes.sizeof(OSVERSIONINFOEXW)
        
        if ntdll.RtlGetVersion(ctypes.byref(osvi)) == 0:
            return f"{osvi.dwMajorVersion}.{osvi.dwMinorVersion}.{osvi.dwBuildNumber}"
    except Exception:
        pass
    
    # Fallback
    return platform.version()


def get_python_implementation() -> str:
    """Get Python implementation name."""
    return platform.python_implementation()


def supports_dpapi() -> bool:
    """Check if DPAPI is available (Windows)."""
    return is_windows()


def get_available_memory_gb() -> float:
    """Get available system memory in GB."""
    try:
        import psutil
        return psutil.virtual_memory().available / (1024 ** 3)
    except ImportError:
        return 0.0