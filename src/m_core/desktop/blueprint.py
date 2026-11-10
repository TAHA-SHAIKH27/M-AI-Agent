"""Blueprint System for Interface Knowledge.

Stores and retrieves learned interface information for applications.
Blueprints capture successful interaction patterns and UI structure.
"""
import asyncio
import json
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

import structlog

from m_core.memory import memory_manager
from m_core.memory.base import MemoryTier
from m_core.tools.builtins.visual_automation import WindowInteractTool

logger = structlog.get_logger(__name__)


class BlueprintStatus(str, Enum):
    """Status of a blueprint."""
    ACTIVE = "active"
    STALE = "stale"
    DEPRECATED = "deprecated"
    LEARNING = "learning"


class ElementType(str, Enum):
    """Types of UI elements."""
    BUTTON = "button"
    INPUT = "input"
    TEXT = "text"
    LINK = "link"
    MENU = "menu"
    ICON = "icon"
    IMAGE = "image"
    SCROLLBAR = "scrollbar"
    TAB = "tab"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    DROPDOWN = "dropdown"
    SLIDER = "slider"
    PROGRESSBAR = "progressbar"
    HEADER = "header"
    STATUS = "status"
    WINDOW = "window"
    DIALOG = "dialog"
    UNKNOWN = "unknown"


@dataclass
class BlueprintElement:
    """A UI element in a blueprint."""
    element_id: str
    element_type: ElementType
    name: str  # Human-readable name (e.g., "Save button")
    description: str  # Full description
    selectors: Dict[str, Any] = field(default_factory=dict)  # How to find it (auto_id, title, class, etc.)
    bbox: Optional[Dict[str, int]] = None  # Relative bounding box
    parent_id: Optional[str] = None  # Parent element
    children: List[str] = field(default_factory=list)  # Child element IDs
    interactions: List[str] = field(default_factory=list)  # Supported interactions
    properties: Dict[str, Any] = field(default_factory=dict)  # Additional properties
    confidence: float = 1.0  # Confidence this element exists
    last_seen: int = field(default_factory=lambda: int(time.time()))
    verified: bool = False  # Whether verified in recent interaction
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BlueprintElement":
        return cls(**data)


@dataclass
class BlueprintAction:
    """A recorded successful action sequence."""
    action_id: str
    name: str  # e.g., "save_document"
    description: str
    steps: List[Dict[str, Any]]  # Sequence of steps
    preconditions: Dict[str, Any] = field(default_factory=dict)
    postconditions: Dict[str, Any] = field(default_factory=dict)
    applications: List[str] = field(default_factory=list)  # App names this works for
    success_count: int = 0
    failure_count: int = 0
    last_used: int = field(default_factory=lambda: int(time.time()))
    created_at: int = field(default_factory=lambda: int(time.time()))
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BlueprintAction":
        return cls(**data)


@dataclass
class ApplicationBlueprint:
    """Complete blueprint for an application."""
    blueprint_id: str
    app_name: str  # e.g., "notepad", "word", "chrome"
    process_name: str  # e.g., "notepad.exe", "WINWORD.EXE"
    window_class: str = ""  # Window class name
    version: str = "1.0"
    status: BlueprintStatus = BlueprintStatus.LEARNING
    elements: Dict[str, BlueprintElement] = field(default_factory=dict)
    actions: Dict[str, BlueprintAction] = field(default_factory=dict)
    window_patterns: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: int = field(default_factory=lambda: int(time.time()))
    updated_at: int = field(default_factory=lambda: int(time.time()))
    last_verified: int = 0
    success_count: int = 0
    failure_count: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "blueprint_id": self.blueprint_id,
            "app_name": self.app_name,
            "process_name": self.process_name,
            "window_class": self.window_class,
            "version": self.version,
            "status": self.status.value,
            "elements": {k: v.to_dict() for k, v in self.elements.items()},
            "actions": {k: v.to_dict() for k, v in self.actions.items()},
            "window_patterns": self.window_patterns,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_verified": self.last_verified,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ApplicationBlueprint":
        bp = cls(
            blueprint_id=data["blueprint_id"],
            app_name=data["app_name"],
            process_name=data["process_name"],
            window_class=data.get("window_class", ""),
            version=data.get("version", "1.0"),
            status=BlueprintStatus(data.get("status", "learning")),
            window_patterns=data.get("window_patterns", []),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", int(time.time())),
            updated_at=data.get("updated_at", int(time.time())),
            last_verified=data.get("last_verified", 0),
            success_count=data.get("success_count", 0),
            failure_count=data.get("failure_count", 0),
        )
        bp.elements = {k: BlueprintElement.from_dict(v) for k, v in data.get("elements", {}).items()}
        bp.actions = {k: BlueprintAction.from_dict(v) for k, v in data.get("actions", {}).items()}
        return bp


