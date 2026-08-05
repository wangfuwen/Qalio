"""Evidence-constrained quality workflow evaluated on the same frozen split."""
from __future__ import annotations
import json, os, time
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI
from score import predict

ROOT=Path(__file__).parent; load_dotenv(ROOT.parent/'.env')
MODEL=os.getenv('QA_EVAL_MODEL','gpt-4o-mini')
PROMPT='''你是客服质检复核 Agent。依据对话识别客服风险，只返回 JSON：
{"label":"越权承诺|售后退款|履约发货|情绪升级|服务规范缺失|正常会话","priority":"P0|P1|P2|—","evidence":"必须逐字引用客服原话；无证据则空字符串","confidence":0到1,"suggestion":"一句动作"}。
仅当客服话术本身有问题时标风险；用户说要投诉不等于客服有风险。'''
def main():
 if not os.getenv('OPENAI_API_KEY'): raise SystemExit('缺少 OPENAI_API_KEY')
 rows=[json.loads(x) for x in (ROOT/'dataset.jsonl').read_text(encoding='utf-8').splitlines() if '"split": "test"' in x]
 out=ROOT/'results'/'copilot_workflow.jsonl'; c=OpenAI()
 with out.open('w',encoding='utf-8') as f:
  for i,row in enumerate(rows,1):
   start=time.perf_counter(); r=c.chat.completions.create(model=MODEL,temperature=0,response_format={'type':'json_object'},messages=[{'role':'system','content':PROMPT},{'role':'user','content':row['dialogue']}]); p=json.loads(r.choices[0].message.content or '{}')
   rule_label,rule_evidence=predict(row['dialogue']); evidence=p.get('evidence','')
   valid_evidence=bool(evidence) and evidence in row['dialogue']
   review=not valid_evidence or (rule_label!='正常会话' and p.get('label')!=rule_label) or float(p.get('confidence',0))<.72
   if rule_label!='正常会话': p.update({'label':rule_label,'evidence':rule_evidence,'confidence':max(.86,float(p.get('confidence',0)))})
   elif not valid_evidence: p.update({'label':'正常会话','priority':'—','evidence':''})
   f.write(json.dumps({'id':row['id'],'prediction':p,'needs_human_review':review,'duration_ms':round((time.perf_counter()-start)*1000),'model':MODEL},ensure_ascii=False)+'\n');f.flush();print(f'[{i}/{len(rows)}] {row["id"]}',flush=True)
 print(out)
if __name__=='__main__':main()
