import os,time,json,logging
from pathlib import Path
import redis
from src.config.database import Session
from src.models.entities import Snapshot,Attribution
from src.services.analysis import calculate
from src.services.attribution import attribute
QUEUES=('camp:snapshots','camp:attributions')
ARTIFACTS=os.getenv('ARTIFACTS_DIR','/artifacts')

def connection():
 return redis.Redis.from_url(os.environ['REDIS_URL'])

def recover(r):
 with Session() as s:
  for pending in s.query(Snapshot).filter(Snapshot.state.in_(['queued','running'])).all():
   pending.state='queued';r.rpush('camp:snapshots',pending.id)
  for pending in s.query(Attribution).filter(Attribution.state.in_(['queued','running'])).all():
   pending.state='queued';r.rpush('camp:attributions',pending.id)
  s.commit()

def process_one(queue,i):
 """处理一条任务；非 queued 状态直接跳过，保证队列重复投递不重复计算。"""
 with Session() as s:
  if queue=='camp:snapshots':
   row=s.get(Snapshot,int(i))
   if not row or row.state!='queued':return
   snapshot=row.result['snapshot'];row.state='running';s.commit()
   try:
    row.result={**calculate(snapshot),'snapshot':snapshot};row.state='done';row.error=''
    Path(ARTIFACTS+'/'+str(row.id)+'.json').write_text(json.dumps(row.result,ensure_ascii=False))
   except Exception as e:row.state='failed';row.error=str(e)
   s.commit()
  else:
   row=s.get(Attribution,int(i))
   if not row or row.state!='queued':return
   row.state='running';s.commit()
   try:
    # 只用提交时冻结的两次签核输入重算；不读取现行口径或事实。
    row.result=attribute(dict(row.inputs['baseline']),dict(row.inputs['current']))
    row.state='done';row.error=''
    Path(ARTIFACTS+'/attribution-'+str(row.id)+'.json').write_text(json.dumps(row.result,ensure_ascii=False))
   except Exception as e:row.state='failed';row.error=str(e)
   s.commit()

def main():
 r=connection();recover(r)
 while True:
  Path('/tmp/heartbeat').touch()
  try:
   item=r.blpop(QUEUES,timeout=3)
   if not item:continue
   process_one(item[0].decode(),item[1])
  except Exception:logging.exception('worker');time.sleep(2)

if __name__=='__main__':main()