class BlueprintStore:
    """Persistent storage for blueprints."""
    
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or Path("data/blueprints.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()
    
    def _init_db(self) -> None:
        """Initialize database schema."""
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        
        cursor = self._conn.cursor()
        
        # Blueprints table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS blueprints (
                blueprint_id TEXT PRIMARY KEY,
                app_name TEXT NOT NULL,
                process_name TEXT NOT NULL,
                window_class TEXT,
                version TEXT DEFAULT '1.0',
                status TEXT DEFAULT 'learning',
                data TEXT NOT NULL,  -- JSON serialized blueprint
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_verified INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                failure_count INTEGER DEFAULT 0
            )
        """)
        
        # Element index for fast lookup
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS blueprint_elements (
                element_id TEXT PRIMARY KEY,
                blueprint_id TEXT NOT NULL,
                element_type TEXT NOT NULL,
                name TEXT NOT NULL,
                selectors TEXT NOT NULL,  -- JSON
                bbox TEXT,  -- JSON
                parent_id TEXT,
                confidence REAL DEFAULT 1.0,
                verified INTEGER DEFAULT 0,
                last_seen INTEGER NOT NULL,
                FOREIGN KEY (blueprint_id) REFERENCES blueprints(blueprint_id)
            )
        """)
        
        # Actions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS blueprint_actions (
                action_id TEXT PRIMARY KEY,
                blueprint_id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                steps TEXT NOT NULL,  -- JSON
                applications TEXT NOT NULL,  -- JSON
                success_count INTEGER DEFAULT 0,
                failure_count INTEGER DEFAULT 0,
                last_used INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (blueprint_id) REFERENCES blueprints(blueprint_id)
            )
        """)
        
        # Interaction history for learning
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS interaction_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                blueprint_id TEXT NOT NULL,
                element_id TEXT,
                action_type TEXT NOT NULL,
                success INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                details TEXT,  -- JSON
                FOREIGN KEY (blueprint_id) REFERENCES blueprints(blueprint_id)
            )
        """)
        
        self._conn.commit()
    
    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._init_db()
        return self._conn
    
    def save_blueprint(self, blueprint: ApplicationBlueprint) -> None:
        """Save or update a blueprint."""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        blueprint.updated_at = int(time.time())
        data = json.dumps(blueprint.to_dict())
        
        cursor.execute("""
            INSERT OR REPLACE INTO blueprints
            (blueprint_id, app_name, process_name, window_class, version, status,
             data, created_at, updated_at, last_verified, success_count, failure_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            blueprint.blueprint_id,
            blueprint.app_name,
            blueprint.process_name,
            blueprint.window_class,
            blueprint.version,
            blueprint.status.value,
            json.dumps(blueprint.to_dict()),
            blueprint.created_at,
            blueprint.updated_at,
            blueprint.last_verified,
            blueprint.success_count,
            blueprint.failure_count,
        ))
        
        # Save elements
        for elem in blueprint.elements.values():
            cursor.execute("""
                INSERT OR REPLACE INTO blueprint_elements
                (element_id, blueprint_id, element_type, name, selectors, bbox, parent_id, confidence, verified, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                elem.element_id,
                blueprint.blueprint_id,
                elem.element_type.value,
                elem.name,
                json.dumps(elem.selectors),
                json.dumps(elem.bbox) if elem.bbox else None,
                elem.parent_id,
                elem.confidence,
                1 if elem.verified else 0,
                elem.last_seen,
            ))
        
        # Save actions
        for action in blueprint.actions.values():
            cursor.execute("""
                INSERT OR REPLACE INTO blueprint_actions
                (action_id, blueprint_id, name, description, steps, applications,
                 success_count, failure_count, last_used, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                action.action_id,
                blueprint.blueprint_id,
                action.name,
                action.description,
                json.dumps(action.steps),
                json.dumps(action.applications),
                action.success_count,
                action.failure_count,
                action.last_used,
                action.created_at,
            ))
        
        conn.commit()
    
    def get_blueprint(self, blueprint_id: str) -> Optional[ApplicationBlueprint]:
        """Load a blueprint by ID."""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute("SELECT data FROM blueprints WHERE blueprint_id = ?", (blueprint_id,))
        row = cursor.fetchone()
        
        if not row:
            return None
        
        return ApplicationBlueprint.from_dict(json.loads(row[0]))
    
    def get_blueprint_for_app(self, process_name: str) -> Optional[ApplicationBlueprint]:
        """Get the most recent blueprint for an application."""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT data FROM blueprints 
            WHERE process_name = ? 
            ORDER BY updated_at DESC 
            LIMIT 1
        """, (process_name,))
        row = cursor.fetchone()
        
        if not row:
            return None
        
        return ApplicationBlueprint.from_dict(json.loads(row[0]))
    
    def find_blueprint_by_window(self, window_title: str, process_name: Optional[str] = None) -> Optional[ApplicationBlueprint]:
        """Find blueprint matching a window."""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        if process_name:
            cursor.execute("""
                SELECT data FROM blueprints 
                WHERE process_name = ? AND status != 'deprecated'
                ORDER BY updated_at DESC LIMIT 1
            """, (process_name,))
        else:
            # Try to match by window pattern
            cursor.execute("""
                SELECT data FROM blueprints 
                WHERE status != 'deprecated'
                ORDER BY updated_at DESC LIMIT 10
            """)
            rows = cursor.fetchall()
            for row in rows:
                bp = ApplicationBlueprint.from_dict(json.loads(row[0]))
                for pattern in bp.window_patterns:
                    if pattern.get("title_pattern", "").lower() in window_title.lower():
                        return bp
            return None
        
        row = cursor.fetchone()
        if not row:
            return None
        return ApplicationBlueprint.from_dict(json.loads(row[0]))
    
    def list_blueprints(self, app_name: Optional[str] = None) -> List[ApplicationBlueprint]:
        """List all blueprints."""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        if app_name:
            cursor.execute("SELECT data FROM blueprints WHERE app_name = ? ORDER BY updated_at DESC", (app_name,))
        else:
            cursor.execute("SELECT data FROM blueprints ORDER BY updated_at DESC")
        
        rows = cursor.fetchall()
        return [ApplicationBlueprint.from_dict(json.loads(row[0])) for row in rows]
    
    def delete_blueprint(self, blueprint_id: str) -> bool:
        """Delete a blueprint."""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM blueprint_actions WHERE blueprint_id = ?", (blueprint_id,))
        cursor.execute("DELETE FROM blueprint_elements WHERE blueprint_id = ?", (blueprint_id,))
        cursor.execute("DELETE FROM blueprints WHERE blueprint_id = ?", (blueprint_id,))
        
        conn.commit()
        return cursor.rowcount > 0
    
    def record_interaction(
        self,
        blueprint_id: str,
        element_id: Optional[str],
        action_type: str,
        success: bool,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record an interaction for learning."""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO interaction_history
            (blueprint_id, element_id, action_type, success, timestamp, details)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            blueprint_id,
            element_id,
            action_type,
            1 if success else 0,
            int(time.time()),
            json.dumps(details) if details else None,
        ))
        
        conn.commit()
    
    def get_interaction_stats(self, blueprint_id: str) -> Dict[str, Any]:
        """Get interaction statistics for a blueprint."""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                COUNT(*) as total,
                SUM(success) as successes,
                COUNT(*) - SUM(success) as failures,
                MAX(timestamp) as last_interaction
            FROM interaction_history
            WHERE blueprint_id = ?
        """, (blueprint_id,))
        
        row = cursor.fetchone()
        if row:
            return {
                "total": row[0],
                "successes": row[1] or 0,
                "failures": (row[0] - (row[1] or 0)),
                "last_interaction": row[3],
            }
        return {"total": 0, "successes": 0, "failures": 0, "last_interaction": 0}
    
    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


