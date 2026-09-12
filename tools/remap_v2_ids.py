"""remap_v2_ids.py — 按 id-remap.json 重写 v2 导出文件中的题目 ID。

用途：网站 official-upload 对同题干题执行 keep_both 后会生成 kaoyan_copy_<hash> 新 ID，
studio 导出的解析文件仍是原 ID；v2-import 按 ID 合并前需把原 ID 替换为线上实际 ID。

用法：python tools/remap_v2_ids.py <v2文件> [--remap kaoyan-import/id-remap.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--remap", default=str(Path(__file__).resolve().parent.parent / "kaoyan-import" / "id-remap.json"))
    args = ap.parse_args()

    remap_path = Path(args.remap)
    if not remap_path.exists():
        print("无 id-remap.json，无需改写")
        return 0
    remap = json.loads(remap_path.read_text(encoding="utf-8"))

    path = Path(args.file)
    data = json.loads(path.read_text(encoding="utf-8"))
    changed = 0
    for q in data.get("questions", []):
        new_id = remap.get(q.get("id", ""))
        if new_id:
            q["id"] = new_id
            changed += 1
    if changed:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{path.name}: 改写 {changed} 个 ID")
    return 0


if __name__ == "__main__":
    sys.exit(main())
