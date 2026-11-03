"""CommCare audio workspace. Run: streamlit run app.py"""
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urljoin

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Audio Workspace | CommCare", page_icon="🎧", layout="wide")
st.markdown('''<style>
.block-container {max-width:1450px;padding-top:2.4rem;padding-bottom:3rem}
h1,h2,h3 {letter-spacing:-.035em;color:#14243b}
h1 {font-size:2.5rem!important;font-weight:750!important}
[data-testid="stSidebar"] {background:#f0f4f9;border-right:1px solid #dfe7f0}
[data-testid="stMetric"] {background:white;border:1px solid #dfe7f0;border-radius:14px;padding:18px 22px;box-shadow:0 3px 12px #14243b05}
[data-testid="stMetricLabel"] {color:#62748b;font-size:.85rem}
[data-testid="stMetricValue"] {color:#14243b;font-weight:700}
.stButton button,.stDownloadButton button {border-radius:9px;min-height:42px;font-weight:600}
[data-testid="stFileUploader"] {border-radius:12px}
.eyebrow {font-size:11px;font-weight:800;letter-spacing:2px;color:#2563eb;margin-bottom:8px}
.hero-copy {color:#64748b;font-size:1.05rem;max-width:780px;margin-bottom:24px}
.sidebar-brand {font-size:22px;font-weight:750;color:#14243b;margin:0 0 4px}
.sidebar-copy {color:#64748b;font-size:13px;margin-bottom:24px}
.step {background:#eaf1fd;color:#2457a7;padding:10px 16px;border-radius:8px;font-size:13px;margin-bottom:18px}
</style>''', unsafe_allow_html=True)

S = st.session_state
for key, value in dict(auth=None, records={}, history=[], workspace=None, zip_path=None,
                       zip_signature=None, upload_signature=None).items():
    if key not in S:
        S[key] = value
if not S.workspace or not Path(S.workspace).is_dir():
    S.workspace = tempfile.mkdtemp(prefix="commcare_workspace_")

EXTENSIONS = {'.mp3','.m4a','.wav','.aac','.ogg','.amr','.3gp','.webm','.mp4','.opus'}
RETRY_CODES = {408,429,500,502,503,504}


def clean_name(value):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value).strip())
    name = re.sub(r'\s+', ' ', name).strip('. ')[:130] or 'audio'
    if name.split('.')[0].upper() in {'CON','PRN','AUX','NUL', *[f'COM{i}' for i in range(1,10)], *[f'LPT{i}' for i in range(1,10)]}:
        name = '_' + name
    return name


def valid_url(value, host):
    if pd.isna(value) or not str(value).strip():
        return None, None
    value = str(value).strip()
    if value.lower() in {'nan','none','null','---'}:
        return None, None
    try:
        p = urlparse(value)
        if p.scheme != 'https' or p.hostname != host or p.username or p.password or p.port not in (None,443):
            return None, 'Must be HTTPS on the selected CommCare server'
    except ValueError:
        return None, 'Malformed URL'
    return value, None


def reset_files():
    shutil.rmtree(S.workspace, ignore_errors=True)
    S.workspace = tempfile.mkdtemp(prefix='commcare_workspace_')
    S.records = {}
    S.zip_path = None
    S.zip_signature = None


def logout():
    reset_files()
    S.auth = None
    S.history = []
    S.upload_signature = None
    for key in ('login_user','login_key','login_domain'):
        S.pop(key, None)


def csv_bytes(frame):
    # Neutralize spreadsheet formulas in exported text cells.
    safe = frame.copy()
    for col in safe.columns:
        safe[col] = safe[col].map(lambda v: "'" + v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v)
    return safe.to_csv(index=False).encode('utf-8-sig')


