# -*- coding: utf-8 -*-
"""接口冒烟：验证校验路径与 SSE 错误路径"""
import base64
import json

import httpx

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

base = "http://127.0.0.1:8765"

with httpx.Client(timeout=60) as c:
    # 1) 未配置时生成 -> 400（当前已保存配置，先删除配置文件再测）
    import pathlib
    cfg_path = pathlib.Path(__file__).parent / "data" / "config.json"
    saved = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else None
    cfg_path.unlink(missing_ok=True)
    r = c.post(f"{base}/api/generate", files={"images": ("t.png", PNG, "image/png")}, data={"competitor_bullets": "x"})
    print("no-config generate ->", r.status_code, r.json())

    # 2) 假配置 -> SSE 错误事件
    c.post(f"{base}/api/config", json={"base_url": "http://127.0.0.1:9/v1", "api_key": "sk-test", "model": "test-vl"})
    r = c.post(f"{base}/api/generate", files={"images": ("t.png", PNG, "image/png")}, data={"competitor_bullets": "1. TEST"})
    print("fake-config generate ->", r.status_code, r.headers.get("content-type"))
    events = [json.loads(line[6:]) for line in r.text.split("\n\n") if line.startswith("data: ")]
    print("sse events ->", [(e["type"], e.get("message", "")[:80]) for e in events])

    # 3) 空竞品 -> 400
    r = c.post(f"{base}/api/generate", files={"images": ("t.png", PNG, "image/png")}, data={"competitor_bullets": "  "})
    print("empty bullets ->", r.status_code, r.json())

    # 4) 恢复原配置
    if saved:
        cfg_path.write_text(saved, encoding="utf-8")
        print("config restored")