class BlueprintManager:
    """High-level blueprint management."""
    
    def __init__(self, store: Optional[BlueprintStore] = None):
        self.store = store or BlueprintStore()
        self._window_tool = WindowInteractTool()
        self._current_blueprint: Optional[ApplicationBlueprint] = None
        self._element_cache: Dict[str, Any] = {}
    
    async def get_or_create_blueprint(
        self,
        app_name: str,
        process_name: str,
        window_title: Optional[str] = None,
    ) -> ApplicationBlueprint:
        """Get existing blueprint or create new one."""
        # Try to find existing blueprint
        blueprint = self.store.get_blueprint_for_app(process_name)
        
        if blueprint:
            # Check if blueprint is still valid
            if await self._verify_blueprint(blueprint):
                logger.info(f"Using existing blueprint for {process_name}")
                return blueprint
            else:
                logger.info(f"Blueprint for {process_name} is stale, creating new one")
        
        # Create new blueprint
        blueprint = ApplicationBlueprint(
            blueprint_id=str(uuid.uuid4()),
            app_name=process_name.replace(".exe", ""),
            process_name=process_name,
            status=BlueprintStatus.LEARNING,
        )
        
        if window_title:
            blueprint.window_patterns.append({
                "title_pattern": window_title,
                "timestamp": int(time.time()),
            })
        
        self.store.save_blueprint(blueprint)
        self._current_blueprint = blueprint
        
        logger.info(f"Created new blueprint for {process_name}")
        return blueprint
    
    async def _verify_blueprint(self, blueprint: ApplicationBlueprint) -> bool:
        """Verify if a blueprint is still valid by checking the application."""
        # Check if application is running and window matches
        try:
            from m_core.tools import tool_registry
            from m_core.tools.schemas import ToolExecutionContext
            from m_core.security import consent_manager, ConsentScope
            
            tool = tool_registry.get_tool('window_list')
            ctx = ToolExecutionContext(
                tool_name='window_list',
                arguments={'filter_process': blueprint.process_name},
                correlation_id='bp-verify',
                user_id='system',
                permissions_granted=['app:control'],
                consent_manager=consent_manager
            )
            result = await tool.execute(ctx)
            
            if not result.success or not result.result.get('windows'):
                return False
            
            # Check if any window matches our patterns
            for window in result.result['windows']:
                if window.get('is_visible'):
                    for pattern in blueprint.window_patterns:
                        if pattern.get('title_pattern', '').lower() in window['title'].lower():
                            return True
            
            return False
        except Exception:
            return False
    
    async def learn_from_interaction(
        self,
        blueprint: ApplicationBlueprint,
        window_title: str,
        action: str,
        element_identifier: Dict[str, Any],
        success: bool,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Learn from a successful or failed interaction."""
        try:
            # Find the element in the window
            from m_core.tools import tool_registry
            from m_core.tools.schemas import ToolExecutionContext
            from m_core.security import consent_manager, ConsentScope
            
            tool = tool_registry.get_tool('window_interact')
            ctx = ToolExecutionContext(
                tool_name='window_interact',
                arguments={
                    'action': 'find_control',
                    'window_title': window_title,
                    'control_identifier': element_identifier,
                    'approved': True,
                },
                correlation_id='learn-elem',
                user_id='system',
                permissions_granted=['automation:window'],
                consent_manager=consent_manager
            )
            result = await tool.execute(ctx)
            
            if result.success and result.result.get('control'):
                control = result.result['control']
                element_id = f"elem_{uuid.uuid4().hex[:8]}"
                
                element = BlueprintElement(
                    element_id=element_id,
                    element_type=ElementType(control['type'].lower()) if control['type'].lower() in [e.value for e in ElementType] else ElementType.UNKNOWN,
                    name=control.get('title', '') or f"Element {element_id}",
                    description=f"{control.get('type', 'Element')}: {control.get('title', 'Unnamed')}",
                    selectors={
                        'type': control.get('type'),
                        'title': control.get('title'),
                        'auto_id': control.get('auto_id'),
                        'control_id': control.get('control_id'),
                        'class_name': control.get('class_name'),
                    },
                    bbox=control.get('rect'),
                    confidence=0.8 if control.get('verified') else 0.5,
                    verified=True,
                )
                
                blueprint.elements[element_id] = element
                blueprint.updated_at = int(time.time())
                
                # Record the interaction
                self.store.record_interaction(
                    blueprint.blueprint_id,
                    element_id,
                    action,
                    True,  # We found it successfully
                    details={
                        'window_title': window_title,
                        'identifier': element_identifier,
                        'control_type': control.get('type'),
                    },
                )
                
                # Update blueprint
                self._save_blueprint_changes(blueprint)
                
                logger.info(f"Learned element {element_id} in {blueprint.app_name}")
                
        except Exception as e:
            logger.warning(f"Failed to learn from interaction: {e}")
    
    def _save_blueprint_changes(self, blueprint: ApplicationBlueprint) -> None:
        """Save blueprint changes to store."""
        self.store.save_blueprint(blueprint)
    
    async def find_element(
        self,
        blueprint: ApplicationBlueprint,
        window_title: str,
        description: str,
    ) -> Optional[BlueprintElement]:
        """Find an element in a blueprint matching a description."""
        description_lower = description.lower()
        
        for element in blueprint.elements.values():
            # Check name
            if description.lower() in element.name.lower():
                return element
            # Check description
            if description.lower() in element.description.lower():
                return element
            # Check selectors
            for key, value in element.selectors.items():
                if isinstance(value, str) and description.lower() in value.lower():
                    return element
        
        return None
    
    async def get_action(
        self,
        blueprint: ApplicationBlueprint,
        action_name: str,
    ) -> Optional[BlueprintAction]:
        """Get a known action from blueprint."""
        return blueprint.actions.get(action_name)
    
    async def record_action(
        self,
        blueprint: ApplicationBlueprint,
        name: str,
        description: str,
        steps: List[Dict[str, Any]],
        applications: Optional[List[str]] = None,
    ) -> BlueprintAction:
        """Record a successful action sequence."""
        action = BlueprintAction(
            action_id=str(uuid.uuid4()),
            name=name,
            description=description,
            steps=steps,
            applications=applications or [blueprint.process_name],
        )
        
        blueprint.actions[name] = action
        blueprint.updated_at = int(time.time())
        self._save_blueprint_changes(blueprint)
        
        return action
    
    async def verify_and_update_element(
        self,
        blueprint: ApplicationBlueprint,
        element_id: str,
        window_title: str,
        found: bool,
    ) -> bool:
        """Verify an element still exists and update confidence."""
        element = blueprint.elements.get(element_id)
        if not element:
            return False
        
        if found:
            element.confidence = min(1.0, element.confidence + 0.1)
            element.verified = True
            element.last_seen = int(time.time())
        else:
            element.confidence = max(0.1, element.confidence - 0.2)
            element.verified = False
        
        blueprint.updated_at = int(time.time())
        self._save_blueprint_changes(blueprint)
        
        return found
    
    def get_blueprint_stats(self, blueprint: ApplicationBlueprint) -> Dict[str, Any]:
        """Get statistics for a blueprint."""
        stats = {
            "blueprint_id": blueprint.blueprint_id,
            "app_name": blueprint.app_name,
            "process_name": blueprint.process_name,
            "status": blueprint.status.value,
            "element_count": len(blueprint.elements),
            "action_count": len(blueprint.actions),
            "verified_elements": sum(1 for e in blueprint.elements.values() if e.verified),
            "total_interactions": blueprint.success_count + blueprint.failure_count,
            "success_rate": blueprint.success_count / max(1, blueprint.success_count + blueprint.failure_count),
            "created_at": blueprint.created_at,
            "updated_at": blueprint.updated_at,
        }
        
        # Add interaction stats from store
        interaction_stats = self.store.get_interaction_stats(blueprint.blueprint_id)
        stats.update(interaction_stats)
        
        return stats
    
    def promote_blueprint(self, blueprint: ApplicationBlueprint) -> None:
        """Promote blueprint from learning to active."""
        if blueprint.status == BlueprintStatus.LEARNING:
            if blueprint.success_count >= 3 and blueprint.success_count > blueprint.failure_count * 2:
                blueprint.status = BlueprintStatus.ACTIVE
                blueprint.updated_at = int(time.time())
                self.store.save_blueprint(blueprint)
                logger.info(f"Promoted blueprint {blueprint.blueprint_id} to ACTIVE")
    
    def mark_stale(self, blueprint: ApplicationBlueprint) -> None:
        """Mark blueprint as stale."""
        blueprint.status = BlueprintStatus.STAL
        blueprint.updated_at = int(time.time())
        self.store.save_blueprint(blueprint)


# Convenience function
async def create_blueprint_manager() -> BlueprintManager:
    """Create and initialize a blueprint manager."""
    return BlueprintManager()


# Export
__all__ = [
    "BlueprintManager",
    "BlueprintStore",
    "ApplicationBlueprint",
    "BlueprintElement",
    "BlueprintAction",
    "BlueprintStatus",
    "ElementType",
    "create_blueprint_manager",
]