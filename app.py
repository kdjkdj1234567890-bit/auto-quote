#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
편한 견적서 — "같은 말 100번 반복하지 마세요."
사장님용 견적·예약 데모 (Flask + SQLite, 외부 API 없음)

문구 원칙: 개발자 용어 금지, 전부 사장님 말로. 톤은 따뜻하게
("편하게", "안심", "걱정 마세요"). "자동화"라는 단어는 쓰지 않습니다.
"""
import json
import os
import re
import secrets
import sqlite3
import traceback
from datetime import date, datetime, timedelta

from flask import (Flask, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "quote.db")

app = Flask(__name__)
app.secret_key = "pyeonhan-quote-demo-key"
app.config["JSON_AS_ASCII"] = False

# ---------------------------------------------------------------------------
# 업종 · 단가표 정의 (전부 사장님 말로)
# ---------------------------------------------------------------------------
BIZ_LIST = ["청소", "이사", "인테리어"]
BIZ_EMOJI = {"청소": "🧹", "이사": "🚚", "인테리어": "🏠"}
BIZ_DESC = {
    "청소": "입주청소·거주청소, 평수대로 바로 견적",
    "이사": "거리·평수·층수 넣으면 바로 견적",
    "인테리어": "도배·장판·리모델링, 평수대로 바로 견적",
}
JOB_LABEL = {"wallpaper": "도배", "floor": "장판", "remodel": "리모델링"}

# 사장님이 /owner 에서 직접 바꾸는 단가표 항목들
FIELD_DEFS = {
    "청소": [
        {"key": "base_per_pyeong", "label": "평당 기본 요금", "unit": "원 / 평", "default": 12000},
        {"key": "opt_ac", "label": "에어컨 분해청소 추가금", "unit": "원", "default": 50000},
        {"key": "opt_mold", "label": "곰팡이 제거 추가금", "unit": "원 / 곳", "default": 30000},
        {"key": "opt_veranda", "label": "베란다 청소 추가금", "unit": "원", "default": 20000},
    ],
    "이사": [
        {"key": "base_fee", "label": "기본 출장비", "unit": "원", "default": 100000},
        {"key": "per_km", "label": "거리 추가금", "unit": "원 / km", "default": 3000},
        {"key": "per_pyeong", "label": "평수 추가금", "unit": "원 / 평", "default": 10000},
        {"key": "per_floor", "label": "층수 추가금 (2층부터, 한 층마다)", "unit": "원 / 층", "default": 20000},
        {"key": "opt_ladder", "label": "사다리차 추가금", "unit": "원", "default": 100000},
        {"key": "opt_packing", "label": "포장 추가금", "unit": "원", "default": 150000},
        {"key": "opt_ac_move", "label": "에어컨 이전·설치 추가금", "unit": "원", "default": 80000},
    ],
    "인테리어": [
        {"key": "job_wallpaper", "label": "도배 평당 요금", "unit": "원 / 평", "default": 15000},
        {"key": "job_floor", "label": "장판 평당 요금", "unit": "원 / 평", "default": 20000},
        {"key": "job_remodel", "label": "리모델링 평당 요금", "unit": "원 / 평", "default": 150000},
        {"key": "opt_silk", "label": "실크벽지 업그레이드", "unit": "원 / 평", "default": 8000},
        {"key": "opt_molding", "label": "몰딩 시공 추가금", "unit": "원", "default": 200000},
        {"key": "opt_led", "label": "LED 조명 교체 추가금", "unit": "원", "default": 150000},
    ],
}
VAT_MODES = ["별도", "포함"]
QUOTE_VALID_DAYS = 7  # 견적 유효기간

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS biz_config (
    biz TEXT PRIMARY KEY,
    config TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quote_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    biz TEXT NOT NULL,
    summary TEXT NOT NULL,
    total INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    biz TEXT NOT NULL,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    hope_date TEXT NOT NULL,
    summary TEXT NOT NULL,
    total INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    name TEXT DEFAULT '',
    referred_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def default_config(biz):
    cfg = {f["key"]: f["default"] for f in FIELD_DEFS[biz]}
    cfg["vat_mode"] = "별도"
    return cfg


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    for biz in BIZ_LIST:
        row = db.execute("SELECT 1 FROM biz_config WHERE biz=?", (biz,)).fetchone()
        if not row:
            db.execute("INSERT INTO biz_config (biz, config) VALUES (?, ?)",
                       (biz, json.dumps(default_config(biz), ensure_ascii=False)))
    db.commit()
    db.close()


def _ensure_referrals_table():
    """기존 DB 마이그레이션: referrals 테이블이 없으면 추가 (기존 DB 유지)."""
    try:
        db = sqlite3.connect(DB_PATH)
        db.execute("""CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT DEFAULT '',
            referred_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )""")
        db.commit()
        db.close()
    except Exception:
        pass


_ensure_referrals_table()


def load_config(biz):
    db = get_db()
    row = db.execute("SELECT config FROM biz_config WHERE biz=?", (biz,)).fetchone()
    if not row:
        return default_config(biz)
    cfg = json.loads(row["config"])
    base = default_config(biz)
    base.update(cfg)
    return base


# ---------------------------------------------------------------------------
# 계산 — 금액의 유일한 기준 (손님 화면·예약·사장님 화면 모두 여기서)
# ---------------------------------------------------------------------------
def won(n):
    return f"{int(n):,}원"


app.template_filter("won")(won)


def _int(v, default=0, lo=None, hi=None):
    try:
        v = int(float(v))
    except (TypeError, ValueError):
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _bool(v):
    return str(v).lower() in ("1", "true", "on", "yes")


def calc_quote(biz, cfg, raw):
    """raw: 손님이 고른 값 dict → 항목별 내역·합계·부가세·유효기간"""
    lines = []
    parts = []  # 이력용 짧은 요약 조각

    if biz == "청소":
        p = _int(raw.get("pyeong"), 30, 5, 100)
        rate = int(cfg["base_per_pyeong"])
        lines.append((f"기본 청소 ({p}평 × {won(rate)}/평)", p * rate))
        parts.append(f"{p}평")
        if _bool(raw.get("ac")):
            lines.append(("에어컨 분해청소", int(cfg["opt_ac"])))
            parts.append("에어컨")
        m = _int(raw.get("mold"), 0, 0, 10)
        if m:
            lines.append((f"곰팡이 제거 ({m}곳)", m * int(cfg["opt_mold"])))
            parts.append(f"곰팡이 {m}곳")
        if _bool(raw.get("veranda")):
            lines.append(("베란다 청소", int(cfg["opt_veranda"])))
            parts.append("베란다")

    elif biz == "이사":
        km = _int(raw.get("km"), 20, 1, 300)
        p = _int(raw.get("pyeong"), 24, 5, 100)
        fl = _int(raw.get("floor"), 3, 1, 50)
        lines.append(("기본 출장비", int(cfg["base_fee"])))
        lines.append((f"거리 ({km}km)", km * int(cfg["per_km"])))
        lines.append((f"짐 양 ({p}평)", p * int(cfg["per_pyeong"])))
        parts += [f"{km}km", f"{p}평", f"{fl}층"]
        if fl > 1:
            lines.append((f"층수 추가 ({fl}층)", (fl - 1) * int(cfg["per_floor"])))
        if _bool(raw.get("ladder")):
            lines.append(("사다리차", int(cfg["opt_ladder"])))
            parts.append("사다리차")
        if _bool(raw.get("packing")):
            lines.append(("포장", int(cfg["opt_packing"])))
            parts.append("포장")
        if _bool(raw.get("ac_move")):
            lines.append(("에어컨 이전·설치", int(cfg["opt_ac_move"])))
            parts.append("에어컨 이전")

    elif biz == "인테리어":
        job = str(raw.get("job", "wallpaper"))
        if job not in JOB_LABEL:
            job = "wallpaper"
        p = _int(raw.get("pyeong"), 30, 5, 100)
        rate = int(cfg[f"job_{job}"])
        lines.append((f"{JOB_LABEL[job]} ({p}평 × {won(rate)}/평)", p * rate))
        parts += [JOB_LABEL[job], f"{p}평"]
        if _bool(raw.get("silk")):
            lines.append((f"실크벽지 업그레이드 ({p}평)", p * int(cfg["opt_silk"])))
            parts.append("실크벽지")
        if _bool(raw.get("molding")):
            lines.append(("몰딩 시공", int(cfg["opt_molding"])))
            parts.append("몰딩")
        if _bool(raw.get("led")):
            lines.append(("LED 조명 교체", int(cfg["opt_led"])))
            parts.append("LED조명")
    else:
        raise ValueError("unknown biz")

    subtotal = sum(a for _, a in lines)
    vat_mode = cfg.get("vat_mode", "별도")
    if vat_mode == "포함":
        total = subtotal
        vat = total - round(total / 1.1)
        supply = total - vat
    else:
        vat = round(subtotal * 0.1)
        total = subtotal + vat
        supply = subtotal

    valid_until = date.today() + timedelta(days=QUOTE_VALID_DAYS)
    return {
        "biz": biz,
        "lines": [{"label": lb, "amount": am, "amount_str": won(am)} for lb, am in lines],
        "subtotal": subtotal,
        "subtotal_str": won(subtotal),
        "supply": supply,
        "supply_str": won(supply),
        "vat": vat,
        "vat_str": won(vat),
        "vat_mode": vat_mode,
        "total": total,
        "total_str": won(total),
        "valid_until": valid_until.isoformat(),
        "valid_until_str": valid_until.strftime("%Y년 %-m월 %-d일"),
        "summary": " · ".join(parts) if parts else "기본 견적",
    }


# ---------------------------------------------------------------------------
# 화면
# ---------------------------------------------------------------------------
@app.route("/")
def opening():
    """짜증 모먼트 오프닝 — 사장님의 실제 하루를 숫자로 재현"""
    return render_template("opening.html")


@app.route("/start")
def start():
    return render_template("start.html", biz_list=BIZ_LIST, emoji=BIZ_EMOJI, desc=BIZ_DESC)


@app.route("/q/<biz>")
def quote(biz):
    if biz not in BIZ_LIST:
        return render_template("error.html", message="없는 가게 종류예요. 처음부터 다시 골라주세요.",
                               back_url=url_for("start")), 404
    cfg = load_config(biz)
    return render_template("quote.html", biz=biz, emoji=BIZ_EMOJI[biz],
                           desc=BIZ_DESC[biz], cfg=cfg)


@app.route("/book")
def book_form():
    biz = request.args.get("biz", "")
    if biz not in BIZ_LIST:
        return render_template("error.html", message="견적 내용이 없어요. 견적 화면에서 다시 와주세요.",
                               back_url=url_for("start")), 400
    cfg = load_config(biz)
    try:
        q = calc_quote(biz, cfg, request.args.to_dict())
    except ValueError:
        return render_template("error.html", message="견적 내용이 이상해요. 다시 골라주세요.",
                               back_url=url_for("quote", biz=biz)), 400
    return render_template("book.html", biz=biz, emoji=BIZ_EMOJI[biz], q=q,
                           args=request.args.to_dict(), error=None, form={})


@app.route("/book", methods=["POST"])
def book_submit():
    biz = request.form.get("biz", "")
    if biz not in BIZ_LIST:
        return render_template("error.html", message="견적 내용이 없어요. 처음부터 다시 해주세요.",
                               back_url=url_for("start")), 400
    cfg = load_config(biz)
    q = calc_quote(biz, cfg, request.form.to_dict())

    name = request.form.get("name", "").strip()
    phone_raw = request.form.get("phone", "")
    phone = re.sub(r"\D", "", phone_raw)
    hope = request.form.get("hope_date", "").strip()
    form = {"name": name, "phone": phone_raw, "hope_date": hope}

    error = None
    if len(name) < 2:
        error = "성함을 두 글자 이상 적어주세요."
    elif not (re.fullmatch(r"0\d{9,10}", phone)):
        error = "연락처를 다시 확인해주세요. 숫자만 10~11자리면 돼요. (예: 01012345678)"
    else:
        try:
            hope_d = datetime.strptime(hope, "%Y-%m-%d").date()
            if hope_d < date.today():
                error = "희망일은 오늘 이후로 골라주세요."
        except ValueError:
            error = "희망일을 달력에서 골라주세요."

    if error:
        return render_template("book.html", biz=biz, emoji=BIZ_EMOJI[biz], q=q,
                               args=request.form.to_dict(), error=error, form=form)

    db = get_db()
    cur = db.execute(
        "INSERT INTO bookings (biz, name, phone, hope_date, summary, total, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (biz, name, phone, hope, q["summary"], q["total"],
         datetime.now().strftime("%Y-%m-%d %H:%M")))
    db.commit()
    return redirect(url_for("book_done", booking_id=cur.lastrowid))


@app.route("/book/done/<int:booking_id>")
def book_done(booking_id):
    db = get_db()
    row = db.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if not row:
        return render_template("error.html", message="예약 내역을 찾을 수 없어요.",
                               back_url=url_for("start")), 404
    return render_template("book_done.html", b=row, emoji=BIZ_EMOJI.get(row["biz"], "📋"))


@app.route("/owner")
def owner():
    db = get_db()
    configs = {biz: load_config(biz) for biz in BIZ_LIST}
    logs = db.execute("SELECT * FROM quote_logs ORDER BY id DESC LIMIT 50").fetchall()
    bookings = db.execute("SELECT * FROM bookings ORDER BY id DESC LIMIT 50").fetchall()
    return render_template("owner.html", biz_list=BIZ_LIST, emoji=BIZ_EMOJI,
                           field_defs=FIELD_DEFS, configs=configs,
                           vat_modes=VAT_MODES, logs=logs, bookings=bookings)


@app.route("/owner/save", methods=["POST"])
def owner_save():
    db = get_db()
    for biz in BIZ_LIST:
        cfg = load_config(biz)
        for f in FIELD_DEFS[biz]:
            raw = request.form.get(f"{biz}__{f['key']}", "")
            try:
                v = int(str(raw).replace(",", "").strip())
            except ValueError:
                v = f["default"]
            cfg[f["key"]] = max(0, v)
        vm = request.form.get(f"{biz}__vat_mode", "별도")
        cfg["vat_mode"] = vm if vm in VAT_MODES else "별도"
        db.execute("INSERT OR REPLACE INTO biz_config (biz, config) VALUES (?, ?)",
                   (biz, json.dumps(cfg, ensure_ascii=False)))
    db.commit()
    flash("바꿨어요. 손님 화면에 바로 반영됐어요. 걱정 마세요.")
    return redirect(url_for("owner"))


# ---------------------------------------------------------------------------
# API (손님 화면 실시간 계산용)
# ---------------------------------------------------------------------------
@app.route("/api/calc", methods=["POST"])
def api_calc():
    data = request.get_json(force=True, silent=True) or {}
    biz = data.get("biz", "")
    if biz not in BIZ_LIST:
        return jsonify({"ok": False, "message": "없는 가게 종류예요."}), 400
    cfg = load_config(biz)
    q = calc_quote(biz, cfg, data.get("inputs", {}))
    q["ok"] = True
    return jsonify(q)


@app.route("/api/log", methods=["POST"])
def api_log():
    """손님이 견적을 확인하면 사장님 화면 이력에 남김"""
    data = request.get_json(force=True, silent=True) or {}
    biz = data.get("biz", "")
    if biz not in BIZ_LIST:
        return jsonify({"ok": False}), 400
    cfg = load_config(biz)
    q = calc_quote(biz, cfg, data.get("inputs", {}))
    db = get_db()
    db.execute("INSERT INTO quote_logs (biz, summary, total, created_at) VALUES (?, ?, ?, ?)",
               (biz, q["summary"], q["total"], datetime.now().strftime("%Y-%m-%d %H:%M")))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 바이럴 · 감동 · 입소문 · 레거시
# ---------------------------------------------------------------------------
SYSTEM_NAME = "편한 견적서"
BADGE_TEXT = "이 가게는 편한 견적 시스템을 사용합니다 🧾"


def fomo_count():
    doy = datetime.now().timetuple().tm_yday
    return 6 + (doy % 4)  # 6~9


def greeting_for(now):
    last_day = (now.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    if (last_day - now).days <= 2:
        return "🌟 이번 달도 정말 수고 많으셨어요. 사장님 최고예요!"
    if 5 <= now.hour < 12:
        return "☀️ 좋은 아침이에요. 오늘도 힘내세요 😊"
    if now.hour >= 18:
        return "🌙 오늘 하루도 수고하셨어요. 푹 쉬세요."
    return "😊 오늘도 편한 견적서가 곁에서 도와드릴게요."


def _recent_weekend_range(now):
    """가장 최근에 지난 주말(토~일) 날짜 범위. 오늘이 주말이면 이번 주말."""
    wd = now.weekday()  # 월=0
    if wd == 5:
        sat = now.date()
    elif wd == 6:
        sat = (now - timedelta(days=1)).date()
    else:
        sat = (now - timedelta(days=wd + 2)).date()
    return sat, sat + timedelta(days=1)


@app.route("/referral")
def referral_page():
    return render_template("referral.html", fomo=fomo_count())


@app.route("/api/referral", methods=["POST"])
def api_referral_create():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()[:20]
    code = (secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]
            or secrets.token_hex(4))
    db = get_db()
    try:
        db.execute(
            "INSERT INTO referrals (code, name, referred_count, created_at)"
            " VALUES (?, ?, 0, ?)",
            (code, name, datetime.now().strftime("%Y-%m-%d %H:%M")))
        db.commit()
    except Exception:
        return jsonify({"ok": False, "message": "다시 눌러주세요 🙏"}), 500
    link = request.host_url.rstrip("/") + "/ref/" + code
    return jsonify({"ok": True, "code": code, "link": link})


@app.route("/api/referral/<code>")
def api_referral_status(code):
    db = get_db()
    row = db.execute("SELECT * FROM referrals WHERE code=?", (code,)).fetchone()
    if not row:
        return jsonify({"ok": False}), 404
    return jsonify({"ok": True, "name": row["name"],
                    "referred_count": row["referred_count"]})


@app.route("/ref/<code>")
def referral_landing(code):
    db = get_db()
    row = db.execute("SELECT * FROM referrals WHERE code=?", (code,)).fetchone()
    if not row:
        return render_template("error.html", message="소개 링크가 이상해요. 처음부터 다시 와주세요.",
                               back_url=url_for("start")), 404
    seen = session.get("ref_seen") or []
    if code not in seen:
        db.execute("UPDATE referrals SET referred_count = referred_count + 1"
                   " WHERE code=?", (code,))
        db.commit()
        seen.append(code)
        session["ref_seen"] = seen
    referrer = row["name"] or "옆 가게 사장님"
    return render_template("ref.html", referrer=referrer, fomo=fomo_count())


@app.route("/about")
def about_page():
    return render_template("about.html")


@app.route("/report")
def report_page():
    return render_template("report.html")


@app.route("/api/owner/touch")
def api_touch():
    """쓰나미 모먼트: 주말 감동 브리핑 (감정톤 · 루틴 인사 · 존엄 카운트)."""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    month = now.strftime("%Y-%m")
    db = get_db()

    sat, sun = _recent_weekend_range(now)
    wk_quotes = db.execute(
        "SELECT COUNT(*) FROM quote_logs WHERE substr(created_at,1,10) BETWEEN ? AND ?",
        (sat.isoformat(), sun.isoformat())).fetchone()[0]
    wk_books = db.execute(
        "SELECT COUNT(*) FROM bookings WHERE substr(created_at,1,10) BETWEEN ? AND ?",
        (sat.isoformat(), sun.isoformat())).fetchone()[0]

    today_books = db.execute(
        "SELECT biz, COUNT(*) AS c FROM bookings WHERE created_at LIKE ?"
        " GROUP BY biz", (today + "%",)).fetchall()
    n_today = sum(r["c"] for r in today_books)
    clean_n = sum(r["c"] for r in today_books if r["biz"] == "청소")

    month_quotes = db.execute(
        "SELECT COUNT(*) FROM quote_logs WHERE created_at LIKE ?",
        (month + "%",)).fetchone()[0]
    month_books = db.execute(
        "SELECT COUNT(*) FROM bookings WHERE created_at LIKE ?",
        (month + "%",)).fetchone()[0]
    month_rev = db.execute(
        "SELECT COALESCE(SUM(total),0) FROM bookings WHERE created_at LIKE ?",
        (month + "%",)).fetchone()[0]

    if wk_quotes > 0 or wk_books > 0:
        tsunami = (f"주말 동안 {wk_quotes}명이 견적을 확인하고 "
                   f"{wk_books}명이 예약했어요. 사장님은 쉬셨어요. 😊")
    else:
        tsunami = (f"{SYSTEM_NAME}가 대기 중이에요. 손님이 오시면 바로 견적을 내드릴게요. "
                   "편하게 계세요 😊")

    if clean_n > 0:
        dignity = f"오늘 {clean_n}가정이 사장님 덕분에 깨끗해졌어요."
    elif n_today > 0:
        dignity = f"오늘 {n_today}팀의 손님이 사장님 덕분에 편하게 일정을 잡았어요."
    else:
        dignity = "오늘도 사장님의 정직한 일손을 기다리는 손님이 있어요."

    return jsonify({
        "greeting": greeting_for(now),
        "tsunami": tsunami,
        "dignity": dignity,
        "weekend_quotes": wk_quotes,
        "weekend_bookings": wk_books,
        "month_quotes": month_quotes,
        "month_bookings": month_books,
        "month_revenue": month_rev,
        "badge_text": BADGE_TEXT,
        "fomo": fomo_count(),
    })


# ---------------------------------------------------------------------------
# 에러 화면 — 개발자 용어·traceback 노출 금지
# ---------------------------------------------------------------------------
@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", message="찾으시는 페이지가 없어요.",
                           back_url=url_for("start")), 404


@app.errorhandler(500)
def server_error(_e):
    app.logger.error(traceback.format_exc())
    return render_template("error.html",
                           message="잠시 문제가 생겼어요. 처음부터 다시 해보시면 돼요.",
                           back_url=url_for("start")), 500


if __name__ == "__main__":
    init_db()
    # 포트 5010 권장, 충돌 시 5011, 5012... 순으로 자동 전환
    import socket

    port = 5010
    while True:
        s = socket.socket()
        try:
            s.bind(("0.0.0.0", port))
            s.close()
            break
        except OSError:
            port += 1
    print(f"편한 견적서 실행 중 → http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
