"""M Core - Main package."""
from .config import settings
from .core import *
from .model_providers import *
from .security import *
from .tools import *
from .agents import *
from .memory import *
from .planning import *
from .orchestration import *
from .api import *

__version__ = "0.1.0-dev"
__all__ = ["settings"]