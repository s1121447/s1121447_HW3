import os
import json
from datetime import datetime

from flask import Flask, request, abort, jsonify
from dotenv import load_dotenv

import google.generativeai as genai

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage


# =========================
# 載入環境變數
# =========================
load_dotenv()

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET or not GEMINI_API_KEY:
    raise ValueError("請確認 .env 或 Render 環境變數已正確設定")

# Gemini 設定
genai.configure(api_key=GEMINI_API_KEY)

# Flask app
app = Flask(__name__)

# LINE Bot
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 歷史紀錄檔
HISTORY_FILE = "history.json"

# 每次只保留最近幾筆歷史，避免 prompt 太長、太耗額度
MAX_HISTORY_MESSAGES = 6

# Gemini 模型候選，先試前面，失敗再往後 fallback
MODEL_CANDIDATES = [
    "gemini-2.5-flash",
]


# =========================
# 基本首頁（Render 健康檢查用）
# =========================
@app.route("/", methods=["GET"])
def home():
    return "LINE Gemini Bot is running on Render"


# =========================
# 工具函式：history.json 讀寫
# =========================
def load_history():
    if not os.path.exists(HISTORY_FILE):
        return {}

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}


def save_history(history_data):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history_data, f, ensure_ascii=False, indent=2)


def get_user_history(user_id):
    history_data = load_history()
    return history_data.get(user_id, [])


def append_user_history(user_id, role, content):
    history_data = load_history()

    if user_id not in history_data:
        history_data[user_id] = []

    history_data[user_id].append({
        "role": role,
        "content": content,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

    save_history(history_data)


def delete_user_history(user_id):
    history_data = load_history()

    if user_id in history_data:
        del history_data[user_id]
        save_history(history_data)
        return True

    return False


# =========================
# 工具函式：組 prompt
# =========================
def build_prompt_from_history(user_id, user_message):
    history = get_user_history(user_id)

    # 只取最近幾筆，降低額度消耗
    history = history[-MAX_HISTORY_MESSAGES:]

    system_prompt = (
        "你是一個友善、清楚、簡潔的 LINE AI 聊天助理。"
        "請一律使用繁體中文回答。"
        "如果使用者的問題和先前對話有關，請根據歷史內容延續回答。"
        "回答盡量自然，不要太冗長。"
    )

    conversation_text = ""
    for item in history:
        if item["role"] == "user":
            conversation_text += f"使用者：{item['content']}\n"
        elif item["role"] == "assistant":
            conversation_text += f"助理：{item['content']}\n"

    conversation_text += f"使用者：{user_message}\n助理："

    final_prompt = f"{system_prompt}\n\n以下是最近的對話紀錄：\n{conversation_text}"
    return final_prompt


# =========================
# 工具函式：問 Gemini
# =========================
def ask_gemini(user_id, user_message):
    prompt = build_prompt_from_history(user_id, user_message)

    print("=== ask_gemini start ===")
    print("user_id =", user_id)
    print("user_message =", user_message)
    print("prompt preview =", repr(prompt[:300]))

    try:
        model = genai.GenerativeModel("gemini-2.5-flash")
        response = model.generate_content(prompt)

        print("Gemini response object =", response)

        if response is None:
            print("Gemini response is None")
            return "抱歉，目前模型沒有回傳內容，請稍後再試。"

        if hasattr(response, "text"):
            print("Gemini response.text =", repr(response.text))
            if response.text and response.text.strip():
                return response.text.strip()

        print("Gemini response text is empty")
        return "抱歉，目前模型沒有回傳有效內容，請稍後再試。"

    except Exception as e:
        print("gemini-2.5-flash error:", repr(e))

        err = str(e).lower()
        if "429" in err or "quota" in err or "resourceexhausted" in err:
            return "目前 Gemini 免費額度暫時用完了，請稍後再試。"

        return "抱歉，系統目前忙碌中，請稍後再試。"


# =========================
# LINE Webhook
# =========================
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    print("=== /callback received ===")
    print("signature =", signature)
    print("body =", body[:500])

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("InvalidSignatureError")
        abort(400)
    except Exception as e:
        print("Webhook handle error:", repr(e))
        abort(500)

    return "OK"


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text.strip()

    print("=== handle_message ===")
    print("user_id =", user_id)
    print("received message =", user_message)

    bot_reply = ask_gemini(user_id, user_message)

    print("bot_reply =", bot_reply)

    append_user_history(user_id, "user", user_message)

    error_replies = {
        "抱歉，系統目前忙碌中，請稍後再試。",
        "目前 Gemini 免費額度暫時用完了，請稍後再試。",
        "抱歉，目前模型沒有回傳內容，請稍後再試。",
        "抱歉，目前模型沒有回傳有效內容，請稍後再試。"
    }

    if bot_reply not in error_replies:
        append_user_history(user_id, "assistant", bot_reply)

    try:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=bot_reply)
        )
        print("reply_message success")
    except Exception as e:
        print("reply_message error:", repr(e))


# =========================
# RESTful API：GET 歷史
# =========================
@app.route("/history/<user_id>", methods=["GET"])
def get_history(user_id):
    history = get_user_history(user_id)
    return jsonify({
        "user_id": user_id,
        "history": history
    }), 200


# =========================
# RESTful API：DELETE 歷史
# =========================
@app.route("/history/<user_id>", methods=["DELETE"])
def remove_history(user_id):
    deleted = delete_user_history(user_id)

    if deleted:
        return jsonify({
            "message": "History deleted successfully.",
            "user_id": user_id
        }), 200
    else:
        return jsonify({
            "message": "User history not found.",
            "user_id": user_id
        }), 404


# =========================
# 主程式進入點
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)