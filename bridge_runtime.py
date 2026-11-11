"""Identify the source generation loaded by the bridge without reading user data."""
from __future__ import annotations
import ast
import hashlib
from pathlib import Path


def source_revision(root: Path) -> str:
    """Hash the entry point and its local Python import closure, including lazy imports."""
    root=Path(root)
    pending=['lan_bridge.py']
    sources={}
    while pending:
        name=pending.pop()
        if name in sources:
            continue
        data=(root/name).read_bytes()
        sources[name]=data
        for node in ast.walk(ast.parse(data,filename=name)):
            if isinstance(node,ast.Import):
                imports=[item.name for item in node.names]
            elif isinstance(node,ast.ImportFrom) and node.module:
                imports=[node.module]
            else:
                continue
            for module in imports:
                dependency=module.split('.',1)[0]+'.py'
                if dependency not in sources and (root/dependency).is_file():
                    pending.append(dependency)
    digest=hashlib.sha256()
    for name,data in sorted(sources.items()):
        digest.update(name.encode()+b'\0'+len(data).to_bytes(8,'big')+data)
    return digest.hexdigest()
