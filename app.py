import streamlit as st

import pandas as pd

import requests

import io

import zipfile

import re

import os
import tempfile

import time

from urllib.parse import urlparse

from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================

# PAGE CONFIG

# ============================================================

st.set_page_config(

    page_title="CommCare Audio Downloader",

    page_icon="🎧",

    layout="wide"

)

# ============================================================

# ============================================================
# CUSTOM CSS
# ============================================================

st.markdown(
    """
    <style>

    /* Consistent Streamlit layout */
    .main-title {
        font-size: 38px;
        font-weight: 700;
        margin-bottom: 5px;
        line-height: 1.2;
    }

    .subtitle {
        color: #666666;
        font-size: 17px;
        margin-bottom: 25px;
        line-height: 1.5;
    }

    /* Metric cards */
    div[data-testid="stMetric"] {
        background-color: #ffffff;
        border: 1px solid #dddddd;
        border-radius: 10px;
        padding: 15px;
    }

    /* Compact buttons - applies to all action/download buttons */
    div[data-testid="stButton"] > button,
    div[data-testid="stDownloadButton"] > button {
        background-color: #0066CC;
        color: #ffffff;
        border: none;
        border-radius: 4px;
        padding: 4px 10px !important;
        min-height: 32px !important;
        height: 32px !important;
        width: auto !important;
        min-width: 0 !important;
        font-size: 12px !important;
        font-weight: 600;
        line-height: 1.2 !important;
        white-space: nowrap;
    }

    div[data-testid="stButton"] > button:hover,
    div[data-testid="stDownloadButton"] > button:hover {
        background-color: #004C99;
        color: #ffffff;
    }

    /* Keep button containers compact even when use_container_width=True */
    div[data-testid="stButton"],
    div[data-testid="stDownloadButton"] {
        width: fit-content !important;
    }

    /* Compact buttons inside the sidebar too */
    section[data-testid="stSidebar"] div[data-testid="stButton"] > button,
    section[data-testid="stSidebar"] div[data-testid="stDownloadButton"] > button {
        padding: 3px 9px !important;
        min-height: 30px !important;
        height: 30px !important;
        font-size: 12px !important;
    }

    .auth-success {
        padding: 10px;
        border-radius: 3px;
        background-color: #e8f5e9;
        color: #1b5e20;
        font-weight: 600;
    }

    .success-box {
        padding: 15px;
        border-radius: 10px;
        background-color: #e8f5e9;
        border: 1px solid #81c784;
        margin-bottom: 15px;
    }

    .warning-box {
        padding: 15px;
        border-radius: 10px;
        background-color: #fff8e1;
        border: 1px solid #ffcc80;
        margin-bottom: 15px;
    }

    .error-box {
        padding: 15px;
        border-radius: 10px;
        background-color: #ffebee;
        border: 1px solid #ef9a9a;
        margin-bottom: 15px;
    }

    /* Compact buttons throughout the app */
    div[data-testid="stButton"] > button,
    div[data-testid="stDownloadButton"] > button {
        padding: 4px 10px !important;
        min-height: 32px !important;
        height: 32px !important;
        width: auto !important;
        min-width: 0 !important;
        font-size: 12px !important;
        line-height: 1.2 !important;
        white-space: nowrap;
    }

    div[data-testid="stButton"],
    div[data-testid="stDownloadButton"] {
        width: fit-content !important;
    }

    section[data-testid="stSidebar"] div[data-testid="stButton"] > button,
    section[data-testid="stSidebar"] div[data-testid="stDownloadButton"] > button {
        padding: 3px 9px !important;
        min-height: 30px !important;
        height: 30px !important;
        font-size: 12px !important;
    }

    </style>
    """,
    unsafe_allow_html=True
)

# ============================================================
# CONSTANTS

# ============================================================

AUDIO_NAME_COLUMN = "audio_name"

LINK_COLUMNS = [

    "audio_link1",

    "audio_link2",

    "audio_link3"

]

CHUNK_SIZE = 1024 * 1024  # 1 MB

RETRY_STATUS_CODES = {

    408, 429, 500, 502, 503, 504

}