def fetch(task, auth, read_timeout, attempts, max_mb):
    """No Streamlit calls in worker threads; credentials never go to another host."""
    last_error = 'Download failed'
    for attempt in range(attempts):
        path = None
        try:
            url = task['url']
            with requests.Session() as session:
                for redirect in range(6):
                    checked, error = valid_url(url, auth['host'])
                    if error or not checked:
                        raise ValueError('Blocked redirect outside the selected CommCare server')
                    response = session.get(url, headers={
                        'Authorization': f"ApiKey {auth['username']}:{auth['key']}",
                        'User-Agent':'CommCare-Audio-Workspace/1.0'},
                        timeout=(15, read_timeout), stream=True, allow_redirects=False)
                    if response.status_code in (301,302,303,307,308):
                        location = response.headers.get('Location')
                        response.close()
                        if not location:
                            raise ValueError('Redirect without a destination')
                        url = urljoin(url, location)
                        continue
                    break
                else:
                    raise ValueError('Too many redirects')
                with response:
                    if response.status_code in RETRY_CODES:
                        last_error = f'HTTP {response.status_code}; retry budget exhausted'
                        if attempt + 1 < attempts:
                            wait = response.headers.get('Retry-After','')
                            time.sleep(min(int(wait),60) if wait.isdigit() else min(2 ** (attempt+1),15))
                            continue
                        return dict(ok=False,error=last_error)
                    if response.status_code != 200:
                        messages = {401:'Unauthorized: verify credentials',403:'Permission denied',404:'Recording not found'}
                        return dict(ok=False,error=messages.get(response.status_code,f'HTTP {response.status_code}'))
                    content_type = response.headers.get('Content-Type','').lower()
                    if any(t in content_type for t in ('text/html','application/json','text/xml','application/xml')):
                        raise ValueError('Server returned a page or API response instead of audio')
                    limit = int(max_mb * 1024 * 1024)
                    length = response.headers.get('Content-Length','')
                    if length.isdigit() and int(length) > limit:
                        raise ValueError('Recording exceeds the configured size limit')
                    fd, path = tempfile.mkstemp(prefix='part_',dir=task['workspace'])
                    total = 0
                    with os.fdopen(fd,'wb') as f:
                        for chunk in response.iter_content(256 * 1024):
                            if chunk:
                                total += len(chunk)
                                if total > limit:
                                    raise ValueError('Recording exceeds the configured size limit')
                                f.write(chunk)
                    if total == 0:
                        raise ValueError('Empty recording')
                    final = str(Path(task['workspace']) / task['filename'])
                    os.replace(path,final)
                    return dict(ok=True,path=final,size=total,error='')
        except (requests.RequestException,OSError,ValueError) as exc:
            # Do not expose response URLs, query parameters or credentials in errors.
            last_error = str(exc) if isinstance(exc,ValueError) else type(exc).__name__ + ': connection or file operation failed'
            if path:
                Path(path).unlink(missing_ok=True)
            if isinstance(exc,ValueError):
                break
            if attempt + 1 < attempts:
                time.sleep(min(2 ** (attempt+1),15))
    return dict(ok=False,error=last_error)


def run_downloads(queue, workers, timeout, attempts, max_mb):
    started = time.monotonic()
    progress = st.progress(0, text='Preparing download queue…')
    live = st.empty()
    success = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for row in queue:
            task = {**row, 'workspace':S.workspace}
            futures[pool.submit(fetch, task, dict(S.auth), timeout, attempts, max_mb)] = row
        for count, future in enumerate(as_completed(futures),1):
            row = futures[future]
            try:
                result = future.result()
            except Exception:
                result = dict(ok=False,error='Unexpected worker failure')
            S.records[row['id']] = {**row, **result, 'status':'Completed' if result['ok'] else 'Failed',
                                     'updated':datetime.now(timezone.utc).isoformat(timespec='seconds')}
            success += int(result['ok'])
            progress.progress(count / len(queue),text=f'Processed {count:,} of {len(queue):,} recordings')
            live.caption(f'{success:,} completed · {count-success:,} failed · {time.monotonic()-started:.0f}s elapsed')
    S.history.append({'UTC time':datetime.now(timezone.utc).isoformat(timespec='seconds'),
                      'Attempted':len(queue),'Completed':success,'Failed':len(queue)-success,
                      'Seconds':round(time.monotonic()-started,1)})
    S.zip_signature = None
    st.success(f'Run finished — {success:,} completed, {len(queue)-success:,} failed.')


