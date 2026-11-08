"""File operations tools - read-only file operations."""
import os
import pathlib
import fnmatch
import structlog
from typing import Dict, Any, List, Optional

from ..schemas import ToolExecutionContext, ToolResult
from ..base import BaseTool

logger = structlog.get_logger(__name__)


class FileReadTool(BaseTool):
    """Read file contents."""
    
    manifest = {
        "name": "file_read",
        "display_name": "Read File",
        "description": "Read the contents of a file. Read-only operation.",
        "category": "file",
        "risk_level": "low",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to read"},
                "encoding": {"type": "string", "default": "utf-8", "description": "File encoding"},
                "max_size": {"type": "integer", "default": 1048576, "description": "Maximum file size in bytes"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "path": {"type": "string"},
                "size": {"type": "integer"},
                "encoding": {"type": "string"},
            },
        },
        "required_permissions": ["file:read"],
        "optional_permissions": [],
        "risk_level": "low",
        "risk_description": "Read-only file access. No modifications.",
        "requires_approval": False,
        "timeout_seconds": 10,
        "max_retries": 1,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["file", "read", "read-only"],
    }
    
    async def _execute_impl(self, context) -> dict:
        args = context.arguments
        path = args.get("path")
        encoding = args.get("encoding", "utf-8")
        max_size = args.get("max_size", 1048576)
        
        # Validate path
        path_obj = pathlib.Path(path).resolve()
        if not path_obj.exists():
            return {"success": False, "error": f"File not found: {path}", "error_code": "NOT_FOUND"}
        
        if not path_obj.is_file():
            return {"success": False, "error": f"Path is not a file: {path}", "error_code": "NOT_A_FILE"}
        
        # Check file size
        file_size = path_obj.stat().st_size
        if file_size > max_size:
            return {"success": False, "error": f"File size {file_size} exceeds max size {max_size}", "error_code": "FILE_TOO_LARGE"}
        
        # Read file
        try:
            content = path_obj.read_text(encoding=encoding)
        except UnicodeDecodeError:
            return {"success": False, "error": f"Cannot decode file with encoding {encoding}", "error_code": "ENCODING_ERROR"}
        except PermissionError:
            return {"success": False, "error": f"Permission denied: {path}", "error_code": "PERMISSION_DENIED"}
        
        return {
            "success": True,
            "result": {
                "content": content,
                "path": str(path_obj),
                "size": file_size,
                "encoding": encoding,
            },
            "error": None,
        }


class FileListTool(BaseTool):
    """List directory contents."""
    
    manifest = {
        "name": "file_list",
        "display_name": "List Directory",
        "description": "List directory contents. Read-only operation.",
        "category": "file",
        "risk_level": "low",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": ".", "description": "Directory path to list"},
                "recursive": {"type": "boolean", "default": False, "description": "List recursively"},
                "pattern": {"type": "string", "description": "Glob pattern to filter files"},
                "include_hidden": {"type": "boolean", "default": False, "description": "Include hidden files"},
            },
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "items": {"type": "array", "items": {"type": "object"}},
            },
        },
        "required_permissions": ["file:read", "file:list"],
        "optional_permissions": [],
        "risk_level": "low",
        "risk_description": "Read-only directory listing. No modifications.",
        "requires_approval": False,
        "timeout_seconds": 10,
        "max_retries": 1,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["file", "list", "read-only"],
    }
    
    async def _execute_impl(self, context) -> dict:
        args = context.arguments
        path = args.get("path", ".")
        recursive = args.get("recursive", False)
        pattern = args.get("pattern")
        include_hidden = args.get("include_hidden", False)
        
        path_obj = pathlib.Path(path).resolve()
        if not path_obj.exists():
            return {"success": False, "error": f"Path not found: {path}", "error_code": "NOT_FOUND"}
        
        if not path_obj.is_dir():
            return {"success": False, "error": f"Path is not a directory: {path}", "error_code": "NOT_A_DIRECTORY"}
        
        items = []
        
        if recursive:
            walker = path_obj.rglob("*")
        else:
            walker = path_obj.iterdir()
        
        for item in walker:
            try:
                if not include_hidden and item.name.startswith("."):
                    continue
                
                if pattern and not fnmatch.fnmatch(item.name, pattern):
                    continue
                
                stat = item.stat()
                items.append({
                    "name": item.name,
                    "path": str(item),
                    "type": "directory" if item.is_dir() else "file",
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                })
            except (PermissionError, OSError):
                continue
        
        return {
            "success": True,
            "result": {
                "path": str(path_obj),
                "items": items,
            },
            "error": None,
        }


