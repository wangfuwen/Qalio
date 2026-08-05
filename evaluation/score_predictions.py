from __future__ import annotations
import json,sys
from pathlib import Path
ROOT=Path(__file__).parent
rows={x['id']:x for x in map(json.loads,(ROOT/'dataset.jsonl').read_text(encoding='utf-8').splitlines()) if x['split']=='test'}
preds={x['id']:x for x in map(json.loads,Path(sys.argv[1]).read_text(encoding='utf-8').splitlines())}
tp=fp=fn=ev=0
for id,row in rows.items():
 p=preds[id]['prediction']; actual=row['label']!='正常会话'; got=p.get('label')!='正常会话'; correct=p.get('label')==row['label']
 tp+=actual and got and correct; fp+=got and (not actual or not correct); fn+=actual and (not got or not correct); ev+=bool(row['evidence']) and row['evidence'] in p.get('evidence','')
precision=tp/(tp+fp) if tp+fp else 0; recall=tp/(tp+fn) if tp+fn else 0
print({'cases':len(rows),'precision':round(precision,3),'recall':round(recall,3),'f1':round(2*precision*recall/(precision+recall),3) if precision+recall else 0,'evidence_accuracy':round(ev/len(rows),3),'avg_duration_ms':round(sum(p['duration_ms'] for p in preds.values())/len(preds))})
