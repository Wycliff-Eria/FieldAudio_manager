"""
FieldAudio Manager
- CommCare credentials are verified before access to the downloader.
- CommCare API credentials are held only in the current Streamlit session.
- Each browser session gets its own temporary workspace.
- Audio files are downloaded in parallel and exported with ZIP_STORED.
"""

import io
import os
import re
import shutil
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import requests
import streamlit as st


# ---------------------------------------------------------------------
# Page setup and configuration
# ---------------------------------------------------------------------
st.set_page_config(
    page_title="FieldAudio Manager",
    page_icon="🎧",
    layout="wide",
    initial_sidebar_state="collapsed",
)

APP_NAME = "FieldAudio Manager"
USER_AGENT = "FieldAudio-Manager/0.1"
AUDIO_NAME_COLUMN = "audio_name"
AUDIO_URL_COLUMN = "survey_audio_link"
SUPPORTED_EXTENSIONS = (".m4a", ".mp3", ".wav", ".aac", ".ogg", ".amr", ".opus")
MAX_WORKERS = 20
CHUNK_SIZE = 1024 * 1024


def get_secret(name: str, default: str = "") -> str:
    """Read a value from Streamlit secrets first, then environment variables."""
    try:
        value = st.secrets.get(name, default)
        if value is not None and str(value).strip():
            return str(value).strip()
    except Exception:
        pass
    return os.getenv(name, default).strip()


def clean_filename(value: object) -> str:
    name = str(value).strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", "_", name)
    name = name.strip(" ._")
    return name[:160] or "audio"


def get_extension(url: str) -> str:
    path = urlparse(str(url)).path.lower()
    suffix = Path(path).suffix
    return suffix if suffix in SUPPORTED_EXTENSIONS else ".m4a"