with st.sidebar:
    st.markdown('<div class="sidebar-brand">🎧 Audio Workspace</div><div class="sidebar-copy">COMMCARE OPERATIONS</div>',unsafe_allow_html=True)
    if not S.auth:
        st.subheader('Connect your account')
        with st.form('auth_form'):
            host = st.selectbox('CommCare server',['www.commcarehq.org','india.commcarehq.org','eu.commcarehq.org'])
            domain = st.text_input('Project domain',key='login_domain',placeholder='your-project')
            username = st.text_input('Username / email',key='login_user')
            key = st.text_input('API key',type='password',key='login_key')
            submitted = st.form_submit_button('Connect securely',type='primary',width='stretch')
        if submitted:
            if not re.fullmatch(r'[a-zA-Z0-9_-]+',domain.strip()) or not username.strip() or not key.strip():
                st.error('Provide a valid project domain, username and API key.')
            else:
                try:
                    with requests.get(f'https://{host}/a/{domain.strip()}/api/v0.5/case/',
                          params={'limit':1},headers={'Authorization':f'ApiKey {username.strip()}:{key.strip()}'},
                          timeout=(10,20),allow_redirects=False) as response:
                        if response.status_code == 200:
                            S.auth = dict(host=host,domain=domain.strip(),username=username.strip(),key=key.strip())
                            st.rerun()
                        else:
                            st.error(f'Connection not verified (HTTP {response.status_code}). Check your server, project and case API permissions.')
                except requests.RequestException:
                    st.error('Unable to reach CommCare. Check your connection and server.')
    else:
        st.success('Connected to CommCare')
        st.caption(S.auth['username'])
        st.caption(f"Project: {S.auth['domain']}")
        st.button('Log out & delete session files',on_click=logout,width='stretch')
    st.divider()
    st.subheader('Transfer settings')
    workers = st.slider('Parallel downloads',1,10,3)
    timeout = st.number_input('Read inactivity timeout (seconds)',10,600,60,10,
                              help='Maximum wait between received data, not a total file deadline.')
    attempts = st.number_input('Maximum attempts per file',1,10,3)
    max_mb = st.number_input('Maximum recording size (MB)',1,2048,250)
    st.caption('HTTPS only. Audio links and redirects must stay on the selected CommCare host.')
    st.divider()
    confirm = st.checkbox('Confirm deletion of session downloads')
    if st.button('Clear downloads',disabled=not confirm,width='stretch'):
        reset_files()
        st.rerun()

st.markdown('<div class="eyebrow">COMMCARE / AUDIO OPERATIONS</div>',unsafe_allow_html=True)
st.title('Your recordings. Organized.')
st.markdown('<div class="hero-copy">Import an audio list, review your queue, and export neatly named recordings from one workspace.</div>',unsafe_allow_html=True)
completed = [r for r in S.records.values() if r['status']=='Completed' and Path(r['path']).is_file()]
failed = [r for r in S.records.values() if r['status']=='Failed']
a,b,c,d = st.columns(4)
a.metric('Saved recordings',f'{len(completed):,}')
b.metric('Needs attention',f'{len(failed):,}')
c.metric('Stored audio',f"{sum(r['size'] for r in completed)/1024**2:,.1f} MB")
d.metric('Download runs',len(S.history))
st.write('')
import_tab, library_tab, history_tab = st.tabs(['Import & download','Recording library','Session history'])

