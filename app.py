# -*- coding: utf-8 -*-
"""Amazon Listing 一键生成 — FastAPI 后端"""
import base64
import json
import re
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from openai import AsyncOpenAI

from prompts import BANNED_TERMS, MARKETPLACE_LANGUAGES, build_messages

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
CONFIG_PATH = DATA_DIR / "config.json"
STATIC_DIR = ROOT / "static"

MAX_IMAGES = 8
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 单图 10MB

DATA_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Amazon Listing Generator")


# ---------- 配置 ----------

class ConfigIn(BaseModel):
    base_url: str
    api_key: str
    model: str
    deepl_api_key: str = ""


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except Exception:
            return {}
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_client(cfg: Optional[dict] = None) -> tuple[AsyncOpenAI, dict]:
    cfg = cfg or load_config()
    if not (cfg.get("base_url") and cfg.get("api_key") and cfg.get("model")):
        raise HTTPException(400, "请先在设置中填写 base_url / api_key / model")
    return AsyncOpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"]), cfg


def mask_key(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}****{key[-4:]}"


@app.get("/api/config")
def get_config():
    cfg = load_config()
    return {
        "base_url": cfg.get("base_url", ""),
        "model": cfg.get("model", ""),
        "api_key_masked": mask_key(cfg["api_key"]) if cfg.get("api_key") else "",
        "has_api_key": bool(cfg.get("api_key")),
        "deepl_api_key_masked": mask_key(cfg["deepl_api_key"]) if cfg.get("deepl_api_key") else "",
        "has_deepl_key": bool(cfg.get("deepl_api_key")),
        "marketplaces": list(MARKETPLACE_LANGUAGES.keys()),
    }


@app.post("/api/config")
async def set_config(cfg_in: ConfigIn):
    old = load_config()
    api_key = cfg_in.api_key.strip()
    if api_key == "__KEEP__":  # 前端留空时保留旧 key
        api_key = old.get("api_key", "")
    deepl_key = cfg_in.deepl_api_key.strip()
    if deepl_key == "__KEEP__":
        deepl_key = old.get("deepl_api_key", "")
    cfg = {
        "base_url": cfg_in.base_url.strip().rstrip("/"),
        "api_key": api_key,
        "model": cfg_in.model.strip(),
        "deepl_api_key": deepl_key,
    }
    if not (cfg["base_url"] and cfg["api_key"] and cfg["model"]):
        raise HTTPException(400, "base_url / api_key / model 均不能为空")
    save_config(cfg)
    return {"ok": True}


class ProbeIn(BaseModel):
    """测试/取列表时用表单当前值（空字段回退已保存配置）"""
    base_url: str = ""
    api_key: str = ""
    model: str = ""


def resolve_form_config(form: ProbeIn) -> dict:
    saved = load_config()
    base_url = form.base_url.strip().rstrip("/") or saved.get("base_url", "")
    api_key = form.api_key.strip()
    if not api_key:  # 留空 → 用已保存 key
        api_key = saved.get("api_key", "")
    model = form.model.strip() or saved.get("model", "")
    if not (base_url and api_key and model):
        raise HTTPException(400, "请填写 base_url / api_key / model 后再操作")
    return {"base_url": base_url, "api_key": api_key, "model": model}


@app.get("/api/models")
async def list_models():
    client, _ = get_client()
    try:
        res = await client.models.list()
        return {"models": sorted(m.id for m in res.data)}
    except Exception as e:
        raise HTTPException(502, f"获取模型列表失败：{e}")


@app.post("/api/models")
async def list_models_from_form(form: ProbeIn):
    cfg = resolve_form_config(form)
    client = AsyncOpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"])
    try:
        res = await client.models.list()
        return {"models": sorted(m.id for m in res.data)}
    except Exception as e:
        raise HTTPException(502, f"获取模型列表失败：{e}")


def model_vision_warning(models, target: str) -> str:
    """若 API 返回了 input_modalities 且目标模型不含 image，给出提醒（非标准字段，缺省则跳过）"""
    for m in models:
        if m.id != target:
            continue
        mods = getattr(m, "input_modalities", None)
        if mods is None:
            mods = (getattr(m, "model_extra", None) or {}).get("input_modalities")
        if mods and "image" not in mods:
            return f"模型 {target} 不支持图片输入（仅 {'/'.join(mods)}），无法识别商品图片，请更换视觉模型"
        return ""
    return ""


@app.post("/api/test")
async def test_connection(form: ProbeIn | None = None):
    if form is None:
        client, cfg = get_client()
    else:
        cfg = resolve_form_config(form)
        client = AsyncOpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"])
    try:
        res = await client.models.list()
        return {
            "ok": True,
            "model": cfg["model"],
            "warning": model_vision_warning(res.data, cfg["model"]),
        }
    except HTTPException:
        raise
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}


# ---------- 生成 ----------

# 各分段允许的标记变体（大小写不敏感；支持 [X] / ## X / **X** / X: / 裸 X 行）
SECTION_NAMES = {
    "item_name": r"ITEM[ _]NAME|TITLE",
    "item_highlights": r"ITEM[ _]HIGHLIGHTS",
    "bullet_points": r"BULLET[ _]POINTS|BULLETS",
    "description": r"DESCRIPTION|PRODUCT[ _]DESCRIPTION",
    "search_terms": r"SEARCH[ _]?TERMS|KEYWORDS|BACKEND[ _]KEYWORDS",
}


def strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def strip_think_blocks(text: str) -> str:
    """剥离思考内容（部分兼容 API 会把 think 标签混进正文）"""
    o = chr(60) + "think" + chr(62)
    c = chr(60) + "/think" + chr(62)
    text = re.sub(re.escape(o) + ".*?" + re.escape(c), "", text, flags=re.S | re.I)
    return re.sub(re.escape(o) + ".*\Z", "", text, flags=re.S | re.I)



def parse_sections(text: str) -> dict:
    text = strip_think_blocks(strip_code_fence(text))
    hits = []  # (start, end, key)
    for key, names in SECTION_NAMES.items():
        pat = re.compile(
            rf"^[ \t]*(?:\[\s*|\*\*\s*|#+\s*)?(?:{names})(?:\s*\]|\s*\*\*)?[ \t]*:?[ \t]*$",
            re.M | re.I,
        )
        for m in pat.finditer(text):
            hits.append((m.start(), m.end(), key))
    hits.sort()
    sections = {}
    for i, (s, e, key) in enumerate(hits):
        if key in sections:
            continue  # 同名标记取第一段
        body_end = hits[i + 1][0] if i + 1 < len(hits) else len(text)
        sections[key] = text[e:body_end].strip()
    return sections


def compliance_warnings(sections: dict) -> list[str]:
    """黑名单词边界扫描（提示人工复核，不阻断）"""
    hits = set()
    for text in sections.values():
        low = text.lower()
        for term in BANNED_TERMS:
            if term.startswith("#"):
                if term in low:
                    hits.add(term)
            elif re.search(rf"\b{re.escape(term)}\b", low):
                hits.add(term)
    if hits:
        return [f"合规提醒：输出疑似包含违禁词 {', '.join(sorted(hits))}，请人工复核"]
    return []


async def deepl_translate(texts: list[str], api_key: str) -> list[str]:
    """DeepL API 批量翻译为简体中文（免费版 key 以 :fx 结尾）"""
    host = "https://api-free.deepl.com" if api_key.endswith(":fx") else "https://api.deepl.com"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{host}/v2/translate",
            headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
            json={"text": texts, "target_lang": "ZH"},
        )
        r.raise_for_status()
        return [t["text"] for t in r.json()["translations"]]


async def read_images(files: list[UploadFile]) -> list[str]:
    if not files:
        raise HTTPException(400, "请至少上传一张商品图片")
    if len(files) > MAX_IMAGES:
        raise HTTPException(400, f"图片最多 {MAX_IMAGES} 张")
    urls = []
    for f in files:
        data = await f.read()
        if not data:
            continue
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(400, f"图片 {f.filename} 超过 10MB，请压缩后重试")
        mime = f.content_type or "image/jpeg"
        if not mime.startswith("image/"):
            raise HTTPException(400, f"不支持的文件类型：{f.filename}")
        b64 = base64.b64encode(data).decode()
        urls.append(f"data:{mime};base64,{b64}")
    if not urls:
        raise HTTPException(400, "图片内容为空")
    return urls


def sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@app.post("/api/generate")
async def generate(
    images: list[UploadFile] = File(...),
    competitor_bullets: str = Form(...),
    marketplace: str = Form("US"),
    product_name: str = Form(""),
    notes: str = Form(""),
):
    if marketplace not in MARKETPLACE_LANGUAGES:
        raise HTTPException(400, f"不支持的站点：{marketplace}")
    if not competitor_bullets.strip():
        raise HTTPException(400, "请填写竞品五点描述")

    client, cfg = get_client()
    image_urls = await read_images(images)
    messages = build_messages(
        competitor_bullets=competitor_bullets,
        marketplace=marketplace,
        product_name=product_name,
        notes=notes,
        image_data_urls=image_urls,
    )

    async def stream():
        full: list[str] = []
        finish_reason = None
        try:
            resp = await client.chat.completions.create(
                model=cfg["model"],
                messages=messages,
                stream=True,
                stream_options={"include_usage": False},
                temperature=0.7,
                max_tokens=16384,
            )
            async for chunk in resp:
                if chunk.choices:
                    ch = chunk.choices[0]
                    if ch.finish_reason:
                        finish_reason = ch.finish_reason
                    delta = ch.delta.content
                    if delta:
                        full.append(delta)
                        yield sse({"type": "delta", "text": delta})
            text = "".join(full)
            sections = parse_sections(text)
            if not sections:
                if finish_reason == "length":
                    yield sse({"type": "error", "message": "输出被 max_tokens 截断（思考过长），请重试", "raw": text})
                else:
                    yield sse({"type": "error", "message": "模型输出未包含预期的分段标记，请重试或更换模型", "raw": text})
            else:
                warnings = ["输出被截断，部分内容可能不完整"] if finish_reason == "length" else []
                warnings.extend(compliance_warnings(sections))
                yield sse({"type": "done", "sections": sections, "raw": text, "warning": "；".join(warnings)})
                # DeepL 中文对照（可选，失败不影响原文结果）
                deepl_key = cfg.get("deepl_api_key", "")
                if deepl_key and sections:
                    keys = [k for k in sections if sections[k]]
                    try:
                        zh = await deepl_translate([sections[k] for k in keys], deepl_key)
                        yield sse({"type": "translated", "sections": dict(zip(keys, zh))})
                    except Exception as e:
                        yield sse({"type": "translate_error", "message": f"DeepL 翻译失败：{e}"})
        except HTTPException:
            raise
        except Exception as e:
            message = f"{type(e).__name__}: {e}"
            yield sse({"type": "error", "message": message})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------- 静态页面 ----------

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