def is_http_url(value: str) -> bool:
    try:
        parsed = urlparse(str(value).strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def create_filenames(df: pd.DataFrame) -> list[str]:
    seen = set()
    result = []
    for name, url in zip(df[AUDIO_NAME_COLUMN], df[AUDIO_URL_COLUMN]):
        base = clean_filename(name)
        ext = get_extension(url)
        candidate = f"{base}{ext}"
        n = 1
        while candidate.casefold() in seen:
            candidate = f"{base}_{n}{ext}"
            n += 1
        seen.add(candidate.casefold())
        result.append(candidate)
    return result


def init_session_state() -> None:
    defaults = {
        "authenticated": False,
        "username": "",
        "commcare_domain": "m-e-uganda",
        "session_headers": None,
        "workspace": None,
        "login_error": "",
        "results": None,
        "failed_csv": None,
        "zip_path": None,
        "zip_name": "fieldaudio_audio_files.zip",
        "zip_signature": None,
        "last_upload_name": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def ensure_workspace() -> Path:
    current = st.session_state.get("workspace")
    if current and Path(current).is_dir():
        return Path(current)
    path = Path(tempfile.mkdtemp(prefix="fieldaudio_"))
    st.session_state.workspace = str(path)
    return path


def clear_workspace() -> None:
    current = st.session_state.get("workspace")
    if current:
        try:
            shutil.rmtree(current, ignore_errors=True)
        except Exception:
            pass
    st.session_state.workspace = None


def reset_commcare_session(delete_files: bool = True) -> None:
    if delete_files:
        clear_workspace()
    st.session_state.authenticated = False
    st.session_state.username = ""
    st.session_state.session_headers = None
    st.session_state.results = None
    st.session_state.failed_csv = None
    st.session_state.zip_path = None
    st.session_state.zip_signature = None
    st.session_state.last_upload_name = None


def verify_commcare_credentials(domain: str, username: str, api_key: str):
    domain = domain.strip()
    domain = re.sub(r"^https?://", "", domain, flags=re.I)
    domain = domain.split("/")[0].strip()
    domain = re.sub(r"\.commcarehq\.org$", "", domain, flags=re.I)
    if not domain or not re.fullmatch(r"[A-Za-z0-9._-]+", domain):
        return False, "Enter a valid CommCare domain."
    if not username.strip() or not api_key.strip():
        return False, "Enter your CommCare username and API key."

    headers = {
        "Authorization": f"ApiKey {username.strip()}:{api_key.strip()}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    # Lightweight check; do not download case data during sign-in.
    url = f"https://www.commcarehq.org/a/{domain}/api/v0.5/user/"
    try:
        response = requests.get(
            url,
            headers=headers,
            params={"limit": 1},
            timeout=(4, 8),
        )
        if response.status_code == 200:
            return True, (domain, username.strip(), headers)
        if response.status_code == 401:
            return False, "CommCare rejected the username or API key (HTTP 401)."
        if response.status_code == 403:
            return False, (
                "CommCare returned HTTP 403. The account may lack permission "
                "for this endpoint/domain. Confirm access with your CommCare administrator."
            )
        if response.status_code == 404:
            return False, "CommCare domain or verification endpoint was not found (HTTP 404)."
        return False, f"CommCare verification failed (HTTP {response.status_code})."
    except requests.exceptions.ConnectTimeout:
        return False, "Connection timed out while reaching CommCare. Try again."
    except requests.exceptions.ReadTimeout:
        return False, "CommCare did not respond in time. Try again."
    except requests.exceptions.RequestException as exc:
        return False, f"Could not connect to CommCare ({type(exc).__name__})."


def download_one(headers: dict, url: str, filename: str, workspace: Path, timeout: int):
    if not is_http_url(url):
        return {"success": False, "filename": filename, "url": url,
                "error": "Invalid URL (expected http:// or https://).", "size_bytes": 0}

    destination = workspace / filename
    partial = workspace / (filename + ".part")
    try:
        with requests.Session() as session:
            session.headers.update(headers)
            with session.get(url, stream=True, timeout=(5, timeout), allow_redirects=True) as response:
                if response.status_code != 200:
                    reason = {
                        401: "HTTP 401 - Unauthorized",
                        403: "HTTP 403 - Permission denied",
                        404: "HTTP 404 - Audio not found",
                        429: "HTTP 429 - Rate limited",
                    }.get(response.status_code, f"HTTP {response.status_code}")
                    return {"success": False, "filename": filename, "url": url,
                            "error": reason, "size_bytes": 0}
                total = 0
                with open(partial, "wb") as target:
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            target.write(chunk)
                            total += len(chunk)
        if total <= 0:
            raise IOError("The server returned an empty file.")
        os.replace(partial, destination)
        return {"success": True, "filename": filename, "url": url,
                "error": "", "size_bytes": total}
    except requests.exceptions.RequestException as exc:
        try:
            partial.unlink(missing_ok=True)
        except Exception:
            pass
        return {"success": False, "filename": filename, "url": url,
                "error": f"{type(exc).__name__}: {exc}", "size_bytes": 0}
    except Exception as exc:
        try:
            partial.unlink(missing_ok=True)
        except Exception:
            pass
        return {"success": False, "filename": filename, "url": url,
                "error": f"{type(exc).__name__}: {exc}", "size_bytes": 0}


# ---------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------
st.markdown(
    """
    <style>
    .block-container {max-width: 1180px; padding-top: 2rem; padding-bottom: 3rem;}
    .brand-row {display:flex; align-items:center; gap:14px; margin-bottom:8px;}
    .brand-mark {display:flex; align-items:center; justify-content:center;
        width:48px; height:48px; border-radius:14px; background:#155eef;
        color:white; font-size:24px;}
    .brand-title {font-size:30px; line-height:1.15; font-weight:750; color:#101828;}
    .brand-subtitle {color:#667085; margin-top:5px; margin-bottom:24px;}
    .login-card {max-width:520px; margin:25px auto; padding:28px 30px;
        border:1px solid #eaecf0; border-radius:18px; background:#fff;
        box-shadow:0 12px 32px rgba(16,24,40,.07);}
    div[data-testid="stButton"] button, div[data-testid="stDownloadButton"] button {
        border-radius:9px; font-weight:650; min-height:42px;}
    div[data-testid="stMetric"] {border:1px solid #eaecf0; border-radius:12px; padding:14px;}
    [data-testid="stSidebar"] {border-right:1px solid #eaecf0;}
    </style>
    """,
    unsafe_allow_html=True,
)


def brand_header(subtitle: str = "Secure, efficient audio downloads for field teams") -> None:
    st.markdown(
        f"""
        <div class="brand-row">
          <div class="brand-mark">🎧</div>
          <div><div class="brand-title">{APP_NAME}</div></div>
        </div>
        <div class="brand-subtitle">{subtitle}</div>
        """,
        unsafe_allow_html=True,
    )


init_session_state()


# ---------------------------------------------------------------------
# Full-page CommCare login
# ---------------------------------------------------------------------
if not st.session_state.authenticated:
    st.markdown('<div class="login-card">', unsafe_allow_html=True)
    brand_header("Sign in with your own CommCare credentials")
    st.info("FieldAudio Manager is an independent utility and is not an official CommCare product.")
    with st.form("commcare_login_form", clear_on_submit=False):
        domain_input = st.text_input(
            "CommCare domain",
            value=st.session_state.commcare_domain,
            placeholder="e.g. m-e-uganda",
        )
        username_input = st.text_input(
            "CommCare username",
            placeholder="Your CommCare username",
        )
        api_key_input = st.text_input(
            "CommCare API key",
            type="password",
            placeholder="Enter your API key",
        )
        login_submit = st.form_submit_button(
            "🔐 Verify & Sign In", type="primary", use_container_width=True
        )
    if st.session_state.login_error:
        st.error(st.session_state.login_error)

    if login_submit:
        with st.spinner("Verifying credentials with CommCare…"):
            ok, result = verify_commcare_credentials(
                domain_input, username_input, api_key_input
            )
        if ok:
            domain, username, headers = result
            st.session_state.commcare_domain = domain
            st.session_state.username = username
            st.session_state.session_headers = headers
            st.session_state.authenticated = True
            st.session_state.login_error = ""
            ensure_workspace()
            st.rerun()
        else:
            st.session_state.login_error = result
            st.rerun()

    st.caption("Your API key is used for this session only; FieldAudio Manager does not save it to a database or file.")
    st.markdown("</div>", unsafe_allow_html=True)
    st.stop()


# ---------------------------------------------------------------------
# Authenticated workspace
# ---------------------------------------------------------------------
with st.sidebar:
    st.markdown(f"### 🎧 {APP_NAME}")
    st.caption("CommCare audio downloader")
    st.write(f"CommCare user: **{st.session_state.username}**")
    st.write(f"Domain: `{st.session_state.commcare_domain}`")
    if st.button("Log out", use_container_width=True):
        reset_commcare_session(delete_files=True)
        st.rerun()

brand_header("Upload a CSV or Excel list, download audio in parallel, and export a ZIP.")
st.success("CommCare credentials verified for this session.")
st.caption("Expected columns: `audio_name` and `survey_audio_link`.")

uploaded = st.file_uploader(
    "Upload your audio list",
    type=["csv", "xlsx", "xls"],
    help="CSV and Excel files are supported. The original upload is not saved permanently.",
)

if uploaded is None:
    st.markdown("### How it works")
    cols = st.columns(3)
    cols[0].markdown("**1. Upload**\n\nChoose your CSV or Excel audio list.")
    cols[1].markdown("**2. Download**\n\nRun parallel downloads with progress tracking.")
    cols[2].markdown("**3. Export**\n\nDownload a fast, uncompressed ZIP and a failed-download CSV.")
    st.stop()

try:
    raw = uploaded.getvalue()
    if uploaded.name.lower().endswith(".csv"):
        df = pd.read_csv(io.BytesIO(raw))
    else:
        df = pd.read_excel(io.BytesIO(raw))
except Exception as exc:
    st.error(f"Could not read the uploaded file: {type(exc).__name__}: {exc}")
    st.stop()

df.columns = [str(col).strip() for col in df.columns]
required = [AUDIO_NAME_COLUMN, AUDIO_URL_COLUMN]
missing = [col for col in required if col not in df.columns]
if missing:
    st.error("Missing required column(s): " + ", ".join(missing))
    st.write("Columns found:", list(df.columns))
    st.stop()

df = df[required].copy()
for col in required:
    df[col] = df[col].fillna("").astype(str).str.strip()

df = df[(df[AUDIO_NAME_COLUMN] != "") & (df[AUDIO_URL_COLUMN] != "")].copy()
df = df[df[AUDIO_URL_COLUMN].map(is_http_url)].reset_index(drop=True)
if df.empty:
    st.warning("No valid rows were found. Check that the file contains non-empty audio names and HTTP/HTTPS audio links.")
    st.stop()

df["filename"] = create_filenames(df)
workspace = ensure_workspace()
existing = {p.name for p in workspace.iterdir() if p.is_file() and not p.name.endswith(".part")}
df["already_downloaded"] = df["filename"].isin(existing)

m1, m2, m3 = st.columns(3)
m1.metric("Valid audio links", f"{len(df):,}")
m2.metric("Already downloaded", f"{int(df['already_downloaded'].sum()):,}")
m3.metric("Remaining", f"{int((~df['already_downloaded']).sum()):,}")

with st.expander("Preview files"):
    st.dataframe(df[[AUDIO_NAME_COLUMN, AUDIO_URL_COLUMN, "filename", "already_downloaded"]],
                 use_container_width=True, height=280)

settings1, settings2 = st.columns(2)
with settings1:
    workers = int(st.number_input("Parallel downloads", min_value=1, max_value=MAX_WORKERS, value=4, step=1))
with settings2:
    timeout = int(st.number_input("Timeout per file (seconds)", min_value=10, max_value=300, value=90, step=10))
st.caption("Start with 3–5 workers. More workers may be limited by network conditions or CommCare rate limits.")

if st.button("⬇ Start / Retry Downloads", type="primary", use_container_width=True):
    pending = df[~df["filename"].isin(existing)].copy()
    failures = []
    downloaded = 0
    skipped = len(df) - len(pending)
    progress = st.progress(0)
    status = st.empty()

    if pending.empty:
        st.success("All files in this upload are already available in this session.")
        st.session_state.results = {
            "total": len(df), "downloaded": 0, "skipped": skipped, "failed": 0
        }
        st.session_state.failed_csv = None
    else:
        tasks = list(pending[[AUDIO_NAME_COLUMN, AUDIO_URL_COLUMN, "filename"]].itertuples(index=False, name=None))
        total = len(tasks)
        completed = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {
                pool.submit(
                    download_one,
                    st.session_state.session_headers,
                    url,
                    filename,
                    workspace,
                    timeout,
                ): (audio_name, url, filename)
                for audio_name, url, filename in tasks
            }
            for future in as_completed(future_map):
                audio_name, url, filename = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"success": False, "filename": filename, "url": url,
                              "error": f"{type(exc).__name__}: {exc}", "size_bytes": 0}
                completed += 1
                progress.progress(completed / total)
                if result["success"]:
                    downloaded += 1
                    status.write(f"Downloaded {completed:,}/{total:,}: {filename}")
                else:
                    failures.append({
                        "audio_name": audio_name,
                        "survey_audio_link": url,
                        "filename": filename,
                        "error": result["error"],
                    })
                    status.write(f"Failed {completed:,}/{total:,}: {filename}")

        st.session_state.results = {
            "total": len(df),
            "downloaded": downloaded,
            "skipped": skipped,
            "failed": len(failures),
        }
        st.session_state.failed_csv = (
            pd.DataFrame(failures).to_csv(index=False).encode("utf-8-sig")
            if failures else None
        )
        st.session_state.zip_path = None
        st.session_state.zip_signature = None
        if failures:
            st.warning(f"Finished with {len(failures)} failed file(s). Download the failure report below, then retry.")
        else:
            st.success("Download batch completed successfully.")

if st.session_state.results:
    st.markdown("### Latest download results")
    result = st.session_state.results
    r1, r2, r3, r4 = st.columns(4)
    r1.metric("Rows", result["total"])
    r2.metric("Downloaded this run", result["downloaded"])
    r3.metric("Skipped / already saved", result["skipped"])
    r4.metric("Failed", result["failed"])

if st.session_state.failed_csv:
    st.markdown("### Failed downloads")
    st.download_button(
        "⬇ Download failed_audio_files.csv",
        data=st.session_state.failed_csv,
        file_name="failed_audio_files.csv",
        mime="text/csv",
        use_container_width=True,
    )

# ZIP includes only successfully downloaded audio files in this session.
audio_files = sorted(
    p for p in workspace.iterdir()
    if p.is_file() and not p.name.endswith(".part") and p.suffix.lower() in SUPPORTED_EXTENSIONS
)
st.markdown("### Export audio files")
if audio_files:
    total_bytes = sum(p.stat().st_size for p in audio_files)
    st.write(f"{len(audio_files):,} audio file(s) ready • {total_bytes / (1024 * 1024):.2f} MB before ZIP overhead.")

    # Build the ZIP directly on disk, not in a BytesIO buffer. ZIP_STORED
    # avoids CPU time spent recompressing audio files.
    signature = tuple((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in audio_files)
    zip_path = workspace / st.session_state.zip_name
    if (
        st.session_state.zip_path != str(zip_path)
        or st.session_state.zip_signature != signature
        or not zip_path.is_file()
    ):
        with st.spinner("Creating ZIP directly on disk (no compression)…"):
            zip_path.unlink(missing_ok=True)
            with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_STORED) as zf:
                for path in audio_files:
                    zf.write(path, arcname=path.name)
        st.session_state.zip_path = str(zip_path)
        st.session_state.zip_signature = signature

    # Streamlit accepts a binary file object for download; the ZIP itself is
    # written directly to the session workspace rather than first copied into RAM.
    with open(zip_path, "rb") as zip_handle:
        st.download_button(
            "⬇ Download ZIP (ZIP_STORED)",
            data=zip_handle,
            file_name=st.session_state.zip_name,
            mime="application/zip",
            type="primary",
            use_container_width=True,
        )
else:
    st.info("No downloaded audio files are available for ZIP export yet.")

st.divider()
st.caption(
    "Downloaded files are isolated to this browser session's temporary workspace and are deleted when you log out. "
    "Avoid using a shared or untrusted computer."
)