class FileSearchTool(BaseTool):
    """Search for files by name or content pattern."""
    
    manifest = {
        "name": "file_search",
        "display_name": "Search Files",
        "description": "Search for files by name pattern or content. Read-only operation.",
        "category": "file",
        "risk_level": "low",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": ".", "description": "Root directory to search"},
                "pattern": {"type": "string", "description": "Glob pattern to match filenames"},
                "content_pattern": {"type": "string", "description": "Regex pattern to search in file contents"},
                "recursive": {"type": "boolean", "default": True, "description": "Search recursively"},
                "max_results": {"type": "integer", "default": 50, "description": "Maximum results to return"},
                "include_content": {"type": "boolean", "default": False, "description": "Include matching content snippets"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "matches": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
            },
        },
        "required_permissions": ["file:read", "file:list", "file:search"],
        "optional_permissions": [],
        "risk_level": "low",
        "risk_description": "Read-only file search. No modifications.",
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 1,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["file", "search", "read-only"],
    }
    
    async def _execute_impl(self, context) -> dict:
        args = context.arguments
        path = args.get("path", ".")
        pattern = args.get("pattern")
        content_pattern = args.get("content_pattern")
        recursive = args.get("recursive", True)
        max_results = args.get("max_results", 50)
        include_content = args.get("include_content", False)
        
        if not pattern and not content_pattern:
            return {"success": False, "error": "Either pattern or content_pattern must be provided", "error_code": "INVALID_INPUT"}
        
        path_obj = pathlib.Path(args.get("path", ".")).resolve()
        if not path_obj.exists():
            return {"success": False, "error": f"Path not found: {path}", "error_code": "NOT_FOUND"}
        
        if not path_obj.is_dir():
            return {"success": False, "error": f"Path is not a directory: {path}", "error_code": "NOT_A_DIRECTORY"}
        
        matches = []
        count = 0
        
        import re
        content_regex = re.compile(content_pattern) if content_pattern else None
        
        if recursive:
            walker = pathlib.Path(args.get("path", ".")).rglob("*")
        else:
            walker = pathlib.Path(args.get("path", ".")).iterdir()
        
        for item in walker:
            if len(matches) >= max_results:
                break
            
            try:
                if not item.is_file():
                    continue
                
                name_match = fnmatch.fnmatch(item.name, pattern) if pattern else False
                content_match = False
                content_snippet = None
                
                if content_regex:
                    try:
                        content = item.read_text(encoding="utf-8", errors="ignore")
                        match = content_regex.search(content)
                        if match:
                            content_match = True
                            # Get snippet around match
                            start = max(0, match.start() - 50)
                            end = min(len(content), match.end() + 50)
                            content_snippet = content[start:end]
                    except (UnicodeDecodeError, PermissionError, OSError):
                        pass
                
                if name_match or content_match:
                    stat = item.stat()
                    match_info = {
                        "path": str(item),
                        "name": item.name,
                        "size": item.stat().st_size,
                        "modified": item.stat().st_mtime,
                        "name_match": name_match,
                        "content_match": content_match,
                    }
                    
                    if include_content and content_match and content_snippet:
                        match_info["snippet"] = content_snippet
                    
                    matches.append(match_info)
            
            except (PermissionError, OSError, UnicodeDecodeError):
                continue
        
        return {
            "success": True,
            "result": {
                "matches": matches,
                "total": len(matches),
            },
            "error": None,
        }


def validate_safe_path(path_str: str, for_delete: bool = False) -> tuple[bool, Optional[pathlib.Path], Optional[str]]:
    """Validate path safety against traversal attacks and critical system directories.
    
    Returns (is_valid, resolved_path, error_message).
    """
    if not path_str or not isinstance(path_str, str):
        return False, None, "Path must be a non-empty string"
    
    # Check for raw dangerous patterns
    if "\x00" in path_str:
        return False, None, "Null byte in path is prohibited"
    
    try:
        raw_path = pathlib.Path(path_str)
        resolved_path = raw_path.resolve()
    except Exception as e:
        return False, None, f"Invalid path resolution: {e}"
    
    # Path traversal check: verify path is not a bare root or drive root
    resolved_str = str(resolved_path).rstrip("\\/").lower()
    
    # Prohibit bare drive roots (e.g. C:, C:\, D:\, /)
    parts = resolved_path.parts
    if len(parts) <= 1 or (len(parts) == 2 and parts[1] in ("\\", "/")):
        return False, None, f"Operations on root drive '{resolved_path}' are forbidden"
    
    # Prohibit sensitive system directories on Windows
    prohibited_prefixes = [
        "c:\\windows",
        "c:\\program files",
        "c:\\program files (x86)",
        "c:\\recovery",
        "c:\\boot",
        "c:\\system volume information",
    ]
    
    system_root = os.environ.get("SystemRoot", "C:\\Windows").lower()
    if system_root not in prohibited_prefixes:
        prohibited_prefixes.append(system_root)
    
    windir = os.environ.get("windir", "C:\\Windows").lower()
    if windir not in prohibited_prefixes:
        prohibited_prefixes.append(windir)
    
    for prefix in prohibited_prefixes:
        prefix_clean = prefix.rstrip("\\/")
        if resolved_str == prefix_clean or resolved_str.startswith(prefix_clean + "\\"):
            return False, None, f"Access to system directory '{prefix}' is forbidden"
    
    return True, resolved_path, None


