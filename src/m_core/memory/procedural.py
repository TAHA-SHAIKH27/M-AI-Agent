"""Procedural Memory implementation using JSON/filesystem storage."""
import asyncio
import json
import time
import uuid
import shutil
import structlog
from typing import Optional, List, Dict, Any
from pathlib import Path
from dataclasses import dataclass, field
from msgspec.structs import replace

from .base import MemoryTierBackend, MemoryConfig
from ..core.types import MemoryEntry, MemorySearchQuery, MemorySearchResult, MemoryTier
from ..config import settings
from ..utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ProcedureVersion:
    """A version of a procedure."""
    version: int
    content: str
    metadata: Dict[str, Any]
    created_at: int
    created_by: str
    description: str = ""


@dataclass
class Procedure:
    """A stored procedure/workflow."""
    id: str
    name: str
    description: str
    current_version: int
    versions: List[ProcedureVersion]
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))
    updated_at: int = field(default_factory=lambda: int(time.time() * 1000))


class ProceduralMemory(MemoryTierBackend):
    """JSON/filesystem-based procedural memory for workflows, skills, and procedures."""
    
    def __init__(self, config: Optional[MemoryConfig] = None):
        super().__init__(MemoryTier.PROCEDURAL)
        self.config = config or MemoryConfig()
        self._base_path = settings.get_procedural_path()
        self._procedures: Dict[str, Procedure] = {}
        self._lock = asyncio.Lock()
        self._loaded = False
    
    def _get_procedure_path(self, procedure_id: str) -> Path:
        """Get the file path for a procedure."""
        return self._base_path / f"{procedure_id}.json"
    
    def _get_index_path(self) -> Path:
        """Get the index file path."""
        return self._base_path / "index.json"
    
    async def initialize(self) -> None:
        """Load all procedures from filesystem."""
        if self._loaded:
            return
        
        async with self._lock:
            if self._loaded:
                return
            
            self._base_path.mkdir(parents=True, exist_ok=True)
            index_path = self._get_index_path()
            
            if index_path.exists():
                try:
                    with open(index_path, "r", encoding="utf-8") as f:
                        index_data = json.load(f)
                    
                    for proc_id, proc_data in index_data.items():
                        versions = [
                            ProcedureVersion(
                                version=v["version"],
                                content=v["content"],
                                metadata=v["metadata"],
                                created_at=v["created_at"],
                                created_by=v["created_by"],
                                description=v.get("description", ""),
                            )
                            for v in proc_data.get("versions", [])
                        ]
                        
                        self._procedures[proc_id] = Procedure(
                            id=proc_data["id"],
                            name=proc_data["name"],
                            description=proc_data["description"],
                            current_version=proc_data["current_version"],
                            versions=versions,
                            tags=proc_data.get("tags", []),
                            metadata=proc_data.get("metadata", {}),
                            created_at=proc_data["created_at"],
                            updated_at=proc_data["updated_at"],
                        )
                    
                    self._logger.info(f"Loaded {len(self._procedures)} procedures from disk")
                except Exception as e:
                    self._logger.error("Failed to load procedures index", error=str(e))
            
            self._loaded = True
    
    async def _save_index(self) -> None:
        """Save the procedures index to disk."""
        index_data = {}
        for proc_id, proc in self._procedures.items():
            index_data[proc_id] = {
                "id": proc.id,
                "name": proc.name,
                "description": proc.description,
                "current_version": proc.current_version,
                "versions": [
                    {
                        "version": v.version,
                        "content": v.content,
                        "metadata": v.metadata,
                        "created_at": v.created_at,
                        "created_by": v.created_by,
                        "description": v.description,
                    }
                    for v in proc.versions
                ],
                "tags": proc.tags,
                "metadata": proc.metadata,
                "created_at": proc.created_at,
                "updated_at": proc.updated_at,
            }
        
        index_path = self._get_index_path()
        temp_path = index_path.with_suffix(".tmp")
        
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(index_data, f, indent=2)
        
        # Atomic write
        shutil.move(str(temp_path), str(index_path))
    
    async def _save_procedure_file(self, procedure: Procedure) -> None:
        """Save individual procedure file."""
        proc_path = self._get_procedure_path(procedure.id)
        temp_path = proc_path.with_suffix(".tmp")
        
        data = {
            "id": procedure.id,
            "name": procedure.name,
            "description": procedure.description,
            "current_version": procedure.current_version,
            "versions": [
                {
                    "version": v.version,
                    "content": v.content,
                    "metadata": v.metadata,
                    "created_at": v.created_at,
                    "created_by": v.created_by,
                    "description": v.description,
                }
                for v in procedure.versions
            ],
            "tags": procedure.tags,
            "metadata": procedure.metadata,
            "created_at": procedure.created_at,
            "updated_at": procedure.updated_at,
        }
        
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        
        shutil.move(str(temp_path), str(proc_path))
    
    def _procedure_to_entry(self, procedure: Procedure) -> MemoryEntry:
        """Convert Procedure to MemoryEntry for search compatibility."""
        current_version = procedure.versions[-1] if procedure.versions else None
        
        return MemoryEntry(
            id=procedure.id,
            tier=MemoryTier.PROCEDURAL,
            content=current_version.content if current_version else "",
            metadata={
                "name": procedure.name,
                "description": procedure.description,
                "current_version": procedure.current_version,
                "total_versions": len(procedure.versions),
                **procedure.metadata,
            },
            created_at=procedure.created_at,
            updated_at=procedure.updated_at,
            tags=procedure.tags,
        )
    
    async def save(self, entry: MemoryEntry) -> None:
        """Save a memory entry as a procedure version."""
        if not self._check_consent("write"):
            from ..core.exceptions import ConsentRequiredError
            raise ConsentRequiredError("Consent required for memory write", capability="memory:write")
        
        if not self._loaded:
            await self.initialize()
        
        # Extract procedure info from entry metadata
        name = entry.metadata.get("name", entry.id)
        description = entry.metadata.get("description", "")
        created_by = entry.metadata.get("created_by", "system")
        version_desc = entry.metadata.get("version_description", "Updated")
        
        async with self._lock:
            if entry.id in self._procedures:
                # Update existing procedure
                procedure = self._procedures[entry.id]
                new_version_num = procedure.current_version + 1
                
                new_version = ProcedureVersion(
                    version=new_version_num,
                    content=entry.content,
                    metadata=entry.metadata,
                    created_at=int(time.time() * 1000),
                    created_by=created_by,
                    description=version_desc,
                )
                
                procedure.versions.append(new_version)
                procedure.current_version = new_version_num
                procedure.updated_at = int(time.time() * 1000)
                
                # Keep only last N versions
                if len(procedure.versions) > self.config.procedural_version_keep:
                    procedure.versions = procedure.versions[-self.config.procedural_version_keep:]
            else:
                # Create new procedure
                procedure = Procedure(
                    id=entry.id,
                    name=name,
                    description=description,
                    current_version=1,
                    versions=[
                        ProcedureVersion(
                            version=1,
                            content=entry.content,
                            metadata=entry.metadata,
                            created_at=int(time.time() * 1000),
                            created_by=created_by,
                            description="Initial version",
                        )
                    ],
                    tags=entry.tags,
                    metadata=entry.metadata,
                    created_at=entry.created_at or int(time.time() * 1000),
                    updated_at=int(time.time() * 1000),
                )
                self._procedures[entry.id] = procedure
            
            # Save to disk
            await self._save_procedure_file(procedure)
            await self._save_index()
        
        self._logger.debug("Procedural entry saved", entry_id=entry.id, procedure_name=name)
    
    async def get(self, entry_id: str) -> Optional[MemoryEntry]:
        """Retrieve a procedure's current version as a memory entry."""
        if not self._check_consent("read"):
            from ..core.exceptions import ConsentRequiredError
            raise ConsentRequiredError("Consent required for memory read", capability="memory:read")
        
        if not self._loaded:
            await self.initialize()
        
        async with self._lock:
            procedure = self._procedures.get(entry_id)
            if procedure:
                return self._procedure_to_entry(procedure)
            return None
    
    async def get_procedure(self, procedure_id: str) -> Optional[Procedure]:
        """Get full procedure with all versions."""
        if not self._check_consent("read"):
            from ..core.exceptions import ConsentRequiredError
            raise ConsentRequiredError("Consent required for memory read", capability="memory:read")
        
        if not self._loaded:
            await self.initialize()
        
        async with self._lock:
            return self._procedures.get(procedure_id)
    
    async def get_version(self, procedure_id: str, version: int) -> Optional[ProcedureVersion]:
        """Get a specific version of a procedure."""
        procedure = await self.get_procedure(procedure_id)
        if procedure:
            for v in procedure.versions:
                if v.version == version:
                    return v
        return None
    
    async def delete(self, entry_id: str) -> bool:
        """Delete a procedure."""
        if not self._check_consent("delete"):
            from ..core.exceptions import ConsentRequiredError
            raise ConsentRequiredError("Consent required for memory delete", capability="memory:delete")
        
        if not self._loaded:
            await self.initialize()
        
        async with self._lock:
            if entry_id in self._procedures:
                del self._procedures[entry_id]
                
                # Delete files
                proc_path = self._get_procedure_path(entry_id)
                if proc_path.exists():
                    proc_path.unlink()
                
                await self._save_index()
                self._logger.debug("Procedural entry deleted", entry_id=entry_id)
                return True
            return False
    
    async def search(self, query: MemorySearchQuery) -> MemorySearchResult:
        """Search procedures by name, description, tags, or content."""
        if not self._check_consent("read"):
            from ..core.exceptions import ConsentRequiredError
            raise ConsentRequiredError("Consent required for memory read", capability="memory:read")
        
        if not self._loaded:
            await self.initialize()
        
        start_time = time.time()
        await self.clear_expired()
        
        async with self._lock:
            results = []
            query_lower = query.query.lower()
            
            for procedure in self._procedures.values():
                if query.tiers and MemoryTier.PROCEDURAL not in query.tiers:
                    continue
                
                # Search in name, description, tags, and current content
                current = procedure.versions[-1] if procedure.versions else None
                content = current.content if current else ""
                
                match = (
                    query_lower in procedure.name.lower() or
                    query_lower in procedure.description.lower() or
                    query_lower in content.lower() or
                    any(query_lower in tag.lower() for tag in procedure.tags)
                )
                
                if not match:
                    continue
                
                # Apply filters
                if query.filters:
                    match = True
                    for key, value in query.filters.items():
                        # Check metadata or tags
                        if key == "tag":
                            if value not in procedure.tags:
                                match = False
                                break
                        elif procedure.metadata.get(key) != value:
                            match = False
                            break
                    if not match:
                        continue
                
                results.append(self._procedure_to_entry(procedure))
                if len(results) >= query.limit:
                    break
            
            query_time = (time.time() - start_time) * 1000
            return MemorySearchResult(
                entries=results,
                total_count=len(results),
                query_time_ms=query_time,
            )
    
    async def list_all(self, limit: int = 100, offset: int = 0) -> List[MemoryEntry]:
        """List all procedures with pagination."""
        if not self._check_consent("read"):
            from ..core.exceptions import ConsentRequiredError
            raise ConsentRequiredError("Consent required for memory read", capability="memory:read")
        
        if not self._loaded:
            await self.initialize()
        
        async with self._lock:
            procedures = list(self._procedures.values())[offset:offset + limit]
            return [self._procedure_to_entry(p) for p in procedures]
    
    async def clear_expired(self) -> int:
        """Procedural memory doesn't auto-expire by default."""
        return 0
    
    async def close(self) -> None:
        """Close procedural memory."""
        self._loaded = False
        self._logger.info("Procedural memory closed")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get procedural memory statistics."""
        return {
            "tier": self.tier.value,
            "procedure_count": len(self._procedures),
            "total_versions": sum(len(p.versions) for p in self._procedures.values()),
            "base_path": str(self._base_path),
            "version_keep": self.config.procedural_version_keep,
        }

    async def execute_procedure(
        self,
        procedure_id: str,
        parameters: Optional[Dict[str, Any]] = None,
        version: Optional[int] = None,
        approved: bool = False,
        approval_token: Optional[str] = None,
    ) -> "ProcedureExecutionResult":
        """Controlled procedure execution engine.
        
        Executes ONLY registered, permitted tools/operations with schema validation
        and consent/approval checks. NEVER executes arbitrary text as code.
        """
        start_time = time.time()
        parameters = parameters or {}
        
        if not self._loaded:
            await self.initialize()
        
        procedure = await self.get_procedure(procedure_id)
        if not procedure:
            from ..core.exceptions import MemoryNotFoundError
            raise MemoryNotFoundError(f"Procedure '{procedure_id}' not found")
        
        # Get target version
        if version is not None:
            proc_version = await self.get_version(procedure_id, version)
            if not proc_version:
                from ..core.exceptions import MemoryNotFoundError
                raise MemoryNotFoundError(f"Procedure version {version} not found for '{procedure_id}'")
        else:
            proc_version = procedure.versions[-1] if procedure.versions else None
            if not proc_version:
                return ProcedureExecutionResult(
                    procedure_id=procedure_id,
                    version=0,
                    success=False,
                    step_results=[],
                    error="Procedure contains no versions",
                    execution_time_ms=0.0,
                )
        
        # Parse procedure content
        steps = []
        param_schema = None
        content_str = proc_version.content.strip()
        
        try:
            if content_str.startswith("{") and content_str.endswith("}"):
                data = json.loads(content_str)
                steps = data.get("steps", [])
                param_schema = data.get("parameters_schema")
            elif "steps:" in content_str:
                # Handle YAML if pyyaml is available
                import yaml
                data = yaml.safe_load(content_str)
                if isinstance(data, dict):
                    steps = data.get("steps", [])
                    param_schema = data.get("parameters_schema")
        except Exception as e:
            self._logger.warning("Failed to parse structured procedure", error=str(e))
        
        # Validate parameters if schema exists
        if param_schema and isinstance(param_schema, dict):
            required_params = param_schema.get("required", [])
            for req in required_params:
                if req not in parameters:
                    from ..core.exceptions import ValidationError
                    raise ValidationError(f"Missing required parameter '{req}' for procedure '{procedure_id}'", field=req)
        
        if not steps:
            return ProcedureExecutionResult(
                procedure_id=procedure_id,
                version=proc_version.version,
                success=True,
                step_results=[],
                error=None,
                execution_time_ms=(time.time() - start_time) * 1000,
            )
        
        # Controlled execution of registered operations
        from ..tools import tool_registry
        from ..security import consent_manager, audit_logger, AuditEventType
        from ..core.exceptions import ConsentRequiredError, ToolExecutionError
        
        step_results: List[ProcedureStepResult] = []
        execution_context_vars = dict(parameters)
        all_success = True
        overall_error = None
        
        for idx, step in enumerate(steps):
            step_start = time.time()
            step_id = step.get("id") or f"step_{idx + 1}"
            tool_name = step.get("tool") or step.get("action")
            
            if not tool_name:
                err = f"Step '{step_id}' missing required 'tool' or 'action' field"
                step_results.append(ProcedureStepResult(
                    step_id=step_id,
                    action="unknown",
                    success=False,
                    error=err,
                    execution_time_ms=(time.time() - step_start) * 1000,
                ))
                all_success = False
                overall_error = err
                break
            
            # Security verification: tool must be explicitly registered
            try:
                tool = tool_registry.get_tool(tool_name)
            except Exception:
                err = f"Unregistered operation '{tool_name}' in step '{step_id}' is forbidden"
                step_results.append(ProcedureStepResult(
                    step_id=step_id,
                    action=tool_name,
                    success=False,
                    error=err,
                    execution_time_ms=(time.time() - step_start) * 1000,
                ))
                all_success = False
                overall_error = err
                break
            
            # Security: check consents
            for perm in tool.get_required_permissions():
                if not consent_manager.is_granted(perm):
                    err = f"Consent required for capability: {perm}"
                    all_success = False
                    overall_error = err
                    step_results.append(ProcedureStepResult(
                        step_id=step_id,
                        action=tool_name,
                        success=False,
                        error=err,
                        execution_time_ms=(time.time() - step_start) * 1000,
                    ))
                    break
            
            if not all_success:
                break
            
            # Security: check approvals
            step_approved = approved or step.get("approved", False)
            step_token = approval_token or step.get("approval_token")
            if tool.requires_approval and not (step_approved or step_token):
                err = f"Tool '{tool_name}' in step '{step_id}' requires explicit approval"
                step_results.append(ProcedureStepResult(
                    step_id=step_id,
                    action=tool_name,
                    success=False,
                    error=err,
                    execution_time_ms=(time.time() - step_start) * 1000,
                ))
                all_success = False
                overall_error = err
                break
            
            # Resolve arguments safely (string substitution without eval)
            raw_args = step.get("arguments", {})
            resolved_args = {}
            for k, v in raw_args.items():
                if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
                    var_key = v[2:-1].strip()
                    resolved_args[k] = execution_context_vars.get(var_key, v)
                else:
                    resolved_args[k] = v
            
            if step_approved:
                resolved_args["approved"] = True
            if step_token:
                resolved_args["approval_token"] = step_token
            
            # Execute tool
            try:
                result = await tool_registry.invoke(tool_name, resolved_args)
                step_time = (time.time() - step_start) * 1000
                
                step_results.append(ProcedureStepResult(
                    step_id=step_id,
                    action=tool_name,
                    success=result.success,
                    result=result.result,
                    error=result.error,
                    execution_time_ms=step_time,
                ))
                
                if result.success:
                    if isinstance(result.result, dict):
                        for res_k, res_v in result.result.items():
                            execution_context_vars[f"{step_id}.{res_k}"] = res_v
                else:
                    all_success = False
                    overall_error = result.error
                    break
            except Exception as e:
                step_time = (time.time() - step_start) * 1000
                step_results.append(ProcedureStepResult(
                    step_id=step_id,
                    action=tool_name,
                    success=False,
                    error=str(e),
                    execution_time_ms=step_time,
                ))
                all_success = False
                overall_error = str(e)
                break
        
        exec_duration = (time.time() - start_time) * 1000
        
        # Record execution results in procedure metadata & audit log
        history_record = {
            "version": proc_version.version,
            "success": all_success,
            "steps_count": len(step_results),
            "execution_time_ms": exec_duration,
            "timestamp": int(time.time() * 1000),
            "error": overall_error,
        }
        
        if "execution_history" not in procedure.metadata:
            procedure.metadata["execution_history"] = []
        procedure.metadata["execution_history"].append(history_record)
        # Keep last 20 execution records
        if len(procedure.metadata["execution_history"]) > 20:
            procedure.metadata["execution_history"] = procedure.metadata["execution_history"][-20:]
        
        await self._save_procedure_file(procedure)
        await self._save_index()
        
        audit_logger.log(
            event_type=AuditEventType.TOOL_EXECUTED if all_success else AuditEventType.TOOL_FAILED,
            resource=f"procedure:{procedure_id}",
            action="execute",
            result="success" if all_success else "failure",
            details=history_record,
        )
        
        return ProcedureExecutionResult(
            procedure_id=procedure_id,
            version=proc_version.version,
            success=all_success,
            step_results=step_results,
            error=overall_error,
            execution_time_ms=exec_duration,
        )


@dataclass
class ProcedureStepResult:
    """Result of an individual procedure step."""
    step_id: str
    action: str
    success: bool
    result: Optional[Any] = None
    error: Optional[str] = None
    execution_time_ms: float = 0.0


@dataclass
class ProcedureExecutionResult:
    """Consolidated procedure execution result."""
    procedure_id: str
    version: int
    success: bool
    step_results: List[ProcedureStepResult]
    error: Optional[str] = None
    execution_time_ms: float = 0.0
    recorded_at: int = field(default_factory=lambda: int(time.time() * 1000))