USER_AGENT = "CommCare-Audio-Downloader/2.0"

# ============================================================

# CUSTOM CSS

# ============================================================

st.markdown(

    """

    <style>

    .main-title {

        font-size: 38px;

        font-weight: 700;

        margin-bottom: 5px;

    }

    .subtitle {

        color: #666;

        font-size: 17px;

        margin-bottom: 25px;

    }

    .success-box {

        padding: 15px;

        border-radius: 10px;

        background-color: #e8f5e9;

        border: 1px solid #81c784;

        margin-bottom: 15px;

    }

    .warning-box {

        padding: 15px;

        border-radius: 10px;

        background-color: #fff8e1;

        border: 1px solid #ffcc80;

        margin-bottom: 15px;

    }

    .error-box {

        padding: 15px;

        border-radius: 10px;

        background-color: #ffebee;

        border: 1px solid #ef9a9a;

        margin-bottom: 15px;

    }

    </style>

    """,

    unsafe_allow_html=True

)

# ============================================================

# ============================================================
# SESSION STATE
# ============================================================

SESSION_DEFAULTS = {
    "authenticated": False,
    "commcare_domain": "",
    "username": "",
    "api_key": "",
    "session_headers": None,
    "completed_files": {},
    "failed_downloads": [],
    "download_started": False,
    "processed_count": 0,
    "total_audio_count": 0,
    "workspace_dir": None,
}

for _key, _default in SESSION_DEFAULTS.items():
    if _key not in st.session_state:
        st.session_state[_key] = _default

# Store downloaded audio on disk instead of keeping every recording in RAM.
if not st.session_state.workspace_dir or not os.path.isdir(st.session_state.workspace_dir):
    st.session_state.workspace_dir = tempfile.mkdtemp(prefix="commcare_audio_session_")

if "final_zip_path" not in st.session_state:
    st.session_state["final_zip_path"] = None
if "final_zip_signature" not in st.session_state:
    st.session_state["final_zip_signature"] = None

# HELPER FUNCTIONS

# ============================================================

def clean_filename(filename):

    """

    Remove characters that are unsafe in filenames.

    """

    filename = str(filename).strip()

    if not filename:

        filename = "audio"

    filename = re.sub(r'[<>:"/\\\\|?*\x00-\x1F]', "_", filename)

    filename = re.sub(r"\s+", " ", filename)

    filename = filename.rstrip(". ")

    return filename[:180]

def get_extension_from_url(url):

    """

    Try to determine the audio extension from the URL.

    """

    try:

        path = urlparse(url).path.lower()

        extensions = [

            ".m4a",

            ".mp3",

            ".wav",

            ".aac",

            ".ogg",

            ".amr",

            ".3gp",

            ".webm"

        ]

        for ext in extensions:

            if path.endswith(ext):

                return ext

    except Exception:

        pass

    return ".m4a"

def make_unique_filename(filename, existing_names):

    """

    Make sure duplicate filenames are unique.

    """

    if filename not in existing_names:

        return filename

    base, ext = os.path.splitext(filename)

    counter = 1

    while f"{base}_{counter}{ext}" in existing_names:

        counter += 1

    return f"{base}_{counter}{ext}"

def rename_first_four_columns(df):

    """

    Rename the first four columns:

    1st column -> audio_name

    2nd column -> audio_link1

    3rd column -> audio_link2

    4th column -> audio_link3

    Any additional columns are preserved.

    """

    df = df.copy()

    if len(df.columns) < 2:

        raise ValueError(

            "The uploaded file must contain at least "

            "an audio name column and one audio link column."

        )

    new_names = [

        "audio_name",

        "audio_link1",

        "audio_link2",

        "audio_link3"

    ]

    columns = list(df.columns)

    for i in range(min(4, len(columns))):

        columns[i] = new_names[i]

    df.columns = columns

    return df

