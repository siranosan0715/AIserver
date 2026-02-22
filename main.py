from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import datetime
import uvicorn

app = FastAPI(title="AI Proxy Server")

# 1日のリクエスト数をカウントするためのインメモリ・ストレージ
# 実際はRenderが再起動するとリセットされますが、1日上限を簡易的に管理します。
request_counts = {
    "date": datetime.date.today().isoformat(),
    "gemini": 0,
    "perplexity": 0
}

LIMIT = 100

def check_and_reset_counts():
    today = datetime.date.today().isoformat()
    if request_counts["date"] != today:
        request_counts["date"] = today
        request_counts["gemini"] = 0
        request_counts["perplexity"] = 0

class AskRequest(BaseModel):
    prompt: str
    
# ※後で実際の非公式APIへの通信処理（curl_cffi等）を統合します。

@app.get("/ping")
def ping():
    check_and_reset_counts()
    return {
        "status": "ok",
        "date": request_counts["date"],
        "counts": {
            "gemini": request_counts["gemini"],
            "perplexity": request_counts["perplexity"]
        },
        "limits": {
            "gemini": LIMIT,
            "perplexity": LIMIT
        },
        "remaining": {
            "gemini": max(0, LIMIT - request_counts["gemini"]),
            "perplexity": max(0, LIMIT - request_counts["perplexity"])
        },
        "region": os.getenv("REGION", "Unknown")
    }

import time
import json
import uuid
import re
import os
import curl_cffi.requests as cffi_requests
import requests
from dotenv import load_dotenv

load_dotenv()
GEMINI_COOKIE = os.getenv("GEMINI_COOKIE", "")
REGION = os.getenv("REGION", "Unknown")

ai_session = requests.Session()
if GEMINI_COOKIE:
    ai_session.headers.update({"Cookie": GEMINI_COOKIE})
ai_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
})

ai_state = {"bl": None, "f_sid": None, "last_init": 0}

def init_gemini_session():
    try:
        res = ai_session.get("https://gemini.google.com/", timeout=15)
        text = res.text
        bl_match = re.search(r'"cfb2h":"(.*?)"', text)
        sid_match = re.search(r'"FdrFJe":"(.*?)"', text)
        if not sid_match:
            sid_match = re.search(r'"SNlM0e":"(.*?)"', text)
            
        if bl_match and sid_match:
            ai_state["bl"] = bl_match.group(1)
            ai_state["f_sid"] = sid_match.group(1)
            ai_state["last_init"] = time.time()
            return True
        return False
    except Exception as e:
        print(f"init_gemini_session error: {e}")
        return False

@app.post("/ask/gemini")
def ask_gemini(req: AskRequest):
    check_and_reset_counts()
    if request_counts["gemini"] >= LIMIT:
        raise HTTPException(status_code=429, detail="Gemini rate limit exceeded for today.")
    
    if ai_state["bl"] is None or ai_state["f_sid"] is None or (time.time() - ai_state["last_init"] > 900):
        success = init_gemini_session()
        if not success:
            raise HTTPException(status_code=500, detail="Gemini Session Initialization Failed. (Cookie may be invalid or missing)")

    f_req = [None, json.dumps([
        [req.prompt, 0, None, None, None, None, 0],
        ["ja"],
        ["", "", "", None, None, None, None, None, None, ""],
        "",
        ""
    ])]

    try:
        response = ai_session.post(
            'https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate',
            params={'bl': ai_state['bl'], 'f.sid': ai_state['f_sid'], 'hl': 'ja', 'rt': 'c'},
            data={'f.req': json.dumps(f_req)},
            timeout=60
        )
        
        answer_text = ""
        for line in response.text.split('\n'):
            if 'wrb.fr' in line:
                try:
                    outer = json.loads(line)
                    inner = json.loads(outer[0][2])
                    text_res = inner[4][0][1][0]
                    answer_text = text_res.replace('\\n', '\n')
                    break
                except Exception:
                    pass
                    
        if not answer_text:
            init_gemini_session()
            raise HTTPException(status_code=500, detail="Empty response from Gemini")
            
        request_counts["gemini"] += 1
        return {"text": answer_text, "model": "gemini"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/ask/perplexity")
def ask_perplexity(req: AskRequest):
    check_and_reset_counts()
    if request_counts["perplexity"] >= LIMIT:
        raise HTTPException(status_code=429, detail="Perplexity rate limit exceeded for today.")
    
    session_uuid = str(uuid.uuid4())
    dev_id = uuid.uuid4().hex[:16]
    
    url = "https://www.perplexity.ai/rest/sse/perplexity_ask"
    payload = {
        "query_str": req.prompt,
        "params": {
            "source": "android",
            "version": "2.17",
            "frontend_uuid": session_uuid,
            "android_device_id": dev_id,
            "mode": "concise",
            "is_related_query": False,
            "is_voice_to_voice": False,
            "timezone": "Asia/Tokyo",
            "language": "ja-JP",
            "query_source": "home",
            "is_incognito": False,
            "use_schematized_api": True,
            "send_back_text_in_streaming_api": False,
            "supported_block_use_cases": [
                "answer_modes", "finance_widgets", "inline_assets", "inline_entity_cards",
                "inline_images", "knowledge_cards", "media_items", "place_widgets",
                "placeholder_cards", "search_result_widgets", "shopping_widgets",
                "sports_widgets", "prediction_market_widgets", "maps_preview"
            ],
            "sources": ["web"],
            "model_preference": "turbo"
        }
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Origin": "https://www.perplexity.ai",
        "Referer": "https://www.perplexity.ai/"
    }

    try:
        response = cffi_requests.post(url, json=payload, headers=headers, impersonate="chrome")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Perplexity connection error: {str(e)}")
    
    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail=f"{response.status_code}: Perplexity API Error")

    try:
        answer_text = ""
        for line in response.text.splitlines():
            if line.startswith("data: "):
                data_str = line[6:]
                try:
                    data_json = json.loads(data_str)
                    if "text" in data_json:
                        steps = json.loads(data_json["text"])
                        for step in steps:
                            if step.get("step_type") == "FINAL":
                                raw_answer = step.get("content", {}).get("answer", "")
                                if raw_answer:
                                    try:
                                        answer_obj = json.loads(raw_answer)
                                        answer_text = answer_obj.get("answer", raw_answer)
                                    except json.JSONDecodeError:
                                        answer_text = raw_answer
                except (json.JSONDecodeError, KeyError):
                    pass

        if not answer_text:
            raise HTTPException(status_code=500, detail="Empty response from Perplexity")
            
        request_counts["perplexity"] += 1
        return {"text": answer_text, "model": "perplexity"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
