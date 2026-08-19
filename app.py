import io
import re
import threading
from datetime import datetime

import streamlit as st
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request


# =========================================================
# Configuration
# =========================================================
HINTS = [

    "Say the word naturally and clearly.",

    "Say it quickly",

    "Say it a little slowly",

    "Say it as if you're asking a question or calling someone.",

    "Say it in a slightly quieter voice.",

    "Say it in a slightly louder voice.",

    "Say it as you would naturally say it in the middle of a sentence.",

]

RECORDINGS_PER_HINT = 3
TARGET_RECORDINGS = 100


def ar_num(n):
    return str(n)


# =========================================================
# Google Drive
# =========================================================

# Serializes folder lookup/creation across all concurrent users so two
# people registering the same speaker name at the same moment can't both
# decide "it doesn't exist yet" and create duplicate folders.
_folder_lock = threading.Lock()

# Serializes credential refreshes so concurrent sessions never call
# creds.refresh() on the same object at the same time.
_creds_lock = threading.Lock()


@st.cache_resource(show_spinner=False)
def get_drive_credentials():
    creds = Credentials(
        token=None,
        refresh_token=st.secrets["oauth_refresh_token"],
        client_id=st.secrets["oauth_client_id"],
        client_secret=st.secrets["oauth_client_secret"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/drive"],
    )

    creds.refresh(Request())

    return creds


def get_drive_service():
    # The Credentials object itself is cached/shared across every user's
    # session (that part is fine, it's just a token holder). What must
    # NOT be shared across threads is the underlying http/service object
    # from googleapiclient, since httplib2.Http is documented as not
    # thread-safe. So we build a brand-new service instance per call,
    # using the shared, refreshed credentials.
    creds = get_drive_credentials()

    if not creds.valid:
        with _creds_lock:
            if not creds.valid:
                creds.refresh(Request())

    return build(
        "drive",
        "v3",
        credentials=creds,
        cache_discovery=False,
    )


def resolve_speaker_folder(
    service,
    parent_folder_id,
    speaker_name,
    create_if_missing=False,
):
    target = speaker_name.strip().casefold()
    page_token = None

    while True:
        resp = (
            service.files()
            .list(
                q=(
                    f"'{parent_folder_id}' in parents and "
                    "mimeType = 'application/vnd.google-apps.folder' "
                    "and trashed = false"
                ),
                fields="nextPageToken, files(id, name)",
                pageToken=page_token,
                pageSize=1000,
            )
            .execute()
        )

        for folder in resp.get("files", []):
            if folder["name"].strip().casefold() == target:
                return folder["id"]

        page_token = resp.get("nextPageToken")

        if not page_token:
            break

    if create_if_missing:
        metadata = {
            "name": speaker_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_folder_id],
        }

        folder = (
            service.files()
            .create(
                body=metadata,
                fields="id",
            )
            .execute()
        )

        return folder["id"]

    return None


def find_or_create_subfolder(service, parent_folder_id, name):
    # Only one session at a time is allowed to check "does this speaker's
    # folder exist?" and, if not, create it. Without this lock, two people
    # registering the same name in the same instant could both see "not
    # found" and each create their own folder, splitting that speaker's
    # recordings across two duplicate folders.
    with _folder_lock:
        return resolve_speaker_folder(
            service,
            parent_folder_id,
            name,
            create_if_missing=True,
        )


def upload_audio(
    service,
    folder_id,
    filename,
    audio_bytes,
    mimetype,
):
    media = MediaIoBaseUpload(
        io.BytesIO(audio_bytes),
        mimetype=mimetype,
        resumable=False,
    )

    metadata = {
        "name": filename,
        "parents": [folder_id],
    }

    return (
        service.files()
        .create(
            body=metadata,
            media_body=media,
            fields="id, webViewLink",
        )
        .execute()
    )


def count_existing_recordings(
    service,
    parent_folder_id,
    speaker_name,
):
    speaker_folder_id = resolve_speaker_folder(
        service,
        parent_folder_id,
        speaker_name,
        create_if_missing=False,
    )

    if not speaker_folder_id:
        return 0

    count = 0
    page_token = None

    while True:
        resp = (
            service.files()
            .list(
                q=(
                    f"'{speaker_folder_id}' in parents "
                    "and trashed = false"
                ),
                fields="nextPageToken, files(id)",
                pageToken=page_token,
                pageSize=1000,
            )
            .execute()
        )

        count += len(resp.get("files", []))

        page_token = resp.get("nextPageToken")

        if not page_token:
            break

    return count


# =========================================================
# Helpers
# =========================================================