def load_uploaded_file(uploaded_file):

    """

    Load Excel or CSV file.

    """

    filename = uploaded_file.name.lower()

    if filename.endswith(".csv"):

        df = pd.read_csv(uploaded_file)

    elif filename.endswith(".xlsx"):

        df = pd.read_excel(uploaded_file, engine="openpyxl")

    elif filename.endswith(".xls"):

        try:

            df = pd.read_excel(uploaded_file, engine="xlrd")

        except ImportError:

            raise ValueError(

                "Reading .xls files requires xlrd. "

                "Install it using: pip install xlrd"

            )

    else:

        raise ValueError(

            "Unsupported file type. Please upload CSV, XLSX or XLS."

        )

    return rename_first_four_columns(df)

def clean_url(value):

    """

    Clean and validate an audio URL.

    """

    if pd.isna(value):

        return None

    value = str(value).strip()

    if not value:

        return None

    if value.lower() in ["nan", "none", "null", "---"]:

        return None

    if not value.startswith(("http://", "https://")):

        return None

    return value

def count_audio_links(df):

    """

    Count all non-empty audio links.

    """

    count = 0

    for column in LINK_COLUMNS:

        if column not in df.columns:

            continue

        for value in df[column]:

            if clean_url(value):

                count += 1

    return count

def build_audio_tasks(df):

    """

    Convert the DataFrame into individual audio download tasks.

    One row can produce:

        name + link1

        name + link2

        name + link3

    """

    tasks = []

    for row_number, row in df.iterrows():

        audio_name = row.get(AUDIO_NAME_COLUMN)

        if pd.isna(audio_name):

            audio_name = f"audio_{row_number + 1}"

        audio_name = clean_filename(audio_name)

        link_number = 0

        for link_column in LINK_COLUMNS:

            if link_column not in df.columns:

                continue

            url = clean_url(row.get(link_column))

            if not url:

                continue

            link_number += 1

            extension = get_extension_from_url(url)

            filename = (

                f"{audio_name}_{link_number}{extension}"

            )

            tasks.append(

                {

                    "row_number": row_number + 2,

                    "audio_name": audio_name,

                    "link_column": link_column,

                    "url": url,

                    "filename": filename

                }

            )

    return tasks

# ============================================================

# DOWNLOAD FUNCTION

# ============================================================

def download_audio(
    task,
    username,
    api_key,
    timeout,
    retries,
    workspace_dir,
):
    """Stream one audio file directly to disk to avoid high RAM use."""
    url = task["url"]
    filename = task["filename"]
    headers = {
        "Authorization": f"ApiKey {username}:{api_key}",
        "User-Agent": USER_AGENT,
    }
    last_error = "Unknown error"

    for attempt in range(1, retries + 1):
        temp_path = None
        response = None
        try:
            response = requests.get(
                url, headers=headers, stream=True, timeout=timeout
            )

            if response.status_code == 401:
                return {"success": False, "filename": filename, "path": None,
                        "error": "401 Unauthorized - check CommCare username/API key."}
            if response.status_code == 403:
                return {"success": False, "filename": filename, "path": None,
                        "error": "403 Forbidden - API key may not have access to this recording."}

            if response.status_code == 200:
                suffix = os.path.splitext(filename)[1] or ".audio"
                fd, temp_path = tempfile.mkstemp(
                    prefix="audio_part_", suffix=suffix, dir=workspace_dir
                )
                os.close(fd)
                with open(temp_path, "wb") as audio_file:
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            audio_file.write(chunk)
                response.close()
                return {"success": True, "filename": filename,
                        "path": temp_path, "error": None}

            if response.status_code in RETRY_STATUS_CODES:
                last_error = f"HTTP {response.status_code}"
                if attempt < retries:
                    time.sleep(min(2 ** attempt, 10))
                    continue
            else:
                last_error = f"HTTP {response.status_code}: {response.reason}"
            return {"success": False, "filename": filename, "path": None,
                    "error": last_error}

        except requests.exceptions.Timeout:
            last_error = "Request timed out."
        except requests.exceptions.ConnectionError:
            last_error = "Connection interrupted or network unavailable."
        except requests.exceptions.RequestException as exc:
            last_error = str(exc)
        except Exception as exc:
            last_error = str(exc)
            break
        finally:
            if response is not None:
                response.close()

        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        if attempt < retries:
            time.sleep(min(2 ** attempt, 10))

    return {"success": False, "filename": filename, "path": None,
            "error": last_error}

