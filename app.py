"""The Professor Who Won't Tell You
An AI case-study coach for MBA students (Use case #16).

Run locally:  streamlit run app.py
"""

import html
import io
import os
import time
from datetime import datetime

import streamlit as st

from cases import SAMPLE_CASES
from gemini_client import BadOutput, GeminiClient, GeminiError
from prompts import (
    FRAMEWORKS,
    PERSONAS,
    PROFESSOR_NAME,
    RUBRIC,
    REPORT_SYSTEM,
    professor_system_prompt,
    report_prompt,
    turn_prompt,
)

st.set_page_config(page_title="The Professor Who Won't Tell You", page_icon="🎓", layout="wide")

# ---------------------------------------------------------------- constants
MIN_CASE_CHARS = 300
MAX_CASE_CHARS = 20000
MAX_ANSWER_CHARS = 2000
MIN_ANSWERS_FOR_REPORT = 3
MAX_HINTS = 3
COLD_CALL_SECONDS = 60
MAX_POINTS_PER_BOX = 6
DEPTH_BADGE = {"surface": "🟡 Surface", "analytical": "🟠 Analytical", "insightful": "🟢 Insightful"}

# ---------------------------------------------------------------- styling
st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Literata:opsz,wght@7..72,400;7..72,600;7..72,800&family=Kalam:wght@400;700&display=swap');
:root { --ink:#23272A; --crimson:#8E1F2F; --slate:#2E4036; --chalk:#EDEBE3; --chalk-new:#F1D27A; --paper:#F4F5F1; }
html, body, [class*="css"], .stMarkdown, .stChatMessage { font-family: 'Literata', Georgia, serif; }
h1, h2, h3 { font-family: 'Literata', Georgia, serif !important; letter-spacing: -0.01em; }
.hero-title { font-size: clamp(2rem, 5vw, 3.4rem); font-weight: 800; line-height: 1.05; color: var(--ink); margin: 0.2rem 0 0.6rem; }
.hero-sub { font-size: 1.1rem; max-width: 62ch; line-height: 1.6; color: #4a4f52; }
.chalk-line { font-family: 'Kalam', cursive; color: var(--crimson); font-size: 1.25rem; margin-top: .4rem; }
.board { background: var(--slate); border: 10px solid #6B4F35; border-radius: 6px; padding: 14px; color: var(--chalk);
         box-shadow: inset 0 0 40px rgba(0,0,0,.35); }
.board-title { font-family: 'Kalam', cursive; font-size: 1.35rem; margin: 0 0 8px 4px; opacity: .9; }
.board-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.box { border: 1.5px dashed rgba(237,235,227,.45); border-radius: 4px; padding: 8px 10px; min-height: 92px; }
.box.wide { grid-column: span 2; }
.box .bt { font-family: 'Kalam', cursive; font-weight: 700; font-size: 1.05rem; margin: 0 0 4px; color: var(--chalk); }
.box p { font-family: 'Kalam', cursive; font-size: 1rem; line-height: 1.3; margin: 2px 0; text-shadow: 0 0 1px rgba(237,235,227,.6); }
.box p.new { color: var(--chalk-new); }
.box p.empty { opacity: .45; font-style: italic; }
.depth-row { display:flex; gap: 14px; flex-wrap: wrap; font-size: .95rem; margin: 10px 0 2px; }
.grade { font-size: 4.5rem; font-weight: 800; color: var(--crimson); line-height: 1; }
.verdict { font-size: 1.25rem; line-height: 1.5; max-width: 60ch; }
.crit { border-left: 3px solid var(--crimson); padding: 4px 0 4px 12px; margin: 10px 0; }
.crit .scores { font-weight: 600; }
@media (max-width: 640px) { .board-grid { grid-template-columns: 1fr; } .box.wide { grid-column: span 1; } }
</style>
""",
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------- state
DEFAULTS = dict(
    phase="setup",  # setup -> class -> assess -> report
    case_title="",
    case_text="",
    framework="SWOT",
    persona="Harvard Cold-Caller",
    cold_call=False,
    messages=[],
    board={},
    turn=0,
    hints_used=0,
    begs=0,
    off_topic=0,
    injections=0,
    slow_answers=0,
    q_time=None,
    pending=None,
    report=None,
    self_scores={},
    notice=None,
)
for k, v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v.copy() if isinstance(v, (dict, list)) else v
S = st.session_state


def reset():
    for k, v in DEFAULTS.items():
        S[k] = v.copy() if isinstance(v, (dict, list)) else v


# ---------------------------------------------------------------- API key + client
def get_api_key():
    key = None
    try:
        key = st.secrets.get("GEMINI_API_KEY")
    except Exception:
        pass
    return key or os.environ.get("GEMINI_API_KEY") or S.get("user_api_key")


def get_model_pref():
    try:
        return st.secrets.get("GEMINI_MODEL")
    except Exception:
        return os.environ.get("GEMINI_MODEL")


def get_client():
    key = get_api_key()
    if not key:
        return None
    if S.get("_client_key") != key:
        S["_client"] = GeminiClient(key, get_model_pref())
        S["_client_key"] = key
    return S["_client"]


# ---------------------------------------------------------------- helpers
def student_answers():
    return [m for m in S.messages if m["role"] == "student" and m.get("intent") == "answer"]


def merge_board(updates):
    if not isinstance(updates, dict):
        return
    for key, items in updates.items():
        if key not in S.board or not isinstance(items, list):
            continue
        existing = {i["text"].lower() for i in S.board[key]}
        for item in items:
            text = str(item).strip().strip("-•").strip()[:120]
            if text and text.lower() not in existing and len(S.board[key]) < MAX_POINTS_PER_BOX:
                S.board[key].append({"text": text, "turn": S.turn})
                existing.add(text.lower())


def ask_professor(student_message, mode="answer", seconds_taken=None):
    """One model call. Returns the parsed dict, or a safe fallback."""
    client = get_client()
    system = professor_system_prompt(S.persona, S.framework)
    prompt = turn_prompt(S.case_text, S.messages, S.board, student_message, mode, seconds_taken)
    try:
        return client.generate_json(system, prompt, temperature=0.7)
    except BadOutput:
        return {
            "reply": "I lost my train of thought there. Could you restate your last point in one sentence?",
            "intent": "question",
            "depth": "none",
            "depth_note": "",
            "board_updates": {},
        }


def professor_turn(student_message, mode="answer", seconds_taken=None):
    S.turn += 1
    with st.spinner(f"{PROFESSOR_NAME} is thinking..."):
        try:
            data = ask_professor(student_message, mode, seconds_taken)
        except GeminiError as e:
            S.notice = ("error", e.user_message)
            if mode == "answer" and S.messages and S.messages[-1]["role"] == "student":
                S.messages.pop()  # let the student resend instead of leaving an unanswered turn
            S.turn -= 1
            return False

    intent = data.get("intent", "answer")
    if mode == "answer" and S.messages and S.messages[-1]["role"] == "student":
        last = S.messages[-1]
        last["intent"] = intent
        if intent == "answer" and data.get("depth") in DEPTH_BADGE:
            last["depth"] = data["depth"]
            last["depth_note"] = data.get("depth_note", "")
        S.begs += intent == "answer_request"
        S.off_topic += intent == "off_topic"
        S.injections += intent == "injection"
        if intent == "answer":
            merge_board(data.get("board_updates", {}))

    reply = str(data.get("reply", "")).strip() or "Interesting. Say more: what in the case supports that?"
    S.messages.append({"role": "professor", "text": reply, "intent": intent})
    S.q_time = time.time()
    return True


def start_class():
    S.board = {k: [] for k in FRAMEWORKS[S.framework]["boxes"]}
    S.messages, S.turn = [], 0
    S.phase = "class"
    if not professor_turn("", mode="opening"):
        S.phase = "setup"


def transcript_markdown():
    lines = [f"# Case discussion: {S.case_title}", f"_{datetime.now():%d %b %Y, %H:%M}_", ""]
    for m in S.messages:
        who = PROFESSOR_NAME if m["role"] == "professor" else "Student"
        badge = f"  ({DEPTH_BADGE[m['depth']]})" if m.get("depth") in DEPTH_BADGE else ""
        lines.append(f"**{who}:** {m['text']}{badge}\n")
    lines.append("\n## Board")
    for key, label in FRAMEWORKS[S.framework]["boxes"].items():
        lines.append(f"\n**{label}**")
        lines += [f"- {i['text']}" for i in S.board.get(key, [])] or ["- (empty)"]
    return "\n".join(lines)


def report_markdown(r):
    lines = [transcript_markdown(), "\n\n# Report card", f"**Grade {r.get('grade','?')} | {r.get('overall_score','?')}/100**",
             r.get("headline", ""), "\n## Rubric (you vs professor)"]
    for i, c in enumerate(r.get("rubric", [])):
        mine = S.self_scores.get(RUBRIC[i], "?") if i < len(RUBRIC) else "?"
        lines.append(f"- **{c.get('criterion')}**: you {mine}/5, professor {c.get('score')}/5. {c.get('comment','')}")
    for title, key in [("Strengths", "strengths"), ("Blind spots", "blind_spots"), ("Next time", "next_time")]:
        lines.append(f"\n## {title}")
        lines += [f"- {x}" for x in r.get(key, [])]
    lines.append(f"\n## Calibration\n{r.get('calibration','')}")
    return "\n".join(lines)


def read_upload(file):
    name = file.name.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(file.getvalue()))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    return file.getvalue().decode("utf-8", errors="ignore")


def countdown(seconds_left):
    seconds_left = max(0, int(seconds_left))
    timer_html = (
        f"""
<div id="cc" style="font-family:Georgia,serif;display:flex;align-items:center;gap:12px;padding:6px 2px;">
  <div id="n" style="font-size:2.1rem;font-weight:800;color:#23272A;min-width:3.2ch;">{seconds_left}</div>
  <div id="m" style="font-size:.95rem;color:#4a4f52;">seconds to answer. You've been cold-called.</div>
</div>
<script>
let r = {seconds_left};
const n = document.getElementById('n'), m = document.getElementById('m');
function paint() {{
  n.textContent = r;
  if (r <= 10) n.style.color = '#8E1F2F';
  if (r <= 0) {{ m.textContent = "Time's up. Answer anyway; the professor will notice."; clearInterval(t); }}
}}
paint();
const t = setInterval(() => {{ r -= 1; paint(); }}, 1000);
</script>"""
    )
    if hasattr(st, "iframe"):  # Streamlit >= 1.5x
        st.iframe(timer_html, height=62)
    else:  # older Streamlit
        import streamlit.components.v1 as components

        components.html(timer_html, height=62)


def render_board(highlight_turn):
    fw = FRAMEWORKS[S.framework]
    boxes = list(fw["boxes"].items())
    cells = []
    for idx, (key, label) in enumerate(boxes):
        items = S.board.get(key, [])
        wide = " wide" if len(boxes) % 2 == 1 and idx == len(boxes) - 1 else ""
        body = "".join(
            f'<p class="{"new" if i["turn"] == highlight_turn else ""}">– {html.escape(i["text"])}</p>' for i in items
        ) or '<p class="empty">nothing yet</p>'
        cells.append(f'<div class="box{wide}"><div class="bt">{html.escape(label)}</div>{body}</div>')
    st.markdown(
        f'<div class="board"><div class="board-title">{html.escape(S.framework)}, written from your answers</div>'
        f'<div class="board-grid">{"".join(cells)}</div></div>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.markdown("### 🎓 The Professor Who Won't Tell You")
    st.caption(
        f"{PROFESSOR_NAME} is an AI coach built on Google Gemini. It asks questions; it never gives you the answer. "
        "It can misread a case or misjudge an answer, so treat its feedback as a sparring partner, not a grade."
    )
    if not get_api_key():
        st.text_input("Gemini API key", type="password", key="user_api_key",
                      help="Free key from aistudio.google.com. Used only for this browser session.")
    if S.phase != "setup":
        st.download_button("Download transcript", transcript_markdown(), file_name="case_discussion.md")
        if st.button("Start over"):
            reset()
            st.rerun()
    st.divider()
    st.caption("🔒 Privacy: your case and answers are sent to Google's Gemini API to generate replies. "
               "Nothing is stored by this app after you close the tab. Don't paste confidential material.")
    client = S.get("_client")
    if client and client.active_model:
        st.caption(f"Model: {client.active_model}")

# ---------------------------------------------------------------- notices
if S.notice:
    kind, text = S.notice
    (st.error if kind == "error" else st.warning)(text)
    S.notice = None

# ================================================================= SETUP
if S.phase == "setup":
    st.markdown('<div class="hero-title">The Professor Who Won\'t Tell You</div>', unsafe_allow_html=True)
    st.markdown(
        f'<div class="hero-sub">Bring a business case. {PROFESSOR_NAME} will question you until you crack it '
        "yourself, writing your analysis on the board as you go. Ask for the answer as nicely as you like. "
        "You won't get it.</div>"
        '<div class="chalk-line">Think first. Then think harder.</div>',
        unsafe_allow_html=True,
    )
    st.write("")

    left, right = st.columns([3, 2], gap="large")
    with left:
        st.subheader("1. Choose a case")
        source = st.radio("Case source", ["Sample case", "Paste my own", "Upload a file"], horizontal=True,
                          label_visibility="collapsed")
        case_title, case_text = "", ""
        if source == "Sample case":
            case_title = st.selectbox("Sample case", list(SAMPLE_CASES))
            case_text = SAMPLE_CASES[case_title]
            with st.expander("Read the case", expanded=False):
                st.markdown(case_text)
        elif source == "Paste my own":
            case_title = st.text_input("Case title", placeholder="e.g. Zomato's 10-minute delivery bet")
            case_text = st.text_area("Case text", height=260, placeholder="Paste the full case here...")
        else:
            up = st.file_uploader("Upload a .txt or .pdf case", type=["txt", "pdf"])
            if up:
                try:
                    case_text = read_upload(up)
                    case_title = up.name.rsplit(".", 1)[0].replace("_", " ")
                    st.caption(f"Read {len(case_text):,} characters from {up.name}.")
                except Exception:
                    st.error("Couldn't read that file. If it's a scanned PDF, copy the text and paste it instead.")

    with right:
        st.subheader("2. Set up the class")
        framework = st.radio("Framework for the board", list(FRAMEWORKS))
        persona = st.radio("Professor style", list(PERSONAS),
                           captions=["Demanding, challenges every claim", "Patient and encouraging"])
        cold_call = st.toggle("Cold-call mode", help=f"{COLD_CALL_SECONDS} seconds per answer, like a real case classroom.")
        consent = st.checkbox("Send my case and answers to Google Gemini")

    case_text = (case_text or "").strip()
    problems = []
    if not get_api_key():
        problems.append("Add a Gemini API key in the sidebar.")
    if len(case_text) < MIN_CASE_CHARS:
        problems.append(f"The case needs at least {MIN_CASE_CHARS} characters so the professor has something to work with.")
    if len(case_text) > MAX_CASE_CHARS:
        problems.append(f"The case is {len(case_text):,} characters. Trim it to under {MAX_CASE_CHARS:,}.")
    if not consent:
        problems.append("Tick the privacy checkbox to continue.")

    st.write("")
    if st.button("Start the class", type="primary", disabled=bool(problems)):
        S.case_title = case_title.strip() or "Untitled case"
        S.case_text, S.framework, S.persona, S.cold_call = case_text, framework, persona, cold_call
        start_class()
        st.rerun()
    for p in problems:
        st.caption(f"• {p}")

# ================================================================= CLASS
elif S.phase == "class":
    # Actions from buttons are queued via callbacks so they run before rendering.
    def queue(action):
        S.pending = action

    user_input = st.chat_input("Your answer...")

    if S.pending == "hint":
        S.pending = None
        if S.hints_used < MAX_HINTS:
            S.hints_used += 1
            S.messages.append({"role": "student", "text": "💡 (asked for a hint)", "intent": "hint"})
            professor_turn("", mode="hint")
    elif S.pending == "end":
        S.pending = None
        S.phase = "assess"
        st.rerun()

    if user_input is not None:
        text = user_input.strip()
        if not text:
            S.notice = ("warning", "That answer was empty. Type something, even a rough first thought.")
        else:
            if len(text) > MAX_ANSWER_CHARS:
                text = text[:MAX_ANSWER_CHARS]
                S.notice = ("warning", f"Long answer trimmed to {MAX_ANSWER_CHARS} characters. Shorter, sharper answers score better anyway.")
            seconds = time.time() - S.q_time if (S.cold_call and S.q_time) else None
            if seconds and seconds > COLD_CALL_SECONDS:
                S.slow_answers += 1
            S.messages.append({"role": "student", "text": text, "intent": "answer",
                               "seconds": round(seconds) if seconds else None})
            professor_turn(text, mode="answer", seconds_taken=seconds)
        st.rerun()

    st.markdown(f"## {html.escape(S.case_title)}")
    st.caption(f"{S.framework} board  |  {S.persona}" + ("  |  ⏱️ Cold-call mode" if S.cold_call else ""))

    chat_col, board_col = st.columns([3, 2], gap="large")
    with chat_col:
        for m in S.messages:
            if m["role"] == "professor":
                with st.chat_message("assistant", avatar="🎓"):
                    st.markdown(m["text"])
            else:
                with st.chat_message("user", avatar="🧑‍🎓"):
                    st.markdown(m["text"])
                    meta = []
                    if m.get("depth") in DEPTH_BADGE:
                        meta.append(f"**{DEPTH_BADGE[m['depth']]}**: {m.get('depth_note', '')}")
                    if m.get("seconds") is not None:
                        meta.append(f"⏱️ {m['seconds']}s")
                    if m.get("intent") == "answer_request":
                        meta.append("🙅 Asked for the answer")
                    elif m.get("intent") in ("off_topic", "injection"):
                        meta.append("↩️ Off-topic, redirected")
                    if meta:
                        st.caption("  |  ".join(meta))

    with board_col:
        if S.cold_call and S.q_time and S.messages and S.messages[-1]["role"] == "professor":
            countdown(COLD_CALL_SECONDS - (time.time() - S.q_time))
        render_board(S.turn)

        answers = student_answers()
        counts = {d: sum(1 for a in answers if a.get("depth") == d) for d in DEPTH_BADGE}
        st.markdown(
            '<div class="depth-row">' + "".join(f"<span>{DEPTH_BADGE[d]} {counts[d]}</span>" for d in DEPTH_BADGE)
            + "</div>",
            unsafe_allow_html=True,
        )
        st.caption(f"{len(answers)} answers  |  {S.hints_used}/{MAX_HINTS} hints used  |  asked for the answer {S.begs}×")

        b1, b2 = st.columns(2)
        b1.button(f"💡 Hint ({MAX_HINTS - S.hints_used} left)", on_click=queue, args=("hint",),
                  disabled=S.hints_used >= MAX_HINTS, width="stretch")
        enough = len(answers) >= MIN_ANSWERS_FOR_REPORT
        b2.button("End class", on_click=queue, args=("end",), disabled=not enough,
                  type="primary", width="stretch")
        if not enough:
            st.caption(f"Give at least {MIN_ANSWERS_FOR_REPORT} real answers to unlock your report card.")

# ================================================================= SELF-ASSESSMENT
elif S.phase == "assess":
    st.markdown("## Before the verdict: grade yourself")
    st.write("Rate your own performance honestly. The professor will grade you too, and you'll see where you agree.")
    scores = {}
    for crit in RUBRIC:
        scores[crit] = st.slider(crit, 1, 5, 3)
    c1, c2 = st.columns([1, 3])
    if c2.button("Back to class"):
        S.phase = "class"
        st.rerun()
    if c1.button("Show the professor's verdict", type="primary"):
        S.self_scores = scores
        stats = {
            "answers": len(student_answers()),
            "hints_used": S.hints_used,
            "asked_for_answer": S.begs,
            "off_topic_attempts": S.off_topic + S.injections,
            "cold_call_overtime_answers": S.slow_answers if S.cold_call else "n/a",
        }
        with st.spinner("Marking your work..."):
            try:
                r = get_client().generate_json(
                    REPORT_SYSTEM, report_prompt(S.case_text, S.messages, S.board, scores, stats), temperature=0.3
                )
                if not isinstance(r.get("rubric"), list):
                    raise BadOutput("rubric missing")
                S.report = r
                S.phase = "report"
            except BadOutput:
                S.notice = ("error", "The report came back incomplete. Press the button again to regenerate it.")
            except GeminiError as e:
                S.notice = ("error", e.user_message)
        st.rerun()

# ================================================================= REPORT
elif S.phase == "report":
    r = S.report
    top_l, top_r = st.columns([1, 4])
    top_l.markdown(f'<div class="grade">{html.escape(str(r.get("grade", "?")))}</div>', unsafe_allow_html=True)
    top_l.caption(f"{r.get('overall_score', '?')}/100")
    top_r.markdown(f"## Report card: {html.escape(S.case_title)}")
    top_r.markdown(f'<div class="verdict">{html.escape(str(r.get("headline", "")))}</div>', unsafe_allow_html=True)

    st.write("")
    left, right = st.columns([3, 2], gap="large")
    with left:
        st.markdown("### You vs the professor")
        for i, c in enumerate(r.get("rubric", [])):
            crit = RUBRIC[i] if i < len(RUBRIC) else c.get("criterion", "")
            mine = S.self_scores.get(crit, "?")
            prof = c.get("score", "?")
            gap = ""
            if isinstance(mine, int) and isinstance(prof, int) and abs(mine - prof) >= 2:
                gap = "  ⚠️ big gap" if mine > prof else "  🙂 you undersold yourself"
            st.markdown(
                f'<div class="crit"><div class="scores">{html.escape(crit)}: you {mine}/5, professor {prof}/5{gap}</div>'
                f'<div>{html.escape(str(c.get("comment", "")))}</div></div>',
                unsafe_allow_html=True,
            )
        st.info(r.get("calibration", ""))
    with right:
        for title, key in [("What you did well", "strengths"), ("What you missed", "blind_spots"),
                           ("Practise next time", "next_time")]:
            st.markdown(f"### {title}")
            for x in r.get(key, []):
                st.markdown(f"- {x}")

    st.divider()
    st.markdown("### Your board")
    render_board(-1)
    st.write("")
    d1, d2 = st.columns(2)
    d1.download_button("Download report and transcript", report_markdown(r), file_name="case_report.md",
                       type="primary", width="stretch")
    if d2.button("Start a new class", width="stretch"):
        reset()
        st.rerun()

st.caption("AI-generated coaching for learning practice. Not a substitute for your professor's judgement.")