def sanitize_id(raw):
    cleaned = re.sub(
        r"[^\w\-]+",
        "_",
        raw.strip(),
        flags=re.UNICODE,
    )

    return cleaned or "speaker"


def pick_hint(uploaded_count):
    idx = (
        uploaded_count // RECORDINGS_PER_HINT
    ) % len(HINTS)

    return HINTS[idx]


# =========================================================
# Page Configuration
# =========================================================

st.set_page_config(
    page_title="Muqla — Voice Recording",
    page_icon="🎙️",
    layout="centered",
)


# =========================================================
# Styling (calmer, muted palette)
# =========================================================

st.markdown(
    """
    <style>

    @import url(
        'https://fonts.googleapis.com/css2?family=Tajawal:wght@400;500;600;700;800;900&display=swap'
    );

    html,
    body,
    [class*="css"] {
        font-family: 'Tajawal', sans-serif !important;
        font-size: 16px;
    }

    .stApp {
        background:
            radial-gradient(
                circle at 10% 5%,
                rgba(159, 138, 122, 0.08),
                transparent 35%
            ),
            radial-gradient(
                circle at 90% 90%,
                rgba(111, 156, 150, 0.08),
                transparent 38%
            ),
            #12151C;
    }

    .main .block-container {
        max-width: 760px;
        padding-top: 3rem;
        padding-bottom: 3rem;
    }


    /* =====================================================
       HEADER
       ===================================================== */

    .app-header {
        text-align: center;
        margin-bottom: 6px;
    }

    .app-header h1 {
        font-size: 32px;
        font-weight: 900;
        margin: 0;

        background:
            linear-gradient(
                90deg,
                #C99479,
                #D9B77E,
                #6FA69A
            );

        -webkit-background-clip: text;
        background-clip: text;
        color: transparent;
    }

    .app-subtitle {
        text-align: center;
        color: #93A0AF;
        font-size: 15px;
        line-height: 1.6;
        margin-bottom: 24px;
    }


    /* =====================================================
       GOAL CARD
       ===================================================== */

    .goal-pill {
        width: 100%;
        box-sizing: border-box;

        padding: 12px 18px;
        margin-bottom: 24px;

        background: rgba(111, 166, 154, 0.07);

        border: 1px solid rgba(111, 166, 154, 0.20);

        border-radius: 12px;

        color: #9FC5BB;

        font-size: 14px;
        font-weight: 500;

        text-align: center;
        line-height: 1.5;
    }


    /* =====================================================
       TEXT INPUT
       ===================================================== */

    div[data-testid="stTextInput"] label {
        color: #C7CFDA !important;
        font-weight: 600 !important;
    }

    div[data-testid="stTextInput"] input {
        height: 48px;

        background: rgba(255, 255, 255, 0.04) !important;

        color: #EDF0F4 !important;

        border:
            1px solid rgba(255, 255, 255, 0.10) !important;

        border-radius: 11px !important;

        font-family:
            'Tajawal',
            sans-serif !important;

        font-size: 16px !important;
    }


    /* =====================================================
       WORD CARD
       ===================================================== */

    .prompt-box {
        background:
            radial-gradient(
                circle at 50% 0%,
                rgba(201, 148, 121, 0.10),
                transparent 58%
            ),
            linear-gradient(
                145deg,
                #201C24,
                #17151C
            );

        border:
            1px solid rgba(201, 148, 121, 0.22);

        border-radius: 20px;

        padding: 42px 24px;

        margin: 26px 0 20px 0;

        text-align: center;

        box-shadow:
            0 14px 45px rgba(0, 0, 0, 0.20);
    }

    .prompt-word {
        font-size: 60px;
        font-weight: 900;

        color: #F1ECE6;

        line-height: 1.2;

        text-shadow:
            0 0 24px rgba(201, 148, 121, 0.20);
    }

    .prompt-line {
        width: 42px;
        height: 4px;

        margin: 18px auto 0;

        background:
            linear-gradient(
                90deg,
                #C99479,
                #D9B77E
            );

        border-radius: 999px;
    }


    /* =====================================================
       AUDIO INPUT
       ===================================================== */

    div[data-testid="stAudioInput"] {
        background:
            linear-gradient(
                145deg,
                rgba(201, 148, 121, 0.05),
                rgba(111, 166, 154, 0.04)
            );

        border:
            1px solid rgba(201, 148, 121, 0.22);

        border-radius: 16px;

        padding: 14px;

        margin: 0;

        min-height: 82px;

        box-sizing: border-box;
    }


    /* =====================================================
       BUTTONS
       ===================================================== */

    .stButton,
    div[data-testid="stFormSubmitButton"] {
        width: 100%;
    }

    .stButton > button,
    div[data-testid="stFormSubmitButton"] > button {
        width: 100% !important;
        min-width: 100% !important;

        height: 50px !important;
        min-height: 50px !important;

        padding: 0 14px !important;

        border-radius: 11px !important;

        font-family:
            'Tajawal',
            sans-serif !important;

        font-size: 15px !important;
        font-weight: 700 !important;
    }


    /* Start Recording button — calmer green, scoped to its own wrapper
       so the Send button (also kind="primary") keeps its own color.
       Note: st.form_submit_button does NOT use kind="primary" like a
       normal st.button, so we target it by testid only. */

    .st-key-start_form_wrap div[data-testid="stFormSubmitButton"] button {
        background:
            linear-gradient(
                90deg,
                #6E9C6C,
                #8CB88A
            ) !important;

        color: #142014 !important;

        border:
            1px solid #7CAA7A !important;

        box-shadow:
            0 6px 16px
            rgba(110, 156, 108, 0.18) !important;
    }

    .st-key-start_form_wrap div[data-testid="stFormSubmitButton"] button:hover {
        background:
            linear-gradient(
                90deg,
                #7CAA7A,
                #9BC498
            ) !important;
    }


    /* Primary */

    .stButton > button[kind="primary"],
    div[data-testid="stFormSubmitButton"] > button[kind="primary"] {
        background:
            linear-gradient(
                90deg,
                #C08865,
                #CDA26F
            ) !important;

        color: #201812 !important;

        border:
            1px solid #C6926B !important;

        box-shadow:
            0 6px 16px
            rgba(192, 136, 101, 0.18);
    }

    .stButton > button[kind="primary"]:hover,
    div[data-testid="stFormSubmitButton"] > button[kind="primary"]:hover {
        background:
            linear-gradient(
                90deg,
                #C6926B,
                #D3AC7A
            ) !important;
    }

    .stButton > button[kind="primary"]:disabled,
    div[data-testid="stFormSubmitButton"] > button[kind="primary"]:disabled {
        background:
            rgba(255, 255, 255, 0.06) !important;

        color: #626C7A !important;

        border-color:
            rgba(255, 255, 255, 0.07) !important;

        box-shadow: none !important;
    }


    /* Secondary */

    .stButton > button[kind="secondary"],
    div[data-testid="stFormSubmitButton"] > button[kind="secondary"] {
        background:
            rgba(111, 166, 154, 0.06) !important;

        color: #9FC5BB !important;

        border:
            1px solid rgba(111, 166, 154, 0.30) !important;
    }

    .stButton > button[kind="secondary"]:hover,
    div[data-testid="stFormSubmitButton"] > button[kind="secondary"]:hover {
        background:
            rgba(111, 166, 154, 0.11) !important;
    }


    /* =====================================================
       SEND BUTTON — MATCH RECORDER HEIGHT
       ===================================================== */

    .st-key-send_btn_wrap {
        height: 100%;
    }

    .st-key-send_btn_wrap .stButton {
        height: 100%;
    }

    .st-key-send_btn_wrap .stButton > button {
        height: 82px !important;
        min-height: 82px !important;
    }


    /* =====================================================
       DONE SCREEN
       ===================================================== */

    .done-card {
        background:
            radial-gradient(
                circle at 50% 0%,
                rgba(111, 166, 154, 0.07),
                transparent 55%
            ),
            linear-gradient(
                145deg,
                #16221E,
                #101917
            );

        border:
            1px solid rgba(111, 166, 154, 0.26);

        border-radius: 20px;

        padding: 42px 24px;

        text-align: center;

        box-shadow:
            0 14px 45px rgba(0, 0, 0, 0.18);
    }


    /* =====================================================
       STREAMLIT MARKDOWN TEXT
       ===================================================== */

    .done-card-text {
        color: #C6D6CE !important;
        text-align: center !important;
        font-size: 15px !important;
        line-height: 1.7 !important;
    }

    .done-card-title {
        color: #8FC2B3 !important;
        text-align: center !important;
        font-size: 27px !important;
        font-weight: 800 !important;
        margin-bottom: 8px !important;
    }


    /* =====================================================
       MOBILE
       ===================================================== */

    @media (max-width: 640px) {

        .main .block-container {
            padding:
                1.5rem 1rem 2rem 1rem;
        }

        .app-header h1 {
            font-size: 26px;
        }

        .prompt-word {
            font-size: 50px;
        }

        .prompt-box {
            padding: 34px 18px;
        }

        .stButton > button,
        div[data-testid="stFormSubmitButton"] > button {
            height: 48px !important;
            min-height: 48px !important;
            font-size: 14px !important;
        }

        .st-key-send_btn_wrap .stButton > button {
            height: 82px !important;
            min-height: 82px !important;
        }
    }

    </style>
    """,
    unsafe_allow_html=True,
)