# ============================================================

# CREATE ZIP

# ============================================================

def create_zip_from_completed_files():
    """Build/reuse an uncompressed ZIP by adding disk files, not RAM bytes."""
    completed = st.session_state.completed_files  # filename -> local path
    if not completed:
        return None

    signature_parts = []
    for name, path in completed.items():
        if not os.path.isfile(path):
            continue
        stat = os.stat(path)
        signature_parts.append((name, stat.st_size, stat.st_mtime_ns))
    signature = tuple(signature_parts)

    existing_path = st.session_state.get("final_zip_path")
    if (existing_path and st.session_state.get("final_zip_signature") == signature
            and os.path.isfile(existing_path)):
        return existing_path

    if existing_path and os.path.isfile(existing_path):
        try:
            os.remove(existing_path)
        except OSError:
            pass

    workspace = st.session_state.workspace_dir
    fd, zip_path = tempfile.mkstemp(
        prefix="commcare_audio_", suffix=".zip", dir=workspace
    )
    os.close(fd)
    try:
        with zipfile.ZipFile(
            zip_path, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True
        ) as zip_file:
            for filename, audio_path in completed.items():
                if os.path.isfile(audio_path):
                    zip_file.write(audio_path, arcname=filename)

        st.session_state.final_zip_path = zip_path
        st.session_state.final_zip_signature = signature
        return zip_path
    except Exception:
        try:
            os.remove(zip_path)
        except OSError:
            pass
        raise


def create_failed_csv():

    """

    Create CSV containing failed downloads.

    """

    if not st.session_state.failed_downloads:

        return None

    failed_df = pd.DataFrame(

        st.session_state.failed_downloads

    )

    return failed_df.to_csv(

        index=False

    ).encode("utf-8")

# ============================================================

# TITLE

# ============================================================

st.markdown(

    '<div class="main-title">🎧 CommCare Audio Downloader</div>',

    unsafe_allow_html=True

)

st.markdown(

    """

    <div class="subtitle">

    Upload your audio list, download CommCare recordings,

    rename them automatically, and package completed files

    into a ZIP.

    </div>

    """,

    unsafe_allow_html=True

)

# ============================================================

# SIDEBAR

# ============================================================

st.sidebar.title("Audio Downloader")

if not st.session_state.authenticated:

    st.sidebar.subheader("CommCare Authentication")

    commcare_domain = st.sidebar.text_input(
        "CommCare Domain",
        value=st.session_state.commcare_domain,
        key="commcare_domain_input"
    ).strip()

    username = st.sidebar.text_input(
        "CommCare Username",
        value=st.session_state.username,
        key="username_input"
    ).strip()

    api_key = st.sidebar.text_input(
        "CommCare API Key",
        value=st.session_state.api_key,
        type="password",
        key="api_key_input"
    ).strip()

    verify_key = st.sidebar.button(

        "Verify API Key",

        type="primary"

    )

    if verify_key:

        if not commcare_domain:
            st.sidebar.error("Please enter your CommCare domain.")

        elif not username or not api_key:
            st.sidebar.error("Please enter your username and API key.")

        else:
            headers = {

                "Authorization": f"ApiKey {username}:{api_key}",

                "User-Agent": "CommCare-Audio-Downloader/3.0",

                "Accept": "*/*",

            }

            test_url = (

                f"https://www.commcarehq.org/a/"

                f"{commcare_domain}/api/v0.5/case/"

            )

            try:

                response = requests.get(

                    test_url,

                    headers=headers,

                    params={"limit": 1},

                    timeout=20

                )

                if response.status_code == 200:

                    st.session_state.authenticated = True

                    st.session_state.username = username
                    st.session_state.api_key = api_key
                    st.session_state.commcare_domain = commcare_domain
                    st.session_state.session_headers = headers

                    st.rerun()

                elif response.status_code == 401:

                    st.sidebar.error(

                        "Invalid username or API key."

                    )

                elif response.status_code == 403:

                    st.sidebar.error(

                        "Authentication succeeded, but you do not have permission."

                    )

                else:

                    st.sidebar.error(

                        f"Authentication failed. HTTP {response.status_code}"

                    )

            except requests.exceptions.RequestException as e:

                st.sidebar.error(f"Connection error: {e}")

