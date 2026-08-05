"""Single-call LLM baseline. Reads only the frozen test split and preserves raw outputs."""
from __future__ import annotations
import argparse, json, os, time
from pathlib import Path
from openai import OpenAI
from dotenv import load_dotenv

ROOT=Path(__file__).parent
load_dotenv(ROOT.parent / ".env")
MODEL=os.getenv("QA_EVAL_MODEL","gpt-4o-mini")
PROMPT='''你是客服质检员。阅读一段对话，只返回 JSON：
{"label":"越权承诺|售后退款|履约发货|情绪升级|服务规范缺失|正常会话","priority":"P0|P1|P2|—","evidence":"原文中的一整句","confidence":0到1,"suggestion":"一句可执行动作"}。
不要把用户投诉意向本身当作客服过错；只有客服承诺、拒绝、信息不一致或不规范时才命中风险。'''

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--limit",type=int,default=0)
    args=parser.parse_args()
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("缺少 OPENAI_API_KEY。请创建项目 .env 或在终端设置环境变量后重试。")
    client=OpenAI(); out=ROOT/'results'/'llm_baseline.jsonl'; out.parent.mkdir(exist_ok=True)
    rows=[json.loads(x) for x in (ROOT/'dataset.jsonl').read_text(encoding='utf-8').splitlines() if '"split": "test"' in x]
    if args.limit: rows=rows[:args.limit]
    with out.open('w',encoding='utf-8') as f:
      for i,row in enumerate(rows,1):
        started=time.perf_counter()
        r=client.chat.completions.create(model=MODEL,temperature=0,response_format={"type":"json_object"},messages=[{"role":"system","content":PROMPT},{"role":"user","content":row['dialogue']}])
        prediction=json.loads(r.choices[0].message.content or '{}')
        f.write(json.dumps({"id":row['id'],"prediction":prediction,"duration_ms":round((time.perf_counter()-started)*1000),"model":MODEL},ensure_ascii=False)+'\n');f.flush()
        print(f'[{i}/{len(rows)}] {row["id"]}',flush=True)
    print(out)
if __name__=='__main__':main()