class FileWriteTool(BaseTool):
    """Write content to a file with path restrictions and approval requirements."""
    
    manifest = {
        "name": "file_write",
        "display_name": "Write File",
        "description": "Write or append content to a file. Requires explicit approval.",
        "category": "file",
        "risk_level": "high",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Target file path"},
                "content": {"type": "string", "description": "Content to write into the file"},
                "encoding": {"type": "string", "default": "utf-8", "description": "File encoding"},
                "append": {"type": "boolean", "default": False, "description": "Append to file instead of overwriting"},
                "create_parents": {"type": "boolean", "default": True, "description": "Create parent directories if missing"},
                "approved": {"type": "boolean", "default": False, "description": "Explicit approval flag"},
                "approval_token": {"type": "string", "description": "Approval token"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "bytes_written": {"type": "integer"},
                "appended": {"type": "boolean"},
            },
        },
        "required_permissions": ["file:write"],
        "optional_permissions": [],
        "risk_description": "Modifies filesystem content. Requires explicit user approval.",
        "requires_approval": True,
        "timeout_seconds": 15,
        "max_retries": 1,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["file", "write", "modify"],
    }
    
    async def _execute_impl(self, context) -> dict:
        args = context.arguments
        path_str = args.get("path")
        content = args.get("content", "")
        encoding = args.get("encoding", "utf-8")
        append = args.get("append", False)
        create_parents = args.get("create_parents", True)
        
        is_valid, resolved_path, err_msg = validate_safe_path(path_str)
        if not is_valid:
            return {
                "success": False,
                "error": err_msg,
                "error_code": "PATH_RESTRICTED",
            }
        
        try:
            if create_parents:
                resolved_path.parent.mkdir(parents=True, exist_ok=True)
            
            mode = "a" if append else "w"
            with open(resolved_path, mode, encoding=encoding) as f:
                bytes_written = f.write(content)
            
            from ...security import audit_logger, AuditEventType
            audit_logger.log(
                event_type=AuditEventType.FILE_WRITE,
                resource=str(resolved_path),
                action="append" if append else "write",
                result="success",
                details={"bytes_written": bytes_written, "append": append},
                correlation_id=context.correlation_id,
            )
            
            return {
                "success": True,
                "result": {
                    "path": str(resolved_path),
                    "bytes_written": bytes_written,
                    "appended": append,
                },
                "error": None,
            }
        except PermissionError:
            return {"success": False, "error": f"Permission denied: {path_str}", "error_code": "PERMISSION_DENIED"}
        except Exception as e:
            return {"success": False, "error": f"File write error: {str(e)}", "error_code": "WRITE_FAILED"}


class FileDeleteTool(BaseTool):
    """Delete a file with path restrictions and explicit approval requirements."""
    
    manifest = {
        "name": "file_delete",
        "display_name": "Delete File",
        "description": "Delete a file from the filesystem. Critical high-risk operation requiring explicit approval.",
        "category": "file",
        "risk_level": "critical",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to delete"},
                "approved": {"type": "boolean", "default": False, "description": "Explicit approval flag"},
                "approval_token": {"type": "string", "description": "Approval token"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "deleted": {"type": "boolean"},
            },
        },
        "required_permissions": ["file:delete"],
        "optional_permissions": [],
        "risk_description": "Permanently deletes files from the filesystem. Requires explicit approval.",
        "requires_approval": True,
        "timeout_seconds": 15,
        "max_retries": 1,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["file", "delete", "destructive"],
    }
    
    async def _execute_impl(self, context) -> dict:
        args = context.arguments
        path_str = args.get("path")
        
        is_valid, resolved_path, err_msg = validate_safe_path(path_str, for_delete=True)
        if not is_valid:
            return {
                "success": False,
                "error": err_msg,
                "error_code": "PATH_RESTRICTED",
            }
        
        if not resolved_path.exists():
            return {
                "success": False,
                "error": f"Path not found: {path_str}",
                "error_code": "NOT_FOUND",
            }
        
        if resolved_path.is_dir():
            return {
                "success": False,
                "error": f"Path is a directory, not a file: {path_str}. Directory deletion is forbidden.",
                "error_code": "NOT_A_FILE",
            }
        
        try:
            resolved_path.unlink()
            
            from ...security import audit_logger, AuditEventType
            audit_logger.log(
                event_type=AuditEventType.FILE_DELETE,
                resource=str(resolved_path),
                action="delete",
                result="success",
                details={"deleted_path": str(resolved_path)},
                correlation_id=context.correlation_id,
            )
            
            return {
                "success": True,
                "result": {
                    "path": str(resolved_path),
                    "deleted": True,
                },
                "error": None,
            }
        except PermissionError:
            return {"success": False, "error": f"Permission denied deleting: {path_str}", "error_code": "PERMISSION_DENIED"}
        except Exception as e:
            return {"success": False, "error": f"File delete error: {str(e)}", "error_code": "DELETE_FAILED"}