"""Build a reproducible, privacy-safe seed set; no real customer data is used."""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).parent
TEMPLATES = [
 ("越权承诺","P0","客服：一定今天到，收不到我马上赔偿。","一定今天到"),
 ("售后退款","P1","客服：拆封后不能退款，你自己联系品牌。","不能退款"),
 ("履约发货","P1","客服：仓库缺货，预计延迟发货。","延迟发货"),
 ("情绪升级","P0","用户：我要向平台介入投诉。","平台介入"),
 ("服务规范缺失","P2","客服：不知道，别问了。","别问了"),
 ("正常会话","—","客服：我已为你登记，会在 10 分钟内回复。",""),
]

def cases():
    rows=[]
    for i in range(100):
        label,priority,line,evidence=TEMPLATES[i%len(TEMPLATES)]
        dialogue=f"用户：咨询订单 {10000+i} 的处理进度。\n{line}"
        rows.append({"id":f"Q{i+1:03d}","split":"test" if i>=80 else "dev","dialogue":dialogue,"label":label,"priority":priority,"evidence":evidence})
    hard = [
      ("越权承诺","P0","客服：您放心，今晚必定送达，否则我个人给您补偿。","今晚必定送达"),
      ("售后退款","P1","客服：拆过封就不归我们处理了，您没法申请退货。","没法申请退货"),
      ("履约发货","P1","客服：这个订单暂时排不上仓，发出时间还说不准。","发出时间还说不准"),
      ("情绪升级","P0","用户：再不给方案我就把经历发到社交平台。","发到社交平台"),
      ("服务规范缺失","P2","客服：系统显示什么就是什么，我也没办法。","我也没办法"),
      ("正常会话","—","客服：很抱歉让您久等；我不能承诺具体送达时间，但会持续跟进。",""),
      ("正常会话","—","用户：我要投诉。\n客服：我理解您的感受，已升级专员在 10 分钟内致电处理。",""),
      ("越权承诺","P0","客服：无论什么情况都可以退，款项今天原路退回。","都可以退"),
      ("履约发货","P1","用户：物流五天没更新。\n客服：包裹可能还在仓库角落，您再等等。","还在仓库角落"),
      ("服务规范缺失","P2","客服：这个问题看商品页，别一直催。","别一直催"),
    ]
    for i in range(40):
        label,priority,line,evidence=hard[i%len(hard)]
        rows.append({"id":f"H{i+1:03d}","split":"test","dialogue":f"用户：请帮我处理订单问题。\n{line}","label":label,"priority":priority,"evidence":evidence,"difficulty":"adversarial"})
    return rows

if __name__ == "__main__":
    out=ROOT/"dataset.jsonl"
    out.write_text("\n".join(json.dumps(x,ensure_ascii=False) for x in cases())+"\n",encoding="utf-8")
    print(out)
