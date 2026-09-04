"""
Troo — Article to Explainer Video
=================================

Pipeline:
    1. Pull an article (URL or pasted text)
    2. Claude writes a scene-by-scene narration script
    3. A human reviews and edits every scene  <-- the point of the app
    4. JSON2Video renders a branded video
    5. Download locally + emit transcript and VideoObject JSON-LD

Run locally:
    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    # put keys in .streamlit/secrets.toml, or export them:
    #   ANTHROPIC_API_KEY, JSON2VIDEO_API_KEY
    streamlit run troo_article_to_video.py
"""

import json
import os
import re
import time
from datetime import date
from pathlib import Path

import requests
import streamlit as st
from anthropic import Anthropic

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

J2V_BASE = "https://api.json2video.com/v2"
NARRATION_LIMIT = 900             # soft cap per scene, keeps pacing sane
OUTPUT_DIR = Path("./renders")
CLAUDE_MODEL = "claude-sonnet-4-6"
WORDS_PER_MINUTE = 150            # for the runtime and credit estimate

BRAND = {
    "violet": "#281245",
    "green": "#2E9E7E",
    "amber": "#DE8F2C",
    "coral": "#C65B5B",
    "paper": "#FBFAFC",
}

# Azure en-GB neural voices. Confirm these resolve in your JSON2Video dashboard
# before relying on any of them in production.
UK_VOICES = {
    "Ryan (male, neutral)": "en-GB-RyanNeural",
    "Sonia (female, neutral)": "en-GB-SoniaNeural",
    "Thomas (male, warm)": "en-GB-ThomasNeural",
    "Libby (female, warm)": "en-GB-LibbyNeural",
    "Abbi (female, bright)": "en-GB-AbbiNeural",
}

RESOLUTIONS = {
    "Landscape 1080p (full-hd)": "full-hd",
    "Landscape 720p (hd)": "hd",
    "Square (squared)": "squared",
    "Vertical (instagram-story)": "instagram-story",
}


def get_secret(name: str):
    """Environment variable locally, st.secrets on Streamlit Cloud."""
    value = os.getenv(name)
    if value:
        return value
    try:
        return st.secrets[name]
    except Exception:
        return None


st.set_page_config(page_title="Troo — Article to Video", layout="wide")
st.markdown(
    f"""
    <style>
      .stApp {{ background: {BRAND['paper']}; }}
      h1, h2, h3 {{ font-family: Poppins, system-ui, sans-serif;
                    color: {BRAND['violet']}; letter-spacing: -0.01em; }}
      .stButton>button {{ background: {BRAND['green']}; color: white;
                          border: 0; border-radius: 6px; font-weight: 600; }}
      .warn {{ color: {BRAND['coral']}; font-weight: 600; }}
      .ok {{ color: {BRAND['green']}; }}
    </style>
    """,
    unsafe_allow_html=True,
)


# ----------------------------------------------------------------------------
# JSON2Video
# ----------------------------------------------------------------------------


