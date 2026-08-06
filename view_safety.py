"""Discord View structural validation used by diagnostics/tests."""
from __future__ import annotations
from typing import Any

def validate_view(view:Any)->list[str]:
    issues=[]; rows={}
    for item in list(getattr(view,'children',[]) or []):
        row=getattr(item,'row',None)
        if row is None: continue
        if not 0<=int(row)<=4: issues.append(f'row範囲外: {row}')
        rows.setdefault(int(row),[]).append(item)
        opts=getattr(item,'options',None)
        if opts is not None and len(opts)>25: issues.append(f'SelectOption超過: {len(opts)}')
    for row,items in rows.items():
        selects=[x for x in items if hasattr(x,'options')]
        if selects and len(items)>1: issues.append(f'row {row}: Selectと他部品が競合')
        if not selects and len(items)>5: issues.append(f'row {row}: Button超過 {len(items)}')
    if len(getattr(view,'children',[]) or [])>25: issues.append('View部品数が25超過')
    return issues