# =========================================================
# Session State
# =========================================================

if "speaker_id" not in st.session_state:
    st.session_state.speaker_id = None

if "uploaded_count" not in st.session_state:
    st.session_state.uploaded_count = 0

if "rec_key" not in st.session_state:
    st.session_state.rec_key = 0

if "finished" not in st.session_state:
    st.session_state.finished = False

if "confirm_finish" not in st.session_state:
    st.session_state.confirm_finish = False


# =========================================================
# Header
# =========================================================

st.markdown(
    """
    <div class="app-header">
        <h1>Muqla — Voice Sample Recording</h1>
    </div>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div class="app-subtitle">
        Record the word "مقلة" in different ways
        and send each recording as soon as you finish.
    </div>
    """,
    unsafe_allow_html=True,
)


# =========================================================
# Drive Folder
# =========================================================

FOLDER_ID = st.secrets.get("drive_folder_id")

if not FOLDER_ID:
    st.error(
        "The setup is incomplete. Please contact the team."
    )
    st.stop()


# =========================================================
# Speaker Registration
# =========================================================

if st.session_state.speaker_id is None:

    st.markdown(
        f"""
        <div class="goal-pill">
            Our goal is to collect at least
            {ar_num(TARGET_RECORDINGS)} recordings from you.
            You can stop earlier if you'd like.
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Wrapping the name field + button in a form makes pressing
    # Enter submit the form directly (no need to click with the mouse).
    with st.container(key="start_form_wrap"):

        with st.form(key="speaker_form", clear_on_submit=False):

            name = st.text_input(
                "Enter your name or a nickname",
                placeholder="e.g. Salma",
            )

            start_clicked = st.form_submit_button(
                "Start Recording",
                type="primary",
                use_container_width=True,
            )

    if start_clicked:

        if not name.strip():

            st.warning(
                "Please enter your name or a nickname first."
            )

        else:

            speaker_id = sanitize_id(name)

            try:

                with st.spinner(
                    "Checking for previous recordings..."
                ):

                    service = get_drive_service()

                    existing_count = count_existing_recordings(
                        service,
                        FOLDER_ID,
                        speaker_id,
                    )

            except Exception as e:

                st.error(
                    "Something went wrong while checking your "
                    "previous recordings. Please try again in a moment."
                )

                st.exception(e)

                st.stop()

            st.session_state.speaker_id = speaker_id
            st.session_state.uploaded_count = existing_count
            st.session_state.rec_key = 0
            st.session_state.last_hint_shown = None
            st.session_state.finished = False
            st.session_state.confirm_finish = False

            if existing_count > 0:

                st.toast(
                    f"Found {ar_num(existing_count)} previous recordings. "
                    "Continuing from where you left off.",
                    icon="👋",
                )

            st.rerun()


# =========================================================
# Recording Screen
# =========================================================

elif not st.session_state.get("finished"):

    hint = pick_hint(
        st.session_state.uploaded_count
    )

    count = st.session_state.uploaded_count


    # -----------------------------------------------------
    # Hint
    # -----------------------------------------------------

    if (
        st.session_state.get("last_hint_shown")
        != hint
    ):

        st.session_state.last_hint_shown = hint

        st.toast(
            hint,
            icon="🎙️",
        )


    # -----------------------------------------------------
    # Progress
    # -----------------------------------------------------

    progress = min(
        count / TARGET_RECORDINGS,
        1.0,
    )

    progress_col_1, progress_col_2 = st.columns(
        [3, 1],
        gap="small",
    )

    with progress_col_1:

        st.markdown(
            f"""
            <div style="
                color:#AAB4C0;
                font-size:14px;
                margin-bottom:8px;
            ">
                <strong style="color:#C99479;">
                    {ar_num(count)}
                </strong>
                recordings uploaded
            </div>
            """,
            unsafe_allow_html=True,
        )

    with progress_col_2:

        st.markdown(
            f"""
            <div style="
                color:#707B8A;
                font-size:14px;
                text-align:right;
                margin-bottom:8px;
            ">
                Goal: {ar_num(TARGET_RECORDINGS)}
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.progress(progress)


    if count < TARGET_RECORDINGS:

        st.caption(
            "You can finish at any time and continue later."
        )

    else:

        st.caption(
            "🎉 You've reached the goal! "
            "You can keep recording or finish now."
        )


    # -----------------------------------------------------
    # Word
    # -----------------------------------------------------

    st.markdown(
        """
        <div class="prompt-box">
            <div class="prompt-word">مقلة</div>
            <div class="prompt-line"></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


    # -----------------------------------------------------
    # Recorder + Send
    # -----------------------------------------------------

    record_col, send_col = st.columns(
        [4, 1.25],
        gap="medium",
        vertical_alignment="center",
    )

    with record_col:

        audio = st.audio_input(
            "Record here",
            key=f"rec_{st.session_state.rec_key}",
            label_visibility="collapsed",
        )

    with send_col:

        with st.container(key="send_btn_wrap"):

            send_clicked = st.button(
                "📤  Send",
                type="primary",
                disabled=audio is None,
                use_container_width=True,
            )


    # -----------------------------------------------------
    # Finish
    # -----------------------------------------------------

    if st.session_state.confirm_finish:

        st.warning(
        "You haven't made any recordings in this session yet."
        "Click Finish again if you'd like to exit without adding any new recordings."
        )

    finish_clicked = st.button(
        "Finish",
        type="secondary",
        use_container_width=True,
    )


    # =====================================================
    # Upload
    # =====================================================

    if send_clicked and audio is not None:

        try:

            with st.spinner(
                "Uploading your recording..."
            ):

                service = get_drive_service()

                speaker_folder_id = (
                    find_or_create_subfolder(
                        service,
                        FOLDER_ID,
                        st.session_state.speaker_id,
                    )
                )

                timestamp = datetime.now().strftime(
                    "%H%M%S%f"
                )

                ext = (
                    audio.type.split("/")[-1]
                    if audio.type
                    else "wav"
                )

                filename = (
                    f"{st.session_state.uploaded_count + 1:03d}"
                    f"_مقلة_{timestamp}.{ext}"
                )

                upload_audio(
                    service,
                    speaker_folder_id,
                    filename,
                    audio.getvalue(),
                    audio.type,
                )


            st.session_state.uploaded_count += 1
            st.session_state.rec_key += 1
            st.session_state.confirm_finish = False

            new_count = (
                st.session_state.uploaded_count
            )

            if new_count == TARGET_RECORDINGS:

                st.toast(
                    f"🎉 You've reached "
                    f"{ar_num(TARGET_RECORDINGS)} recordings! "
                    "You can keep going or finish now.",
                    icon="🏁",
                )

            else:

                st.toast(
                    "Recording uploaded successfully.",
                    icon="✅",
                )

            st.rerun()


        except Exception as e:

            st.error(
                "Something went wrong while uploading. "
                "Please try again."
            )

            st.exception(e)


    # =====================================================
    # Finish
    # =====================================================

    if finish_clicked:

        if (
            st.session_state.uploaded_count == 0
            and not st.session_state.confirm_finish
        ):

            st.session_state.confirm_finish = True
            st.rerun()

        else:

            st.session_state.finished = True
            st.session_state.confirm_finish = False
            st.rerun()

# =========================================================
# Finished Screen
# =========================================================

if st.session_state.get("finished"):

    st.markdown(
        """
        <style>
        .done-wrapper {
            background:
                linear-gradient(
                    145deg,
                    #16221E,
                    #101917
                );

            border: 1px solid rgba(111, 166, 154, 0.26);
            border-radius: 20px;

            padding: 36px 24px;
            margin: 10px 0 20px 0;

            text-align: center;

            box-shadow:
                0 14px 45px rgba(0, 0, 0, 0.18);
        }

        .done-title {
            color: #8FC2B3;
            font-size: 27px;
            font-weight: 800;
            margin-bottom: 12px;
        }

        .done-text {
            color: #C6D6CE;
            font-size: 15px;
            line-height: 1.7;
        }

        .done-number {
            color: #F1ECE6;
            font-weight: 800;
        }

        .done-speaker {
            color: #9FC5BB;
            font-weight: 700;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # =====================================================
    # Done Card
    # =====================================================

    st.markdown(
        f"""
        <div class="done-wrapper">
            <div class="done-title">
                All done!
            </div>
                <div class="done-text">
                    {ar_num(st.session_state.uploaded_count)}
                    audio files were uploaded successfully
                    under the name
                    {st.session_state.speaker_id}.
                </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # =====================================================
    # New Recording
    # =====================================================

    if st.button(
        "Record again!",
        type="secondary",
        use_container_width=True,
    ):

        st.session_state.speaker_id = None
        st.session_state.uploaded_count = 0
        st.session_state.rec_key = 0
        st.session_state.finished = False
        st.session_state.confirm_finish = False
        st.session_state.last_hint_shown = None

        st.rerun()