class JSON2Video:
    """Movie JSON builder + client.

    Rules encoded here, from the JSON2Video v2 docs:
      - `scenes` is required on the movie root
      - every element carries an explicit `type`
      - elements are siblings inside scene.elements, never nested
      - movie-level keys (resolution, quality, cache) never appear in a scene
      - poll no faster than every 5s; stop on done | error
    """

    def __init__(self, api_key: str):
        self.headers = {"x-api-key": api_key, "Content-Type": "application/json"}

    # -- movie construction --------------------------------------------------

    @staticmethod
    def build_movie(
        script,
        voice_id,
        resolution="full-hd",
        voice_model="azure",
        connection=None,
        music_url="",
        subtitles=True,
        cta="Talk to Troo about your renewal — troocost.com",
        draft=False,
    ):
        def heading(text, size="4.4vw", colour="#FFFFFF", weight="700"):
            return {
                "type": "text",
                "text": text,
                "position": "center-left",
                "x": 120,
                "width": 1400,
                "fade-in": 0.4,
                "z-index": 2,
                "settings": {
                    "font-family": "Poppins",
                    "font-size": size,
                    "font-weight": weight,
                    "font-color": colour,
                    "text-align": "left",
                    "horizontal-position": "left",
                    "vertical-position": "center",
                    "text-shadow": "none",
                },
            }

        def voice(text):
            el = {
                "type": "voice",
                "text": text,
                "voice": voice_id,
                "model": voice_model,
            }
            if connection:
                el["connection"] = connection
            return el

        scenes = []

        # Title card
        scenes.append({
            "background-color": BRAND["violet"],
            "duration": 3,
            "elements": [heading(script["title"], size="5vw")],
        })

        # Body scenes — one per reviewed scene. Scene length follows the voice
        # element. If timing comes out wrong, set an explicit "duration" here.
        for scene in script["scenes"]:
            elements = []
            img = (scene.get("image_url") or "").strip()
            if img:
                elements.append({"type": "image", "src": img, "z-index": 1})
            elements.append(heading(scene["heading"], size="3.6vw"))
            elements.append(voice(scene["narration"]))
            scenes.append({
                "background-color": BRAND["violet"],
                "elements": elements,
            })

        # Closing card
        scenes.append({
            "background-color": BRAND["green"],
            "duration": 4,
            "elements": [heading(cta, size="3.4vw")],
        })

        movie = {
            "resolution": resolution,
            "quality": "high",
            "draft": draft,
            "scenes": scenes,
            "elements": [],
        }

        if music_url:
            movie["elements"].append({
                "type": "audio", "src": music_url, "volume": 0.18,
            })

        if subtitles:
            movie["elements"].append({
                "type": "subtitles",
                "settings": {
                    "style": "classic",
                    "font-family": "Inter",
                    "font-weight": "700",
                    "font-size": "44",
                    "position": "bottom-center",
                    "max-words-per-line": 6,
                    "all-caps": False,
                    "line-color": "#FFFFFF",
                    "word-color": BRAND["amber"],
                    "outline-color": BRAND["violet"],
                    "outline-width": 6,
                },
            })

        return movie

    # -- api -----------------------------------------------------------------

    def render(self, movie: dict) -> str:
        r = requests.post(f"{J2V_BASE}/movies", headers=self.headers,
                          json=movie, timeout=60)
        if r.status_code >= 400:
            # A 4xx here means the JSON is invalid before rendering starts.
            raise RuntimeError(f"{r.status_code}: {r.text[:600]}")
        body = r.json()
        if not body.get("success"):
            raise RuntimeError(body.get("message", str(body)[:600]))
        return body["project"]

    def status(self, project_id: str) -> dict:
        r = requests.get(f"{J2V_BASE}/movies", headers=self.headers,
                         params={"project": project_id}, timeout=30)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def parse_status(payload: dict):
        """Returns (state, url, message). The response shape varies a little,
        so probe the likely locations rather than assuming one."""
        movie = payload.get("movie") or payload
        if isinstance(movie, list) and movie:
            movie = movie[0]
        return (
            movie.get("status", "unknown"),
            movie.get("url") or movie.get("download"),
            movie.get("message", ""),
        )


# ----------------------------------------------------------------------------
# Article intake
# ----------------------------------------------------------------------------


def fetch_article(url: str) -> str:
    """Crude extractor. Swap for the HubSpot CMS API once this is wired into
    the Knowledge Centre properly."""
    from bs4 import BeautifulSoup

    r = requests.get(url, timeout=30, headers={"User-Agent": "TrooVideoBot/1.0"})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()
    body = soup.find("article") or soup.find("main") or soup.body
    text = body.get_text("\n", strip=True) if body else ""
    return re.sub(r"\n{3,}", "\n\n", text)


# ----------------------------------------------------------------------------
# Scripting with Claude
# ----------------------------------------------------------------------------

SCRIPT_SYSTEM = """You write narration scripts for short B2B explainer videos for \
Troo Cost, a UK business energy consultancy regulated under the Ofgem TPI framework.

Turn the supplied article into a scene-by-scene narration script.

Hard rules:
- Never introduce a claim, figure, saving, or guarantee that is not in the source
  article. If the article hedges, the narration hedges in the same way.
- No superlatives about Troo's own service unless the article states them.
- UK English. Plain business language. No jargon the article does not itself use.
- Write for the ear: short sentences, one idea per sentence, no bullet-point syntax.
- Narration per scene: 200-450 characters. Never exceed 900.
- Each heading is an on-screen caption, not a sentence. Six words maximum.
- Open with the problem the reader has, not with the company.
- Close with a single clear next step.

Return ONLY a JSON object, no markdown fences, no preamble:
{"title": str, "description": str,
 "scenes": [{"heading": str, "narration": str}]}
"""


