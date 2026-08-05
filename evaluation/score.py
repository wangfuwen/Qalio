from __future__ import annotations
import json
from pathlib import Path

ROOT=Path(__file__).parent
KEYWORDS={"越权承诺":["一定今天到","马上赔偿"],"售后退款":["不能退款"],"履约发货":["延迟发货","缺货"],"情绪升级":["平台介入","投诉"],"服务规范缺失":["别问了","不知道"]}
def predict(text):
    for label,words in KEYWORDS.items():
        hit=next((x for x in words if x in text),None)
        if hit:return label,hit
    return "正常会话",""
def main():
    rows=[json.loads(x) for x in (ROOT/'dataset.jsonl').read_text(encoding='utf-8').splitlines()]
    test=[x for x in rows if x['split']=='test']; tp=fp=fn=ev=0
    for x in test:
        pred,e=predict(x['dialogue']); pos=x['label']!='正常会话'; got=pred!='正常会话'
        tp+=pos and got and pred==x['label']; fp+=got and (not pos or pred!=x['label']); fn+=pos and (not got or pred!=x['label']); ev+=bool(e) and e==x['evidence']
    precision=tp/(tp+fp) if tp+fp else 0; recall=tp/(tp+fn) if tp+fn else 0; f1=2*precision*recall/(precision+recall) if precision+recall else 0
    print({"test_cases":len(test),"precision":round(precision,3),"recall":round(recall,3),"f1":round(f1,3),"evidence_accuracy":round(ev/len(test),3)})
if __name__=='__main__':main()
