import re
from typing import Sequence
from pathlib import Path


def cat(path: Path) -> str:
    try:
        with open(path, 'r') as fp:
            return fp.read()
    except FileNotFoundError:
        return ''

def grep(val, pattern) -> Sequence:
    return re.search(pattern, val).groups()