else:

    st.sidebar.markdown(

        """

        <div class="auth-success">

        ✅ CommCare authenticated

        </div>

        """,

        unsafe_allow_html=True

    )

    st.sidebar.write(

        f"User: {st.session_state.username}"

    )

    if st.sidebar.button("Log out"):

        st.session_state.authenticated = False

        st.session_state.session_headers = None

        st.session_state.username = None

        st.session_state.download_complete = False

        st.session_state.failed_csv = None

        st.session_state.download_results = None

        st.session_state.zip_bytes = None

        st.session_state.zip_filename = None

        st.session_state.final_zip_path = None

        st.session_state.final_zip_size = 0

        st.session_state.zip_ready = False

        st.rerun()

    st.sidebar.divider()

    st.sidebar.subheader("Download Settings")

    timeout = st.sidebar.number_input(
        "Timeout per file (seconds)",
        min_value=10,
        max_value=600,
        value=60,
        step=10,
        help="Maximum time allowed for each audio file."
    )

    retries = st.sidebar.number_input(
        "Retries per file",
        min_value=1,
        max_value=10,
        value=3,
        step=1,
        help="Number of retry attempts when a download fails."
    )

    workers = st.sidebar.number_input(
        "Parallel downloads",
        min_value=1,
        max_value=10,
        value=3,
        step=1,
        help="Number of audio files downloaded at the same time."
    )

    st.sidebar.divider()

    if st.session_state.completed_files:

        st.success(

            f"✅ {len(st.session_state.completed_files)} "

            "completed files retained."

        )

    if st.session_state.failed_downloads:

        st.warning(

            f"⚠️ {len(st.session_state.failed_downloads)} "

            "failed downloads."

        )

    if st.sidebar.button(

        "🗑️ Clear Previous Downloads",

        use_container_width=True

    ):

        old_zip_path = st.session_state.get("final_zip_path")
        if old_zip_path and os.path.isfile(old_zip_path):
            try:
                os.remove(old_zip_path)
            except OSError:
                pass
        st.session_state["final_zip_path"] = None
        st.session_state["final_zip_signature"] = None
        # Remove previously downloaded audio files from this session workspace.
        for old_path in list(st.session_state.completed_files.values()):
            try:
                if os.path.isfile(old_path):
                    os.remove(old_path)
            except OSError:
                pass
        st.session_state.completed_files = {}

        st.session_state.failed_downloads = []

        st.session_state.download_started = False

        st.session_state.processed_count = 0

        st.session_state.total_audio_count = 0

        st.rerun()

# ============================================================

# Use persisted credentials after authentication.
username = st.session_state.username
api_key = st.session_state.api_key
commcare_domain = st.session_state.commcare_domain


# UPLOAD FILE

# ============================================================

st.header("Upload Audio List")

uploaded_file = st.file_uploader(

    "Upload Excel or CSV file",

    type=["xlsx", "xls", "csv"],

    help=(

        "The first four columns should be: "

        "audio name, link 1, link 2, link 3."

    )

)

# ============================================================

# SHOW DATA

# ============================================================

df = None

