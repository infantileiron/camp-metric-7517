import os,json
from datetime import datetime,timezone
import redis
from fastapi import APIRouter,Depends,HTTPException,Response
from src.config.database import db
from src.models.entities import User,Metric,Fact,Snapshot,Audit,Attribution
from src.schemas.business import Login,MetricInput,FactInput,RunInput,NoteInput,AttributionInput
from src.middlewares.auth import current,editor,admin
from src.utils.security import verify,token
from src.services.business import audit,dump
from src.services.repository import save_metric,save_fact
from src.controllers import business as ensure
router=APIRouter()
@router.post('/auth/login')
def login(data:Login,s=Depends(db)):
 u=s.query(User).filter_by(email=data.email).first()
 if not u or not verify(data.password,u.password):raise HTTPException(401,'账号或密码错误')
 return {'token':token(u),'user':dump(u)}
@router.get('/me')
def me(u=Depends(current)):return dump(u)
@router.get('/metrics')
def metrics(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Metric).order_by(Metric.code,Metric.version).all()]
@router.post('/metrics')
def create_metric(data:MetricInput,u=Depends(editor),s=Depends(db)):
 r=save_metric(s,data);audit(s,u,'建立口径 '+r.code+' v'+str(r.version));return dump(r)
@router.put('/metrics/{i}')
def edit_metric(i:int,data:MetricInput,u=Depends(editor),s=Depends(db)):
 r=save_metric(s,data,i);audit(s,u,'修订口径 '+str(i));return dump(r)
@router.get('/facts')
def facts(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Fact).order_by(Fact.day.desc(),Fact.class_code).all()]
@router.post('/facts')
def create_fact(data:FactInput,u=Depends(editor),s=Depends(db)):
 r=save_fact(s,data);audit(s,u,'录入事实 '+str(r.id));return dump(r)
@router.put('/facts/{i}')
def edit_fact(i:int,data:FactInput,u=Depends(editor),s=Depends(db)):
 r=save_fact(s,data,i);audit(s,u,'修订事实 '+str(i));return dump(r)
@router.get('/snapshots')
def snapshots(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Snapshot).order_by(Snapshot.id.desc()).all()]
@router.post('/snapshots')
def run(data:RunInput,u=Depends(editor),s=Depends(db)):
 m=ensure.metric(s,data.metric_id);baseline=None
 if data.baseline_id:
  b=ensure.snapshot(s,data.baseline_id)
  if b.state!='done':raise HTTPException(409,'仅成功快照可作为比较基线')
  if b.result['metric']['code']!=m.code:raise HTTPException(409,'只能比较同一指标代码的口径版本')
  baseline=dict(id=b.id,date_from=b.date_from,date_to=b.date_to,result={k:b.result[k] for k in ['metric','value','facts_checksum']})
 values=data.model_dump(mode='json');rows=[dump(f) for f in s.query(Fact).filter(Fact.day>=values['date_from'],Fact.day<=values['date_to']).order_by(Fact.day,Fact.class_code).all()]
 frozen={'metric':dump(m),'facts':rows,'baseline':baseline,'date_from':values['date_from'],'date_to':values['date_to']}
 r=Snapshot(**values,result={'snapshot':frozen});s.add(r);s.commit()
 try:redis.Redis.from_url(os.environ['REDIS_URL']).rpush('camp:snapshots',r.id)
 except redis.RedisError:r.state='failed';r.error='队列不可用，请恢复后重新试算';s.commit();raise HTTPException(503,r.error)
 audit(s,u,'提交试算 '+str(r.id));return dump(r)
@router.get('/snapshots/{i}')
def detail(i:int,u=Depends(current),s=Depends(db)):return dump(ensure.snapshot(s,i))
@router.put('/snapshots/{i}/note')
def note(i:int,data:NoteInput,u=Depends(editor),s=Depends(db)):
 r=ensure.snapshot(s,i)
 if r.state!='done' or r.approval=='approved':raise HTTPException(409,'仅成功且未签核快照可编辑意见')
 r.note=data.note;s.commit();audit(s,u,'起草签核意见 '+str(i));return dump(r)
