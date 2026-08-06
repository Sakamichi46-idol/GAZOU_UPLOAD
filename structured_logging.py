from __future__ import annotations
import json, logging, time
LOGGER=logging.getLogger('photo_archive.events')
def log_event(event:str, **fields):
    payload={'event':event,**fields}
    LOGGER.info(json.dumps(payload,ensure_ascii=False,default=str,separators=(',',':')))
class timed_event:
    def __init__(self,event:str,**fields): self.event=event; self.fields=fields
    def __enter__(self): self.started=time.perf_counter(); return self
    def __exit__(self,typ,val,tb): log_event(self.event,status='error' if typ else 'ok',duration_ms=round((time.perf_counter()-self.started)*1000,1),error_type=getattr(typ,'__name__','') if typ else '',**self.fields)