if uploaded_file:

    try:

        df = load_uploaded_file(

            uploaded_file

        )

        st.success(

            f"File loaded successfully: "

            f"**{uploaded_file.name}**"

        )

        st.subheader("Uploaded Data")

        st.dataframe(

            df,

            use_container_width=True,

            height=300

        )

        st.info(

            "The first four columns are interpreted as: "

            "**audio_name, audio_link1, audio_link2, audio_link3**."

        )

        # ----------------------------------------------------

        # CHECK REQUIRED COLUMN

        # ----------------------------------------------------

        if AUDIO_NAME_COLUMN not in df.columns:

            st.error(

                "The first column could not be identified as "

                "`audio_name`."

            )

            st.stop()

        # ----------------------------------------------------

        # COUNT LINKS

        # ----------------------------------------------------

        total_links = count_audio_links(df)

        st.metric(

            "Total audio links",

            total_links

        )

        # ----------------------------------------------------

        # BUILD TASKS

        # ----------------------------------------------------

        tasks = build_audio_tasks(df)

        if not tasks:

            st.warning(

                "No valid audio links were found."

            )

        else:

            st.subheader("Summary")

            summary_col1, summary_col2, summary_col3 = st.columns(3)

            with summary_col1:

                st.metric(

                    "Rows",

                    len(df)

                )

            with summary_col2:

                st.metric(

                    "Audio files",

                    len(tasks)

                )

            with summary_col3:

                st.metric(

                    "Names",

                    df[AUDIO_NAME_COLUMN]

                    .dropna()

                    .nunique()

                )

    except Exception as e:

        st.error(

            f"Could not read the uploaded file: {e}"

        )

        st.stop()

# ============================================================

# DOWNLOAD SECTION

# ============================================================

if df is not None and tasks:

    st.header("Download Audio")

    st.write(

        """

        Each row can contain up to **three audio links**.

        The downloader will download every available link.

        """

    )

    # --------------------------------------------------------

    # EXAMPLE

    # --------------------------------------------------------

    

    # --------------------------------------------------------

    # START DOWNLOAD

    # --------------------------------------------------------

    start_download = st.button(

        "Start Download",

        type="primary",

        use_container_width=True

    )

    if start_download:

        if not username.strip():

            st.error(

                "Please enter your CommCare username."

            )

            st.stop()

        if not api_key.strip():

            st.error(

                "Please enter your CommCare API key."

            )

            st.stop()

        if not commcare_domain.strip():

            st.error(

                "Please enter your CommCare domain."

            )

            st.stop()

        # ----------------------------------------------------

        # RESET CURRENT FAILED LIST

        # ----------------------------------------------------

        st.session_state.failed_downloads = []

        st.session_state.download_started = True

        st.session_state.processed_count = 0

        st.session_state.total_audio_count = len(tasks)

        # ----------------------------------------------------

        # PROGRESS

        # ----------------------------------------------------

        progress_bar = st.progress(0)

        status_text = st.empty()

        success_text = st.empty()

        failed_text = st.empty()

        # ----------------------------------------------------

        # DOWNLOAD IN PARALLEL

        # ----------------------------------------------------

        completed_this_run = 0

        failed_this_run = 0

        with ThreadPoolExecutor(

            max_workers=int(workers)

        ) as executor:

            future_to_task = {}

            for task in tasks:

                future = executor.submit(

                    download_audio,

                    task,

                    username.strip(),

                    api_key.strip(),

                    int(timeout),

                    int(retries),
                    st.session_state.workspace_dir,
                )

                future_to_task[future] = task

            for future in as_completed(

                future_to_task

            ):

                task = future_to_task[future]

                try:

                    result = future.result()

                except Exception as e:

                    result = {

                        "success": False,

                        "filename": task["filename"],

                        "content": None,

                        "error": str(e)

                    }

                # --------------------------------------------

                # SUCCESS

                # --------------------------------------------

                if result["success"]:

                    filename = result["filename"]

                    # Make filename unique if necessary

                    filename = make_unique_filename(

                        filename,

                        st.session_state.completed_files

                    )

                    # Move the completed temporary download to its final unique name.
                    final_path = os.path.join(st.session_state.workspace_dir, filename)
                    if os.path.abspath(result["path"]) != os.path.abspath(final_path):
                        os.replace(result["path"], final_path)
                    st.session_state.completed_files[filename] = final_path

                    completed_this_run += 1

                # --------------------------------------------

                # FAILURE

                # --------------------------------------------

                else:

                    failed_this_run += 1

                    st.session_state.failed_downloads.append(

                        {

                            "row_number": task["row_number"],

                            "audio_name": task["audio_name"],

                            "link_column": task["link_column"],

                            "url": task["url"],

                            "filename": task["filename"],

                            "error": result["error"]

                        }

                    )

                # --------------------------------------------

                # UPDATE PROGRESS

                # --------------------------------------------

                st.session_state.processed_count += 1

                processed = (

                    st.session_state.processed_count

                )

                total = (

                    st.session_state.total_audio_count

                )

                progress = (

                    processed / total

                    if total > 0

                    else 0

                )

                progress_bar.progress(

                    min(progress, 1.0)

                )

                status_text.info(

                    f"Processing {processed} "

                    f"of {total} audio files..."

                )

                success_text.success(

                    f"✅ Completed: "

                    f"{len(st.session_state.completed_files)}"

                )

                if st.session_state.failed_downloads:

                    failed_text.warning(

                        f"⚠️ Failed: "

                        f"{len(st.session_state.failed_downloads)}"

                    )

        # ----------------------------------------------------

        # FINAL RESULT

        # ----------------------------------------------------

        st.success(

            "🎉 Download process finished."

        )

        col1, col2, col3 = st.columns(3)

        with col1:

            st.metric(

                "✅ Completed",

                len(st.session_state.completed_files)

            )

        with col2:

            st.metric(

                "❌ Failed",

                len(st.session_state.failed_downloads)

            )

        with col3:

            st.metric(

                "Audio files saved",

                len(st.session_state.completed_files)

            )