def build_script(client: Anthropic, article: str, target_scenes: int, audience: str):
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4000,
        system=SCRIPT_SYSTEM,
        messages=[{
            "role": "user",
            "content": (f"Target audience: {audience}\n"
                        f"Target number of scenes: {target_scenes}\n\n"
                        f"ARTICLE:\n{article[:40000]}"),
        }],
    )
    raw = "".join(b.text for b in msg.content if b.type == "text")
    return json.loads(raw.replace("```json", "").replace("```", "").strip())


# ----------------------------------------------------------------------------
# Post-render artefacts
# ----------------------------------------------------------------------------


def video_object_jsonld(title, description, page_url, video_url, transcript, seconds=None):
    obj = {
        "@context": "https://schema.org",
        "@type": "VideoObject",
        "name": title,
        "description": description,
        "uploadDate": date.today().isoformat(),
        "contentUrl": video_url,
        "embedUrl": video_url,
        "transcript": transcript,
        "publisher": {"@type": "Organization", "name": "Troo Cost Ltd",
                      "url": "https://www.troocost.com"},
    }
    if page_url:
        obj["mainEntityOfPage"] = {"@type": "WebPage", "@id": page_url}
    if seconds:
        obj["duration"] = f"PT{int(seconds) // 60}M{int(seconds) % 60}S"
    return json.dumps(obj, indent=2)


def save_local(url: str, slug: str) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"{slug}.mp4"
    with requests.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return path


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "troo-video"


def estimate_seconds(script) -> int:
    words = sum(len(s["narration"].split()) for s in script["scenes"])
    return int(words / WORDS_PER_MINUTE * 60) + 7  # + title and closing cards


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------

st.title("Article to explainer video")
st.caption("Knowledge Centre article in. Reviewed, on-brand narration out.")

anthropic_key = get_secret("ANTHROPIC_API_KEY")
j2v_key = get_secret("JSON2VIDEO_API_KEY")

missing = [n for n, v in [("ANTHROPIC_API_KEY", anthropic_key),
                          ("JSON2VIDEO_API_KEY", j2v_key)] if not v]
if missing:
    st.error("Missing keys: " + ", ".join(missing)
             + ". Set them in .streamlit/secrets.toml or as environment variables.")
    st.stop()

claude = Anthropic(api_key=anthropic_key)
renderer = JSON2Video(j2v_key)

st.sidebar.caption("Renderer: JSON2Video. Free tier is watermarked, "
                   "roughly 600 credits. 1 credit is about 1 second up to 1080p.")

for key, default in [("article", ""), ("script", None),
                     ("job", None), ("page_url", "")]:
    st.session_state.setdefault(key, default)

# --- Step 1: article -------------------------------------------------------

st.header("1. Article")
mode = st.radio("Source", ["Paste text", "Fetch URL"],
                horizontal=True, label_visibility="collapsed")

if mode == "Fetch URL":
    url = st.text_input("Article URL",
                        placeholder="https://www.troocost.com/knowledge/...")
    if st.button("Fetch") and url:
        with st.spinner("Fetching"):
            try:
                st.session_state.article = fetch_article(url)
                st.session_state.page_url = url
            except Exception as e:
                st.error(f"Could not fetch that page: {e}")

st.session_state.article = st.text_area(
    "Article text", value=st.session_state.article, height=240)
st.caption(f"{len(st.session_state.article):,} characters")

# --- Step 2: script --------------------------------------------------------

st.header("2. Draft the script")
c1, c2, c3 = st.columns([1, 2, 1])
scene_count = c1.slider("Scenes", 4, 14, 8)
audience = c2.text_input(
    "Audience", "UK SME finance decision-maker, energy contract renewal")

if c3.button("Draft script", use_container_width=True):
    if len(st.session_state.article) < 300:
        st.warning("Article looks too short to be worth a video.")
    else:
        with st.spinner("Claude is writing the narration"):
            try:
                st.session_state.script = build_script(
                    claude, st.session_state.article, scene_count, audience)
            except json.JSONDecodeError:
                st.error("Model returned malformed JSON. Try again.")
            except Exception as e:
                st.error(f"Script generation failed: {e}")

# --- Step 3: review --------------------------------------------------------

