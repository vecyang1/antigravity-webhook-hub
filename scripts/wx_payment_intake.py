#!/usr/bin/env python3
import subprocess
import json
import logging
import argparse
from typing import List, Dict, Any

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger("wx_payment_intake")

def fetch_wechat_payments(limit: int = 20) -> List[Dict[str, Any]]:
    cmd = ["wx", "search", "支付金额", "-n", str(limit), "--json"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            parsed = json.loads(result.stdout)
            if isinstance(parsed, dict) and "data" in parsed:
                return parsed["data"]
            elif isinstance(parsed, list):
                return [x for x in parsed if isinstance(x, dict)]
            else:
                return []
        else:
            return []
    except Exception as e:
        return []

def parse_payment_record(record: Dict[str, Any]) -> Dict[str, Any]:
    content = record.get("content", "")
    timestamp = record.get("timestamp", 0)
    
    merchant = "Unknown"
    amount = 0.0
    order_type = "unknown"
    
    if "拼多多" in content:
        merchant = "拼多多"
        order_type = "ecommerce"
    elif "生蚝" in content or "凭祥市吴记" in content:
        merchant = "凭祥市吴记新鲜生蚝"
        order_type = "food"
        
    lines = content.split('\n')
    for line in lines:
        if "支付金额" in line or "扣费金额" in line:
            import re
            match = re.search(r'(\d+\.\d+)', line)
            if match:
                amount = float(match.group(1))
                
    return {
        "timestamp": timestamp,
        "merchant": merchant,
        "amount": amount,
        "order_type": order_type,
        "raw_content": content[:100] + "..." if len(content) > 100 else content
    }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5, help="Number of records to fetch")
    parser.add_argument("--dry-run", action="store_true", help="Do not sync, just print")
    args = parser.parse_args()
    
    logger.info(f"Starting WeChat Payment Intake pipeline (limit={args.limit})")
    records = fetch_wechat_payments(limit=args.limit)
    
    parsed_records = []
    for r in records:
        parsed = parse_payment_record(r)
        if parsed["merchant"] != "Unknown":
            parsed_records.append(parsed)
            
    logger.info(f"Successfully processed {len(parsed_records)} valid payment records.")
    
    if args.dry_run:
        for p in parsed_records:
            print(json.dumps(p, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