# ============================================================

# COMPLETED FILES / ZIP

# ============================================================

if st.session_state.completed_files:

    st.header("Download Completed Files")

    st.success(

        f"""

        You currently have

        **{len(st.session_state.completed_files)}**

        successfully downloaded audio files.

        These files are retained in the app session even if

        another download fails.

        """

    )

    # --------------------------------------------------------

    # CREATE ZIP

    # --------------------------------------------------------

    zip_path = create_zip_from_completed_files()
    if zip_path and os.path.isfile(zip_path):
        zip_size_mb = os.path.getsize(zip_path) / (1024 * 1024)
        st.caption(f"ZIP size: {zip_size_mb:.1f} MB")
        # The archive is reused on reruns instead of being rebuilt on every click.
        # Streamlit needs downloadable data in memory, but the archive is built
        # from disk files; the individual recordings are never all held in RAM.
        with open(zip_path, "rb") as zip_file:
            zip_data = zip_file.read()

        st.download_button(
            label=(
                f"⬇ Download ZIP "
                f"({len(st.session_state.completed_files)} files)"
            ),
            data=zip_data,
            file_name="commcare_completed_audio.zip",
            mime="application/zip",
            use_container_width=True,
            key="download_final_audio_zip"
        )



    # --------------------------------------------------------

    # SHOW FILE LIST

    # --------------------------------------------------------

    with st.expander("View completed files"):

        completed_df = pd.DataFrame(

            {

                "File name": list(

                    st.session_state.completed_files.keys()

                )

            }

        )

        st.dataframe(

            completed_df,

            use_container_width=True,

            hide_index=True

        )

# ============================================================

# FAILED DOWNLOADS

# ============================================================

if st.session_state.failed_downloads:

    st.header("Failed Downloads")

    st.warning(

        f"""

        {len(st.session_state.failed_downloads)}

        audio file(s) could not be downloaded.

        Your successfully downloaded files have NOT been lost.

        """

    )

    failed_df = pd.DataFrame(

        st.session_state.failed_downloads

    )

    st.dataframe(

        failed_df,

        use_container_width=True,

        hide_index=True

    )

    # --------------------------------------------------------

    # FAILED CSV

    # --------------------------------------------------------

    failed_csv = create_failed_csv()

    if failed_csv:

        st.download_button(

            label="⬇ Download Failed CSV",

            data=failed_csv,

            file_name="commcare_failed_downloads.csv",

            mime="text/csv",

            use_container_width=True

        )

# ============================================================

# INSTRUCTIONS

# ============================================================

# ============================================================

# FOOTER

# ============================================================

st.divider()

st.caption(

    "CommCare Audio Downloader — "

    "Download, rename and package CommCare recordings."

)