if st.session_state.script:
    script = st.session_state.script

    st.header("3. Review every scene")
    st.info("Nothing is rendered until you approve it. Check that no claim here "
            "outruns what the published article actually says.")

    script["title"] = st.text_input("Video title", script.get("title", ""))
    script["description"] = st.text_area(
        "Description", script.get("description", ""), height=70)

    too_long = False
    for i, scene in enumerate(script["scenes"]):
        with st.expander(f"Scene {i + 1} — {scene.get('heading', '')}",
                         expanded=i < 3):
            scene["heading"] = st.text_input(
                "On-screen heading", scene.get("heading", ""), key=f"h{i}")
            scene["narration"] = st.text_area(
                "Narration", scene.get("narration", ""), height=110, key=f"n{i}")
            scene["image_url"] = st.text_input(
                "Background image URL (optional)",
                scene.get("image_url", ""), key=f"i{i}",
                placeholder="HubSpot file manager URL, chart export, etc.")
            n = len(scene["narration"])
            if n > NARRATION_LIMIT:
                too_long = True
                st.markdown(
                    f"<span class='warn'>{n} / {NARRATION_LIMIT} — too long, "
                    f"split this into two scenes</span>",
                    unsafe_allow_html=True)
            else:
                st.markdown(f"<span class='ok'>{n} / {NARRATION_LIMIT}</span>",
                            unsafe_allow_html=True)

    secs = estimate_seconds(script)
    st.caption(f"Estimated runtime about {secs // 60}m {secs % 60}s, "
               f"roughly {secs} credits")

    # --- Step 4: render ----------------------------------------------------

    st.header("4. Render")

    a1, a2, a3 = st.columns(3)
    res_label = a1.selectbox("Resolution", list(RESOLUTIONS))
    voice_label = a2.selectbox("Voice", list(UK_VOICES))
    connection = a3.text_input(
        "Voice connection ID (optional)",
        help="Set a dashboard connection to bill premium voices to your own "
             "provider key. Never paste a raw third-party key into the JSON.")

    b1, b2, b3 = st.columns(3)
    music_url = b1.text_input("Background music URL (optional)")
    subtitles = b2.checkbox("Burn in subtitles", value=True)
    draft = b3.checkbox("Draft render (faster preview)", value=True)

    movie = JSON2Video.build_movie(
        script,
        voice_id=UK_VOICES[voice_label],
        resolution=RESOLUTIONS[res_label],
        connection=connection.strip() or None,
        music_url=music_url.strip(),
        subtitles=subtitles,
        draft=draft,
    )

    with st.expander("Movie JSON"):
        st.code(json.dumps(movie, indent=2), language="json")
        st.caption("Validate against "
                   "https://json2video.com/docs/v2/movie-schema.json "
                   "if a render fails at submit time.")

    approved = st.checkbox("I have reviewed every scene")
    if st.button("Render", disabled=not approved or too_long):
        try:
            st.session_state.job = renderer.render(movie)
            st.success(f"Project submitted: {st.session_state.job}")
        except Exception as e:
            st.error("Submit failed. The message names the offending path "
                     f"inside the movie JSON:\n\n{e}")

# --- Step 5: collect -------------------------------------------------------

if st.session_state.job:
    st.header("5. Collect")
    st.caption("Always save a local copy. Hosted renders expire.")

    if st.button("Check status"):
        placeholder = st.empty()
        video_url = None

        for _ in range(90):
            state, video_url, message = JSON2Video.parse_status(
                renderer.status(st.session_state.job))
            placeholder.write(f"Status: **{state}**")

            if state in ("done", "completed", "success"):
                break
            if state in ("error", "failed"):
                st.error(f"Render failed: {message or state}")
                video_url = None
                break
            time.sleep(8)   # docs: never poll faster than every 5 seconds
        else:
            st.warning("Still rendering. Check again shortly.")

        if video_url:
            script = st.session_state.script
            slug = slugify(script["title"])

            st.video(video_url)
            st.markdown(f"[Direct download]({video_url})")

            try:
                st.success(f"Saved to {save_local(video_url, slug)}")
            except Exception as e:
                st.warning(f"Could not save locally: {e}")

            transcript = "\n\n".join(s["narration"] for s in script["scenes"])
            st.subheader("Transcript")
            st.text_area("For the page body", transcript, height=180)
            st.download_button("Download transcript", transcript,
                               f"{slug}-transcript.txt")

            st.subheader("VideoObject JSON-LD")
            jsonld = video_object_jsonld(
                script["title"], script["description"],
                st.session_state.page_url, video_url, transcript,
                estimate_seconds(script))
            st.code(jsonld, language="json")
            st.download_button("Download JSON-LD", jsonld,
                               f"{slug}-videoobject.json")