with import_tab:
    st.markdown('<div class="step">01 IMPORT → 02 MAP COLUMNS → 03 REVIEW & DOWNLOAD</div>',unsafe_allow_html=True)
    left,right = st.columns([3,1])
    with left:
        upload = st.file_uploader('Upload your audio list',type=['csv','xlsx','xls'])
    with right:
        st.markdown('**Need a starting point?**')
        st.caption('One name column and up to three link columns.')
        st.download_button('Download CSV template',b'audio_name,audio_link1,audio_link2,audio_link3\ninterview_001,,,\n',
                           file_name='audio_list_template.csv',mime='text/csv',on_click='ignore')
    if upload:
        try:
            raw = upload.getvalue()
            signature = hashlib.sha256(raw).hexdigest()
            if S.upload_signature != signature:
                for k in list(S):
                    if k.startswith(('map_','queue_')):
                        del S[k]
                S.upload_signature = signature
            if upload.name.lower().endswith('.csv'):
                df = pd.read_csv(io.BytesIO(raw),dtype=str,keep_default_na=False)
            else:
                xls = pd.ExcelFile(io.BytesIO(raw))
                sheet = st.selectbox('Worksheet',xls.sheet_names)
                df = pd.read_excel(xls,sheet_name=sheet,dtype=str,keep_default_na=False)
            if len(df.columns)<2:
                raise ValueError('Include a name column and at least one URL column.')
            # Positional labels avoid ambiguous duplicate column headings.
            labels = [f'{i+1}. {col}' for i,col in enumerate(df.columns)]
            st.subheader('Map your columns')
            maps = st.columns(4)
            name_index = maps[0].selectbox('Recording name',range(len(labels)),format_func=lambda i:labels[i],key='map_name')
            link_indices=[]
            for i in range(3):
                options = [-1] + list(range(len(labels)))
                selected = maps[i+1].selectbox(f'Audio link {i+1}',options,
                    index=i+2 if len(labels)>i+1 else 0,
                    format_func=lambda n:'Not used' if n==-1 else labels[n],key=f'map_link_{i}')
                if selected!=-1:
                    link_indices.append((i+1,selected))
            positions = [p for _,p in link_indices]
            if not positions or name_index in positions or len(set(positions))!=len(positions):
                st.warning('Choose at least one link column; every mapped column must be different.')
            else:
                host = S.auth['host'] if S.auth else 'www.commcarehq.org'
                tasks,issues = [],[]
                for idx in range(len(df)):
                    value = df.iloc[idx,name_index]
                    base = clean_name(value if str(value).strip() else f'audio_{idx+1}')
                    for slot,col in link_indices:
                        url,error = valid_url(df.iloc[idx,col],host)
                        if error:
                            issues.append({'Row':idx+2,'Column':str(df.columns[col]),'Issue':error})
                        if not url:
                            continue
                        identity = hashlib.sha256(f'{signature}|{idx}|{col}|{url}'.encode()).hexdigest()
                        ext = Path(urlparse(url).path).suffix.lower()
                        ext = ext if ext in EXTENSIONS else '.m4a'
                        # Full task ID ensures deterministic, collision-resistant names across uploads.
                        filename = f'{base}_{slot}__{identity}{ext}'
                        record = S.records.get(identity,{})
                        state = record.get('status','Pending')
                        if state=='Completed' and not Path(record.get('path','')).is_file():
                            state='Missing file'
                        tasks.append(dict(id=identity,row=idx+2,name=base,link=f'Audio {slot}',url=url,
                                          filename=filename,status=state))
                st.caption(f'{len(df):,} rows · {len(tasks):,} valid recordings · {len(issues):,} invalid links')
                with st.expander('Preview source data'):
                    st.dataframe(df.head(200),width='stretch',hide_index=True)
                if issues:
                    with st.expander('Review invalid links',expanded=True):
                        st.dataframe(pd.DataFrame(issues),width='stretch',hide_index=True)
                st.subheader('Review download queue')
                q1,q2 = st.columns([2,1])
                search = q1.text_input('Search recording names',key='queue_search',placeholder='Find a recording…')
                statuses = q2.multiselect('Queue status',['Pending','Failed','Completed','Missing file'],
                                          default=['Pending','Failed','Missing file'],key='queue_status')
                filtered=[t for t in tasks if search.casefold() in t['name'].casefold() and t['status'] in statuses]
                if filtered:
                    display_df = pd.DataFrame(filtered)[['row','name','link','filename','status']]
                    display_df.insert(0,'Select',[t['status']!='Completed' for t in filtered])
                    edited = st.data_editor(display_df,hide_index=True,width='stretch',
                                            disabled=['row','name','link','filename','status'],key='queue_editor')
                    chosen = [t for t,checked in zip(filtered,edited['Select']) if checked and t['status']!='Completed']
                    st.caption(f'{len(chosen):,} selected. Completed recordings are skipped; clear downloads to fetch them again.')
                    if st.button(f'Download selected ({len(chosen)})',type='primary',disabled=not chosen or not S.auth):
                        run_downloads(chosen,int(workers),int(timeout),int(attempts),int(max_mb))
                        st.rerun()
                else:
                    st.info('No recordings match the current filters.')
                if not S.auth:
                    st.info('Connect your account in the sidebar before starting a download.')
        except Exception as exc:
            st.error(f'Could not prepare this upload: {type(exc).__name__}. Check the file format and column selections.')
    else:
        st.info('Start by uploading an Excel workbook or CSV. Your first four columns are selected by default; you can change the mapping.')

