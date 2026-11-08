import msgspec
from msgspec.structs import replace
from typing import Optional, List, Dict, Any

class Test(msgspec.Struct, frozen=True):
    x: int = 1
    y: str = 'test'

t = Test()
print('Original:', t)
try:
    t2 = replace(t, x=2)
    print('Replaced:', t2)
except Exception as e:
    print(f"Error: {e}")