@router.post('/snapshots/{i}/approve')
def approve(i:int,u=Depends(admin),s=Depends(db)):
 r=ensure.snapshot(s,i)
 if r.state!='done' or r.approval=='approved':raise HTTPException(409,'仅成功且未签核快照可签核')
 if r.result['value'] is None:raise HTTPException(409,'零分母undefined快照不能签核，请修正口径')
 if len(r.note)<8:raise HTTPException(409,'先填写至少8字业务解释')
 m=ensure.metric(s,r.metric_id)
 if any(getattr(m,k)!=v for k,v in r.result['metric'].items() if k not in ['locked']):raise HTTPException(409,'口径在试算后已变更，请用当前版本重新试算再签核')
 r.approval='approved';r.approved_by=u.email;m.locked=True;s.commit();audit(s,u,'签核冻结 '+str(i));return dump(r)
@router.get('/snapshots/{i}/export')
def export(i:int,u=Depends(current),s=Depends(db)):
 r=ensure.snapshot(s,i)
 if r.state!='done':raise HTTPException(409,'仅成功快照可导出')
 return Response(json.dumps(dump(r),ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="camp-metric-{i}.json"'})
@router.get('/audits')
def audits(u=Depends(admin),s=Depends(db)):return [dump(r) for r in s.query(Audit).order_by(Audit.id.desc()).limit(100)]

def _frozen_pair(s,data):
 b=ensure.approved_snapshot(s,data.baseline_id);c=ensure.approved_snapshot(s,data.current_id)
 if b.result.get('value') is None or c.result.get('value') is None:raise HTTPException(409,'存在undefined快照，不能归因')
 bm=b.result['metric']['code'];cm=c.result['metric']['code']
 if bm!=cm:raise HTTPException(409,'只能对同一指标代码的两次签核做归因')
 if (str(b.date_from),str(b.date_to))!=(str(c.date_from),str(c.date_to)):raise HTTPException(409,'两次签核的日期窗不同，无法只按口径和日报归因')
 from src.services.attribution import freeze_snapshot
 try:return freeze_snapshot(b),freeze_snapshot(c)
 except ValueError as e:raise HTTPException(409,str(e))


@router.get('/attributions')
def attributions(u=Depends(current),s=Depends(db)):
 return [_public(r) for r in s.query(Attribution).order_by(Attribution.id.desc()).all()]


def _public(row):
 out=dump(row);out.pop('inputs',None);return out

@router.post('/attributions')
def create_attribution(data:AttributionInput,u=Depends(editor),s=Depends(db)):
 baseline,current=_frozen_pair(s,data)
 existing=s.query(Attribution).filter_by(baseline_id=data.baseline_id,current_id=data.current_id).first()
 if existing:
  if existing.state in ('queued','running','done'):return _public(existing)
  # 失败重试复用原记录，不新增归因；输入仍是当初冻结的两次签核。
  existing.state='queued';existing.error='';s.commit()
  try:redis.Redis.from_url(os.environ['REDIS_URL']).rpush('camp:attributions',existing.id)
  except redis.RedisError:
   existing.state='failed';existing.error='队列不可用，请恢复后重试';s.commit();raise HTTPException(503,existing.error)
  audit(s,u,'重试归因 '+str(existing.id));return _public(existing)
 r=Attribution(baseline_id=data.baseline_id,current_id=data.current_id,state='queued',inputs={'baseline':baseline,'current':current},requested_by=u.email,created=datetime.now(timezone.utc).isoformat());s.add(r)
 try:s.commit()
 except Exception:
  s.rollback();existing=s.query(Attribution).filter_by(baseline_id=data.baseline_id,current_id=data.current_id).first()
  if existing:return _public(existing)
  raise
 try:redis.Redis.from_url(os.environ['REDIS_URL']).rpush('camp:attributions',r.id)
 except redis.RedisError:
  r.state='failed';r.error='队列不可用，请恢复后重试';s.commit();raise HTTPException(503,r.error)
 audit(s,u,'提交归因 '+str(r.id));return _public(r)

@router.get('/attributions/{i}')
def attribution(i:int,u=Depends(current),s=Depends(db)):
 row=dump(ensure.attribution(s,i));row.pop('inputs',None);return row

@router.get('/attributions/{i}/export')
def attribution_export(i:int,u=Depends(current),s=Depends(db)):
 r=ensure.attribution(s,i)
 if r.state!='done':raise HTTPException(409,'仅完成的归因可导出')
 return Response(json.dumps(dump(r),ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="camp-attribution-{i}.json"'})
