#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ticket Plus 遠大售票系統 - 雲端網頁版監控中心 (免開電腦 24H 雲端執行版)
支援部署於：Hugging Face Spaces (Docker 免費 16GB RAM)、Zeabur、Render、Railway
"""

from collections import deque
from datetime import datetime, timedelta, timezone
import os
import random
import threading
import time
from typing import Deque, Dict, List, Tuple

from flask import Flask, jsonify, render_template_string
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

app = Flask(__name__)

# 台灣時區 (UTC+8)
TW_TZ = timezone(timedelta(hours=8))

# ==============================================================================
# 基本監控與通知設定 (支援從雲端環境變數讀取，或直接使用下方預設值)
# ==============================================================================
TARGET_URL = os.getenv(
    "TARGET_URL",
    "https://ticketplus.com.tw/activity/af2bd66d8fe65d78963cbb5bdc85771c",
)
EVENT_TITLE = os.getenv("EVENT_TITLE", "田馥甄 要去什麼地方 2026台北小巨蛋演唱會")

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
RANDOM_JITTER_SECONDS = (1, 5)

SESSION_DATES = [
    "2026-12-12",
    "2026-12-13",
    "2026-12-15",
    "2026-12-18",
    "2026-12-19",
    "2026-12-20",
]

# Resend API 設定
ENABLE_RESEND_EMAIL = True
RESEND_API_KEY = os.getenv(
    "RESEND_API_KEY", "re_d2BZbP3F_5bqPxbHTjKyFhVW1eJaNWKNN"
)
RESEND_TO_EMAILS = [
    email.strip()
    for email in os.getenv("RESEND_TO_EMAILS", "hatikvah0930@hotmail.com").split(",")
    if email.strip()
]
RESEND_FROM_EMAIL = os.getenv(
    "RESEND_FROM_EMAIL", "Ticket Monitor <onboarding@resend.dev>"
)

# ntfy.sh 手機推播設定 (選用)
ENABLE_NTFY_PUSH = os.getenv("ENABLE_NTFY_PUSH", "false").lower() == "true"
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "hebe_ticket_2026_change_me")

# ==============================================================================
# 全域狀態儲存 (供網頁儀表板即時顯示)
# ==============================================================================
state_lock = threading.Lock()
check_trigger_event = threading.Event()

monitor_state = {
    "is_checking": False,
    "run_count": 0,
    "last_check_time": "尚未執行",
    "next_check_time": "即將開始",
    "has_tickets": False,
    "last_summary": "系統啟動中，準備進行第一次檢查...",
    "session_status": {d: "等待檢查..." for d in SESSION_DATES},
    "logs": deque(maxlen=50),  # 保留最近 50 筆紀錄
}


def now_tw_str() -> str:
    return datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")


def add_log(level: str, message: str) -> None:
    with state_lock:
        monitor_state["logs"].appendleft(
            {"time": now_tw_str(), "level": level, "message": message}
        )


def create_cloud_webdriver() -> webdriver.Chrome:
    """建立雲端 Linux Docker / 本機相容的無頭 Chrome WebDriver。"""
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    )
    options = ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"--user-agent={user_agent}")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--blink-settings=imagesEnabled=false")  # 不載入圖片，大幅節省雲端記憶體與加速載入
    options.add_argument("--js-flags=--max-old-space-size=256")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--log-level=3")

    chrome_bin = os.getenv("CHROME_BIN")
    if chrome_bin and os.path.exists(chrome_bin):
        options.binary_location = chrome_bin

    chromedriver_path = os.getenv("CHROMEDRIVER_PATH")
    if chromedriver_path and os.path.exists(chromedriver_path):
        service = ChromeService(executable_path=chromedriver_path)
        return webdriver.Chrome(service=service, options=options)

    return webdriver.Chrome(options=options)


def check_ticket_status() -> Tuple[bool, List[str], str, Dict[str, str]]:
    """開啟遠大售票活動頁面並逐場次檢查售票狀態（排除非關鍵位置之「立即購買」）。"""
    driver = None
    per_session_map: Dict[str, str] = {d: "未載入" for d in SESSION_DATES}
    try:
        driver = create_cloud_webdriver()
        driver.set_page_load_timeout(30)
        driver.get(TARGET_URL)

        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

        for _ in range(20):
            body_text = driver.find_element(By.TAG_NAME, "body").text
            if any(d in body_text for d in SESSION_DATES):
                break
            time.sleep(1)
        time.sleep(2)

        page_text = driver.find_element(By.TAG_NAME, "body").text

        purchase_keywords = ["立即購買", "立即訂購", "選購", "熱賣中", "剩餘"]
        sold_out_keywords = ["銷售一空", "已售完", "已完售", "暫停販售", "尚未開賣"]

        available_sessions: List[str] = []
        loaded_dates: List[str] = []
        sold_out_dates: List[str] = []

        for idx, session_date in enumerate(SESSION_DATES):
            if session_date not in page_text:
                continue

            loaded_dates.append(session_date)

            after_date_text = page_text.split(session_date, 1)[1]
            if idx + 1 < len(SESSION_DATES) and SESSION_DATES[idx + 1] in after_date_text:
                row_status_text = after_date_text.split(SESSION_DATES[idx + 1], 1)[0]
            else:
                row_status_text = after_date_text[:120]

            is_sold_out = any(kw in row_status_text for kw in sold_out_keywords)
            matched_buy_kw = [kw for kw in purchase_keywords if kw in row_status_text]

            if is_sold_out and not matched_buy_kw:
                sold_out_dates.append(session_date)
                per_session_map[session_date] = "銷售一空"
            elif matched_buy_kw:
                kw_str = ", ".join(matched_buy_kw)
                per_session_map[session_date] = f"🔥 可購買 ({kw_str})"
                available_sessions.append(
                    f"場次 {session_date} 偵測到購票關鍵字：{kw_str}"
                )
            elif not is_sold_out:
                clean_status = " ".join(row_status_text.split())
                per_session_map[session_date] = f"⚠️ 狀態變更 ({clean_status[:25]})"
                available_sessions.append(
                    f"場次 {session_date} 未顯示銷售一空 (當前內容：{clean_status})"
                )

        for session_date in loaded_dates:
            xpath_query = (
                f"//*[contains(text(), '{session_date}')]"
                f"/ancestor::*[string-length(normalize-space(.)) < 300]"
                f"//button"
            )
            row_buttons = driver.find_elements(By.XPATH, xpath_query)
            for btn in row_buttons:
                btn_text = btn.text.strip()
                if btn_text and any(kw in btn_text for kw in purchase_keywords):
                    per_session_map[session_date] = f"🔥 按鈕：{btn_text}"
                    msg = f"場次 {session_date} 偵測到可點擊購票按鈕：「{btn_text}」"
                    if msg not in available_sessions:
                        available_sessions.append(msg)

        if len(loaded_dates) == 0:
            preview = " ".join(page_text.split())[:80]
            return (
                False,
                [],
                f"未偵測到場次日期 (畫面摘要：{preview or '空白'})，略過本次判定",
                per_session_map,
            )

        has_tickets = len(available_sessions) > 0
        if has_tickets:
            summary = " | ".join(available_sessions)
        else:
            summary = (
                f"目前全數售罄 (已精準檢查 {len(loaded_dates)} 個場次列，"
                f"{len(sold_out_dates)} 個皆為銷售一空)"
            )

        return has_tickets, available_sessions, summary, per_session_map

    except Exception as exc:
        return False, [], f"檢查發生例外錯誤：{exc}", per_session_map
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def send_resend_email(details: List[str], is_test: bool = False) -> Tuple[bool, str]:
    """透過 Resend REST API 發送通知信。"""
    if not ENABLE_RESEND_EMAIL:
        return False, "Resend 未啟用"

    api_key = RESEND_API_KEY.strip()
    if not api_key.startswith("re_"):
        return False, "未設定有效的 RESEND_API_KEY"

    now_str = now_tw_str()
    if is_test:
        subject = f"✅【雲端測試成功】{EVENT_TITLE} 雲端網頁版監控運作中"
        heading = "✅ 雲端監控中心測試信發送成功！"
        heading_color = "#188038"
    else:
        subject = f"🚨【釋票通知】{EVENT_TITLE} 有票可購買！"
        heading = "🚨 偵測到演唱會釋票！"
        heading_color = "#d93025"

    detail_items_html = "".join(f"<li>{item}</li>" for item in details)
    html_body = f"""
    <html>
      <body style="font-family: 'Microsoft JhengHei', sans-serif; line-height: 1.6; color: #202124;">
        <h2 style="color: {heading_color};">{heading}</h2>
        <p><strong>活動名稱：</strong>{EVENT_TITLE}</p>
        <p><strong>發送時間：</strong>{now_str} (台灣時間)</p>
        <p><strong>狀態明細：</strong></p>
        <ul>{detail_items_html}</ul>
        <p style="margin-top: 24px;">
          <a href="{TARGET_URL}"
             style="background-color: #1a73e8; color: #ffffff; padding: 12px 20px;
                    text-decoration: none; border-radius: 6px; font-weight: bold;">
            👉 立即開啟 Ticket Plus 售票頁面
          </a>
        </p>
      </body>
    </html>
    """

    payload = {
        "from": RESEND_FROM_EMAIL,
        "to": RESEND_TO_EMAILS,
        "subject": subject,
        "html": html_body,
    }

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
        if resp.status_code in (200, 201):
            res_id = resp.json().get("id", "")
            return True, f"已寄出至 {', '.join(RESEND_TO_EMAILS)} (ID: {res_id})"
        return False, f"HTTP {resp.status_code}: {resp.text}"
    except Exception as exc:
        return False, str(exc)


def send_ntfy_push(details: List[str]) -> None:
    if not ENABLE_NTFY_PUSH or "change_me" in NTFY_TOPIC:
        return
    msg = "\n".join(details) + f"\n點擊前往：{TARGET_URL}"
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC.strip()}",
            data=msg.encode("utf-8"),
            headers={
                "Title": f"釋票通知：{EVENT_TITLE}".encode("utf-8"),
                "Priority": "urgent",
                "Tags": "rotating_light,ticket",
                "Click": TARGET_URL,
            },
            timeout=15,
        )
    except Exception:
        pass


def background_monitor_loop() -> None:
    """背景常駐執行緒：每分鐘定期檢查票況。"""
    add_log("INFO", "🚀 雲端背景監控執行緒已啟動！")
    while True:
        with state_lock:
            monitor_state["is_checking"] = True

        has_tickets, details, summary, per_session_map = check_ticket_status()
        now_str = now_tw_str()

        with state_lock:
            monitor_state["run_count"] += 1
            run_no = monitor_state["run_count"]
            monitor_state["is_checking"] = False
            monitor_state["last_check_time"] = now_str
            monitor_state["has_tickets"] = has_tickets
            monitor_state["last_summary"] = summary
            monitor_state["session_status"] = per_session_map

        if has_tickets:
            add_log("ALERT", f"[第 {run_no} 次] 🎉 {summary}")
            ok, msg = send_resend_email(details, is_test=False)
            add_log("INFO" if ok else "ERROR", f"[Resend 通知] {msg}")
            send_ntfy_push(details)
        else:
            add_log("INFO", f"[第 {run_no} 次] {summary}")

        jitter = random.randint(*RANDOM_JITTER_SECONDS)
        sleep_seconds = CHECK_INTERVAL_SECONDS + jitter
        next_dt = datetime.now(TW_TZ) + timedelta(seconds=sleep_seconds)
        with state_lock:
            monitor_state["next_check_time"] = next_dt.strftime("%Y-%m-%d %H:%M:%S")

        # 等待下次週期，或由網頁端「立即檢查」按鈕提前喚醒
        check_trigger_event.wait(timeout=sleep_seconds)
        check_trigger_event.clear()


# 啟動背景監控執行緒
monitor_thread = threading.Thread(target=background_monitor_loop, daemon=True)
monitor_thread.start()


# ==============================================================================
# 網頁版 UI 與 API 路由
# ==============================================================================
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Ticket Plus 雲端監控中心</title>
  <style>
    :root {
      --bg: #0f172a;
      --card: #1e293b;
      --border: #334155;
      --text: #f8fafc;
      --muted: #94a3b8;
      --green: #22c55e;
      --red: #ef4444;
      --blue: #3b82f6;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; padding: 20px;
      font-family: 'Microsoft JhengHei', -apple-system, BlinkMacSystemFont, sans-serif;
      background: var(--bg); color: var(--text);
    }
    .container { max-width: 900px; margin: 0 auto; }
    .header {
      background: var(--card); border: 1px solid var(--border);
      border-radius: 14px; padding: 20px; margin-bottom: 20px;
      display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 14px;
    }
    .title-group h1 { margin: 0 0 6px 0; font-size: 1.35rem; }
    .title-group p { margin: 0; color: var(--muted); font-size: 0.9rem; }
    .badge {
      padding: 8px 14px; border-radius: 999px; font-weight: bold; font-size: 0.9rem;
      display: inline-flex; align-items: center; gap: 8px;
    }
    .badge.ok { background: rgba(34,197,94,0.15); color: #4ade80; border: 1px solid rgba(34,197,94,0.4); }
    .badge.alert { background: rgba(239,68,68,0.2); color: #f87171; border: 1px solid rgba(239,68,68,0.5); }
    .grid-stats {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 14px; margin-bottom: 20px;
    }
    .stat-card {
      background: var(--card); border: 1px solid var(--border);
      border-radius: 12px; padding: 16px;
    }
    .stat-label { color: var(--muted); font-size: 0.85rem; margin-bottom: 6px; }
    .stat-value { font-size: 1.15rem; font-weight: bold; }
    .sessions-card, .logs-card {
      background: var(--card); border: 1px solid var(--border);
      border-radius: 14px; padding: 20px; margin-bottom: 20px;
    }
    .section-head {
      display: flex; justify-content: space-between; align-items: center;
      margin-bottom: 14px; flex-wrap: wrap; gap: 10px;
    }
    .section-head h2 { margin: 0; font-size: 1.1rem; }
    .btn-group { display: flex; gap: 10px; flex-wrap: wrap; }
    button, .btn-link {
      background: var(--blue); color: #fff; border: none; border-radius: 8px;
      padding: 9px 15px; font-size: 0.9rem; font-weight: bold; cursor: pointer;
      text-decoration: none; display: inline-block;
    }
    button.secondary { background: #475569; }
    button:hover, .btn-link:hover { opacity: 0.9; }
    .session-grid {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 12px;
    }
    .session-item {
      background: #0f172a; border: 1px solid var(--border); border-radius: 10px;
      padding: 12px 16px; display: flex; justify-content: space-between; align-items: center;
    }
    .session-date { font-weight: bold; }
    .session-status.soldout { color: var(--muted); }
    .session-status.available { color: #f87171; font-weight: bold; }
    .log-list {
      max-height: 320px; overflow-y: auto; background: #0f172a;
      border: 1px solid var(--border); border-radius: 10px; padding: 12px;
      font-family: Consolas, monospace; font-size: 0.85rem;
    }
    .log-row { padding: 6px 0; border-bottom: 1px solid #1e293b; }
    .log-row.ALERT { color: #f87171; font-weight: bold; }
    .log-row.ERROR { color: #fb923c; }
    .log-time { color: var(--muted); margin-right: 8px; }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div class="title-group">
        <h1>🎯 Ticket Plus 雲端監控中心</h1>
        <p>{{ event_title }}</p>
      </div>
      <div id="status-badge" class="badge ok">🟢 雲端每分鐘自動監控中</div>
    </div>

    <div class="grid-stats">
      <div class="stat-card">
        <div class="stat-label">累積檢查次數</div>
        <div class="stat-value" id="run-count">-</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">最後檢查時間</div>
        <div class="stat-value" id="last-check">-</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">預計下次檢查</div>
        <div class="stat-value" id="next-check">-</div>
      </div>
    </div>

    <div class="sessions-card">
      <div class="section-head">
        <h2>📅 各場次即時售票狀態</h2>
        <div class="btn-group">
          <button onclick="triggerCheck()">🔄 立即手動檢查</button>
          <button class="secondary" onclick="sendTestEmail()">📨 發送 Resend 測試信</button>
          <a class="btn-link" href="{{ target_url }}" target="_blank">👉 開啟售票網</a>
        </div>
      </div>
      <p id="last-summary" style="color: var(--muted); margin-top: 0; margin-bottom: 14px; font-size: 0.9rem;"></p>
      <div class="session-grid" id="session-grid"></div>
    </div>

    <div class="logs-card">
      <div class="section-head">
        <h2>📜 最近檢查紀錄 (自動每 5 秒刷新畫面)</h2>
      </div>
      <div class="log-list" id="log-list"></div>
    </div>
  </div>

  <script>
    async function fetchStatus() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        document.getElementById('run-count').textContent = data.run_count + ' 次' + (data.is_checking ? ' (檢查中...)' : '');
        document.getElementById('last-check').textContent = data.last_check_time;
        document.getElementById('next-check').textContent = data.next_check_time;
        document.getElementById('last-summary').textContent = '最新結論：' + data.last_summary;

        const badge = document.getElementById('status-badge');
        if (data.has_tickets) {
          badge.className = 'badge alert';
          badge.textContent = '🚨 偵測到釋票！快搶票！';
        } else {
          badge.className = 'badge ok';
          badge.textContent = data.is_checking ? '🔄 正在檢查網頁...' : '🟢 雲端每分鐘自動監控中';
        }

        const grid = document.getElementById('session-grid');
        grid.innerHTML = '';
        for (const [date, status] of Object.entries(data.session_status)) {
          const isSoldOut = status.includes('銷售一空') || status.includes('等待') || status.includes('未載入');
          grid.innerHTML += `
            <div class="session-item">
              <span class="session-date">${date}</span>
              <span class="session-status ${isSoldOut ? 'soldout' : 'available'}">${status}</span>
            </div>
          `;
        }

        const logList = document.getElementById('log-list');
        logList.innerHTML = data.logs.map(item => `
          <div class="log-row ${item.level}">
            <span class="log-time">[${item.time}]</span>${item.message}
          </div>
        `).join('');
      } catch (e) {
        console.error(e);
      }
    }

    async function triggerCheck() {
      await fetch('/api/check-now', { method: 'POST' });
      fetchStatus();
    }

    async function sendTestEmail() {
      const res = await fetch('/api/test-email', { method: 'POST' });
      const data = await res.json();
      alert(data.message);
      fetchStatus();
    }

    fetchStatus();
    setInterval(fetchStatus, 5000);
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(
        DASHBOARD_HTML, event_title=EVENT_TITLE, target_url=TARGET_URL
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": now_tw_str()})


@app.route("/api/status")
def api_status():
    with state_lock:
        return jsonify(
            {
                "is_checking": monitor_state["is_checking"],
                "run_count": monitor_state["run_count"],
                "last_check_time": monitor_state["last_check_time"],
                "next_check_time": monitor_state["next_check_time"],
                "has_tickets": monitor_state["has_tickets"],
                "last_summary": monitor_state["last_summary"],
                "session_status": monitor_state["session_status"],
                "logs": list(monitor_state["logs"]),
            }
        )


@app.route("/api/check-now", methods=["POST"])
def api_check_now():
    check_trigger_event.set()
    return jsonify({"ok": True, "message": "已觸發立即檢查"})


@app.route("/api/test-email", methods=["POST"])
def api_test_email():
    ok, msg = send_resend_email(
        ["這是從「Ticket Plus 雲端網頁版監控中心」手動觸發的測試通知信！"],
        is_test=True,
    )
    add_log("INFO" if ok else "ERROR", f"[手動測試信] {msg}")
    return jsonify({"ok": ok, "message": msg})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "7860"))
    app.run(host="0.0.0.0", port=port, debug=False)