with library_tab:
    st.subheader('Recording library')
    st.caption('Downloads from all uploads in this session. Logging out deletes these files and clears history.')
    if completed:
        filter_name=st.text_input('Find saved recordings',placeholder='Search the library')
        visible=[r for r in completed if filter_name.casefold() in r['name'].casefold()]
        st.dataframe(pd.DataFrame([{'Name':r['name'],'File':r['filename'],'MB':round(r['size']/1024**2,2)} for r in visible]),
                     width='stretch',hide_index=True)
        st.caption('ZIP export includes all completed recordings, not only the search results.')
        if st.button('Prepare ZIP archive',type='primary'):
            sig=tuple(sorted((r['id'],r['size']) for r in completed))
            if sig!=S.zip_signature or not S.zip_path or not Path(S.zip_path).is_file():
                if S.zip_path:
                    Path(S.zip_path).unlink(missing_ok=True)
                S.zip_path = str(Path(S.workspace)/'recordings.zip')
                with st.spinner('Packaging recordings…'):
                    with zipfile.ZipFile(S.zip_path,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as z:
                        for r in completed:
                            z.write(r['path'],arcname=r['filename'])
                        manifest = pd.DataFrame([{'name':r['name'],'filename':r['filename'],'source_row':r['row']} for r in completed])
                        z.writestr('manifest.csv',csv_bytes(manifest))
                S.zip_signature=sig
        if S.zip_path and Path(S.zip_path).is_file() and S.zip_signature:
            size=Path(S.zip_path).stat().st_size/1024**2
            st.caption(f'Archive size: {size:,.1f} MB')
            if size > 200:
                st.warning('Large archive: downloading is enabled, but Streamlit holds downloadable data in server memory. Ensure the server has sufficient available RAM.')
            with open(S.zip_path,'rb') as archive:
                st.download_button('Download ZIP archive',archive,file_name='commcare_recordings.zip',
                                   mime='application/zip',on_click='ignore')
        st.download_button('Export recording manifest',csv_bytes(pd.DataFrame([
            {'name':r['name'],'filename':r['filename'],'row':r['row']} for r in completed])),
            file_name='recording_manifest.csv',mime='text/csv',on_click='ignore')
    else:
        st.info('Your completed recordings will appear here.')
    if failed:
        st.divider()
        st.subheader('Needs attention')
        st.dataframe(pd.DataFrame(failed)[['name','row','link','error']],width='stretch',hide_index=True)
        if st.button(f'Retry all failed ({len(failed)})',disabled=not S.auth):
            run_downloads(failed,int(workers),int(timeout),int(attempts),int(max_mb))
            st.rerun()
        st.download_button('Export failure report',csv_bytes(pd.DataFrame(failed)[['name','row','link','url','error']]),
                           file_name='failed_recordings.csv',mime='text/csv',on_click='ignore')

with history_tab:
    st.subheader('Session activity')
    if S.history:
        history=pd.DataFrame(S.history[::-1])
        st.dataframe(history,width='stretch',hide_index=True)
        st.download_button('Export activity',csv_bytes(history),file_name='download_activity.csv',mime='text/csv',on_click='ignore')
    else:
        st.info('A summary of each download run will appear here.')
    st.caption('History is session-only, not a persistent audit log. Refreshes, server restarts or session expiry can lose session state.')

st.divider()
st.caption('Audio Workspace · CommCare • Keep API keys and participant recordings private. Explicitly clear downloads or log out when finished.')
