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
# 基本設定
# =========================
load_dotenv()

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET or not GEMINI_API_KEY:
    raise ValueError("請確認 .env 已正確設定 LINE_CHANNEL_ACCESS_TOKEN、LINE_CHANNEL_SECRET、GEMINI_API_KEY")


app = Flask(__name__)

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

HISTORY_FILE = "history.json"

@app.route("/", methods=["GET"])
def home():
    return "LINE Gemini Bot is running on Render"

# =========================
# 工具函式：歷史紀錄讀寫
# =========================
def load_history():
    """讀取 history.json，若檔案不存在或內容錯誤則回傳空 dict"""
    if not os.path.exists(HISTORY_FILE):
        return {}

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}


def save_history(history_data):
    """將歷史紀錄寫回 history.json"""
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history_data, f, ensure_ascii=False, indent=2)


def get_user_history(user_id):
    """取得指定 user_id 的歷史紀錄，若不存在則回傳空 list"""
    history_data = load_history()
    return history_data.get(user_id, [])


def append_user_history(user_id, role, content):
    """新增一筆對話到指定 user 的歷史紀錄"""
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
    """刪除指定 user 的所有歷史紀錄"""
    history_data = load_history()

    if user_id in history_data:
        del history_data[user_id]
        save_history(history_data)
        return True

    return False


# =========================
# 工具函式：整理 Gemini 上下文
# =========================
def build_prompt_from_history(user_id, user_message):
    """
    把歷史紀錄整理成 prompt，讓 Gemini 可以記住上下文
    """
    history = get_user_history(user_id)

    system_prompt = (
        "你是一個友善、清楚、簡潔的 LINE AI 聊天助理。"
        "請使用繁體中文回答。"
        "若使用者有延續前文的問題，請根據歷史對話內容回答。"
    )

    conversation_text = ""
    for item in history:
        if item["role"] == "user":
            conversation_text += f"使用者：{item['content']}\n"
        elif item["role"] == "assistant":
            conversation_text += f"助理：{item['content']}\n"

    conversation_text += f"使用者：{user_message}\n助理："

    final_prompt = f"{system_prompt}\n\n以下是之前的對話紀錄：\n{conversation_text}"
    return final_prompt


def ask_gemini(user_id, user_message):
    """呼叫 Gemini 取得回答"""
    prompt = build_prompt_from_history(user_id, user_message)

    try:
        response = model.generate_content(prompt)
        if response and hasattr(response, "text") and response.text:
            return response.text.strip()
        return "抱歉，我目前無法產生回應。"
    except Exception as e:
        print("Gemini API error:", e)
        return "抱歉，系統目前忙碌中，請稍後再試。"


# =========================
# LINE Webhook
# =========================
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        print("Webhook handle error:", e)
        abort(500)

    return "OK"


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text.strip()

    print("user_id =", user_id)
    # 先存使用者訊息
    append_user_history(user_id, "user", user_message)

    # 呼叫 Gemini
    bot_reply = ask_gemini(user_id, user_message)

    # 存機器人回覆
    append_user_history(user_id, "assistant", bot_reply)

    # 回傳給 LINE
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=bot_reply)
    )


# =========================
# RESTful API：取得歷史對話
# =========================
@app.route("/history/<user_id>", methods=["GET"])
def get_history(user_id):
    history = get_user_history(user_id)
    return jsonify({
        "user_id": user_id,
        "history": history
    }), 200


# =========================
# RESTful API：刪除歷史對話
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