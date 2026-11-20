from fastapi import HTTPException
from src.models.entities import Metric,Snapshot,Attribution
def metric(s,i):
 x=s.get(Metric,i)
 if not x:raise HTTPException(404,'口径版本不存在')
 return x
def snapshot(s,i):
 x=s.get(Snapshot,i)
 if not x:raise HTTPException(404,'试算快照不存在')
 return x
def approved_snapshot(s,i):
 x=snapshot(s,i)
 if x.state!='done' or x.approval!='approved':raise HTTPException(409,'仅已成功签核的快照可做归因')
 return x
def attribution(s,i):
 x=s.get(Attribution,i)
 if not x:raise HTTPException(404,'归因记录不存在')
 return x
