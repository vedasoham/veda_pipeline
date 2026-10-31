import streamlit as st
import os
from pathlib import Path
import json
import base64
import time
import pandas as pd
import shutil
import zipfile
import sys
import subprocess
import math
from concurrent.futures import ThreadPoolExecutor
import psutil
import datetime
import traceback

# --- Imports ---
import config
from backend import PipelineManager
from backend import proteins  # Make sure proteins.py is inside /backend
from backend import ui_logic  # Make sure ui_logic.py is inside /backend
from backend import pipeline
from backend.workers import vaxign_worker, deeploc_worker, gibbscluster_worker
import dashboard

# -----------------------------
# Streamlit Configuration
# -----------------------------
st.set_page_config(
    page_title="VEDA Command Center",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- Compatibility Check for Python 3.7 & Protobuf ---
if sys.version_info[:2] == (3, 7):
    try:
        import google.protobuf
        if google.protobuf.__version__.startswith("4"):
            msg = "⚠️ CRITICAL: Protobuf 4.x detected on Python 3.7. This causes RecursionErrors. Run: pip install protobuf==3.20.3"
            print(msg)
            st.error(msg)
    except (ImportError, Exception):
        pass

def safe_rerun():
    """Helper to handle Streamlit version differences for rerun."""
    if hasattr(st, "rerun"):
        st.rerun()
    else:
        st.experimental_rerun()

def set_step(step_name):
    """Callback to safely update the pipeline step."""
    st.session_state.pipeline_step_radio = step_name

def get_project_metadata(project_path):
    """Safely read project_data.json"""
    p_data = project_path / "project_data.json"
    if p_data.exists():
        try:
            with open(p_data, 'r') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

@st.cache_data(show_spinner=False)
def parse_fasta_content(content_bytes):
    """Parses FASTA content bytes into a DataFrame."""
    ids = []
    seqs = []
    curr_id = None
    curr_seq = []
    
    import io
    text_stream = io.StringIO(content_bytes.decode("utf-8"))
    
    for line in text_stream:
        line = line.strip()
        if not line: continue
        if line.startswith(">"):
            if curr_id:
                ids.append(curr_id)
                seqs.append("".join(curr_seq))
            curr_id = line[1:].split()[0]
            curr_seq = []
        else:
            curr_seq.append(line)
    if curr_id:
        ids.append(curr_id)
        seqs.append("".join(curr_seq))
        
    return pd.DataFrame({'UniProtID': ids, 'Sequence': seqs})

def get_size_str(path):
    """Returns formatted file size string."""
    path = Path(path)
    if not path.exists(): return "0 B"
    size = path.stat().st_size
    if size < 1024: return f"{size} B"
    elif size < 1024*1024: return f"{size/1024:.2f} KB"
    else: return f"{size/(1024*1024):.2f} MB"

def show_file_info(label, path):
    """Displays file status, path, and size."""
    if path is None:
        st.markdown(f"**{label}:** `None` &nbsp;&nbsp;|&nbsp;&nbsp; **Status:** ❌ Absent")
        return
    path = Path(path)
    exists = "✅ Present" if path.exists() else "❌ Absent"
    sz = get_size_str(path)
    try:
        disp_path = path.relative_to(PROJECT_PATH)
    except:
        disp_path = path.name
    st.markdown(f"**{label}:** `{disp_path}` &nbsp;&nbsp;|&nbsp;&nbsp; **Status:** {exists} &nbsp;&nbsp;|&nbsp;&nbsp; **Size:** `{sz}`")

def get_cpu_temperature():
    """Attempts to find a valid CPU temperature."""
    try:
        temps = psutil.sensors_temperatures()
        if not temps: return "N/A"
        # Priority list for sensor names
        priority_names = ['coretemp', 'k10temp', 'zenpower', 'acpitz']
        # 1. Try priority sensors first
        for name in priority_names:
            if name in temps:
                for entry in temps[name]:
                    if entry.current > 0: return entry.current
        # 2. Fallback to any sensor with positive value
        for name, entries in temps.items():
            for entry in entries:
                if entry.current > 0: return entry.current
        return "N/A"
    except Exception: 
        return "N/A"

def render_resource_monitor():
    """Renders system resource usage with dynamic updates."""
    with st.expander("💻 System Resource Monitor", expanded=False):
        monitor_placeholder = st.empty()
        
        def _update_metrics():
            with monitor_placeholder.container():
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("CPU Cores", f"{psutil.cpu_count(logical=True)}")
                c1.metric("CPU Usage", f"{psutil.cpu_percent(interval=0.1)}%")
                
                mem = psutil.virtual_memory()
                c2.metric("System RAM", f"{mem.used / (1024**3):.1f} / {mem.total / (1024**3):.1f} GB")
                
                # Get specific RAM usage for this Python app
                process = psutil.Process(os.getpid())
                app_mem_mb = process.memory_info().rss / (1024**2)
                if app_mem_mb > 1024:
                    c2.metric("App RAM", f"{app_mem_mb/1024:.2f} GB")
                else:
                    c2.metric("App RAM", f"{app_mem_mb:.1f} MB")
                    
                temp = get_cpu_temperature()
                c3.metric("CPU Temp", f"{temp} °C" if temp != "N/A" else "N/A")
                has_nvidia = shutil.which("nvidia-smi") is not None
                c4.metric("GPU Status", "Present" if has_nvidia else "Not Found")

        _update_metrics()
        if st.button("🔄 Live Monitor"):
            for _ in range(5):
                _update_metrics()
                time.sleep(1)

@st.cache_data(show_spinner=False)
def _get_cached_mhc_stats(json_path_str):
    """Cached helper to prevent reloading massive JSON files. Aggressively utilizes up to 80% RAM for max speed."""
    json_path = Path(json_path_str)
    res_dir = json_path.parent
    
    unique_peptides, alleles, sb_peps, wb_peps = set(), set(), set(), set()
    sb_evts, wb_evts = 0, 0
    import gc
    import psutil
    from concurrent.futures import ThreadPoolExecutor

    # Strategy 1: Fast Parallel extraction from intermediate batch files
    batch_files = sorted(list(res_dir.glob("batch_*.json")))
    if batch_files and len(batch_files) > 1:
        valid_files = [jf for jf in batch_files if jf.name != "netmhc_results.json"]
        
        def _parse_file(jf):
            lp, ls, lw, la = set(), set(), set(), set()
            e_sb, e_wb = 0, 0
            try:
                with open(jf, 'r') as f_in: data = json.load(f_in)
                if isinstance(data, list):
                    for item in data:
                        pep, alll, lvl = item.get('peptide'), item.get('allele'), item.get('level')
                        if pep:
                            lp.add(pep)
                            if lvl == 'SB': 
                                ls.add(pep)
                                e_sb += 1
                            elif lvl == 'WB': 
                                lw.add(pep)
                                e_wb += 1
                        if alll: la.add(alll)
            except Exception: pass
            return lp, ls, lw, la, e_sb, e_wb

        # Max out ThreadPool to aggressively utilize RAM and multiple CPUs
        threads = min(32, (os.cpu_count() or 4) * 2)
        with ThreadPoolExecutor(max_workers=threads) as executor:
            for res in executor.map(_parse_file, valid_files):
                unique_peptides.update(res[0])
                sb_peps.update(res[1])
                wb_peps.update(res[2])
                alleles.update(res[3])
                sb_evts += res[4]
                wb_evts += res[5]
                
                # Stay within 80% RAM boundary dynamically
                if psutil.virtual_memory().percent > 80.0: gc.collect()
                
        return len(unique_peptides), len(alleles), len(sb_peps), len(wb_peps - sb_peps), len(unique_peptides - (sb_peps | wb_peps)), sb_evts, wb_evts

    # Strategy 2: Bulk load the massive netmhc_results.json if memory allows
    try:
        if not json_path.exists(): return None
        file_size = json_path.stat().st_size
        mem = psutil.virtual_memory()
        
        # Less conservative check: Dicts take ~5x file size. Let it run up to 80% RAM.
        if (file_size * 5) < (mem.total * 0.8 - mem.used):
            with open(json_path, 'r') as f:
                data = json.load(f)
                if isinstance(data, list):
                    for item in data:
                        pep, alll, lvl = item.get('peptide'), item.get('allele'), item.get('level')
                        if pep:
                            unique_peptides.add(pep)
                            if lvl == 'SB': 
                                sb_peps.add(pep)
                                sb_evts += 1
                            elif lvl == 'WB': 
                                wb_peps.add(pep)
                                wb_evts += 1
                        if alll: alleles.add(alll)
                del data
                gc.collect()
        else:
            # Chunked line-by-line fallback if absolutely necessary
            with open(json_path, 'r') as f:
                for i, line in enumerate(f):
                    if i % 100000 == 0 and psutil.virtual_memory().percent > 80.0: gc.collect()
                    
                    line = line.strip()
                    if line in ("[", "]", ""): continue
                    if line.endswith(","): line = line[:-1]
                    try:
                        item = json.loads(line)
                        pep, alll, lvl = item.get('peptide'), item.get('allele'), item.get('level')
                        if pep:
                            unique_peptides.add(pep)
                            if lvl == 'SB': 
                                sb_peps.add(pep)
                                sb_evts += 1
                            elif lvl == 'WB': 
                                wb_peps.add(pep)
                                wb_evts += 1
                        if alll: alleles.add(alll)
                    except json.JSONDecodeError: continue
    except (FileNotFoundError, Exception): return None
            
    return len(unique_peptides), len(alleles), len(sb_peps), len(wb_peps - sb_peps), len(unique_peptides - (sb_peps | wb_peps)), sb_evts, wb_evts

def display_netmhc_stats(tool_name, json_path, res_dir, parameters=None):
    """Calculates and displays statistics for NetMHC runs."""
    stats_json_path = res_dir / "netmhc_stats.json"
    
    n_peptides, n_alleles, n_sb, n_wb, n_nb = 0, 0, 0, 0, 0
    n_sb_events, n_wb_events = "N/A", "N/A"
    n_batches, n_parts = "N/A", "N/A"
    n_elapsed = "N/A"
    stats_loaded = False

    # --- PRIORITY 1: Try to load from pre-computed stats file ---
    if stats_json_path.exists():
        try:
            with open(stats_json_path, 'r') as f:
                stats = json.load(f)
            n_peptides = stats.get("n_peptides", 0)
            n_alleles = stats.get("n_alleles", 0)
            n_sb = stats.get("n_sb", 0)
            n_wb = stats.get("n_wb", 0)
            n_nb = stats.get("n_nb", 0)
            n_sb_events = stats.get("n_sb_events", "N/A")
            n_wb_events = stats.get("n_wb_events", "N/A")
            n_batches = stats.get("n_batches", "N/A")
            n_parts = stats.get("n_parts", "N/A")
            n_elapsed = stats.get("elapsed_seconds", "N/A")
            stats_loaded = True
        except (json.JSONDecodeError, FileNotFoundError):
            stats_loaded = False # Will trigger fallback

    # --- PRIORITY 2: Fallback to reading the full JSON (if stats file is missing/corrupt) ---
    if not stats_loaded:
        if not json_path.exists(): return
        cached_stats = _get_cached_mhc_stats(str(json_path))
        if not cached_stats: return
        n_peptides, n_alleles, n_sb, n_wb, n_nb, n_sb_events, n_wb_events = cached_stats

    # Get batch/part info from project metadata if not in stats (older runs)
    if n_batches == "N/A":
        p_data = get_project_metadata(PROJECT_PATH)
        run_inputs = p_data.get(tool_name, {}).get("inputs", {})
        n_batches = run_inputs.get("batches", "N/A")
        n_parts = run_inputs.get("parts_per_batch", "N/A")
        
        if n_batches == "N/A":
            import re
            log_file_name = "NetMHCpan.log" if "ii" not in tool_name.lower() else "NetMHCIIpan.log"
            log_file_path = res_dir / log_file_name
            if log_file_path.exists():
                try:
                    with open(log_file_path, "r") as f:
                        content = f.read()
                        b_match = re.search(r"Total batches:\s*(\d+)", content)
                        if b_match: n_batches = int(b_match.group(1))
                        
                        p_match = re.search(r"Allele chunks:\s*(\d+)", content)
                        if p_match: n_parts = int(p_match.group(1))
                except Exception: pass
            
            if n_batches == "N/A":
                batch_files = [f for f in res_dir.glob("batch_*.fasta") if re.match(r'batch_\d+\.fasta', f.name)]
                n_batches = len(batch_files) if batch_files else "N/A"
                part_files = list(res_dir.glob("batch_1_part*.fasta"))
                if not part_files: part_files = list(res_dir.glob("batch_1_part*.json"))
                n_parts = len(part_files) if part_files else "N/A"
    
    # Calculate percentages
    pct_sb = (n_sb / n_peptides * 100) if n_peptides > 0 else 0.0
    pct_wb = (n_wb / n_peptides * 100) if n_peptides > 0 else 0.0
    pct_nb = (n_nb / n_peptides * 100) if n_peptides > 0 else 0.0

    # Try to fetch elapsed time from logs if not in stats
    if not stats_loaded or n_elapsed == "N/A":
        log_path = res_dir / ("NetMHCpan.log" if "ii" not in tool_name.lower() else "NetMHCIIpan.log")
        if log_path.exists():
            try:
                with open(log_path, "r") as f:
                    t_match = re.search(r"Finished in ([\d\.]+)s", f.read())
                    if t_match: n_elapsed = t_match.group(1)
            except: pass

    st.markdown("---")
    col_head, col_btn = st.columns([6, 1])
    col_head.subheader("📊 Run Statistics")
    if col_btn.button("🔄 Recount", key=f"recount_{tool_name}"):
        if stats_json_path.exists():
            stats_json_path.unlink()
        safe_rerun()

    with st.container():
        st.caption("**Input & Execution Summary**")
        if n_elapsed != "N/A":
            st.caption(f"⏱️ **Elapsed Time:** `{n_elapsed} seconds`")
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Peptides", n_peptides, help="Unique Sequences")
        k2.metric("Alleles", n_alleles, help="Target MHCs")
        k3.metric("Batches", n_batches, help="Seq Chunks")
        k4.metric("Parts per Batch", n_parts, help="Allele Chunks")

    with st.container():
        st.caption("**Unique Peptide Predictions**")
        r1, r2, r3 = st.columns(3)
        r1.metric("Strong Binders (Peptides)", n_sb, f"{pct_sb:.1f}%", delta_color="normal", help="Number of UNIQUE peptides that are Strong Binders to at least 1 allele")
        r2.metric("Weak Binders (Peptides)", n_wb, f"{pct_wb:.1f}%", delta_color="inverse")
        r3.metric("Non-Binders (Peptides)", n_nb, f"{pct_nb:.1f}%", delta_color="off")
        
    if n_sb_events != "N/A":
        with st.container():
            st.caption("**Total Binding Events**")
            e1, e2, _ = st.columns(3)
            e1.metric("Strong Binding Events", n_sb_events, help="Total number of Strong Binder hits across all alleles")
            e2.metric("Weak Binding Events", n_wb_events)
    
    # Update Project Data
    config.update_project_metadata(
        PROJECT_PATH,
        tool_name,
        inputs={
            "batches": n_batches, 
            "parts_per_batch": n_parts, 
            "n_peptides": n_peptides, 
            "n_alleles": n_alleles
        },
        outputs={
            "strong_binders": n_sb, 
            "weak_binders": n_wb, 
            "non_binders": n_nb
        },
        parameters={**(parameters or {}), "elapsed_seconds": n_elapsed}
    )

# Load CSS
ui_logic.load_css(config.APP_DIR / "css/styles.css")

# --- Institute Logo Setup ---
# Use absolute path relative to this script to ensure we find the logo
resources_path = Path(__file__).resolve().parent.parent / "resources"
logo_path = resources_path / "logo.png"
iel_path = resources_path / "iel.png"
cxbl_path = resources_path / "cxbl.png"

logo_html = ""
if logo_path.exists():
    with open(logo_path, "rb") as f:
        enc = base64.b64encode(f.read()).decode()
    logo_html = f'<img src="data:image/png;base64,{enc}" class="institute-logo">'

iel_html = ""
if iel_path.exists():
    with open(iel_path, "rb") as f:
        enc = base64.b64encode(f.read()).decode()
    iel_html = f'<img src="data:image/png;base64,{enc}" class="right-logo">'

cxbl_html = ""
if cxbl_path.exists():
    with open(cxbl_path, "rb") as f:
        enc = base64.b64encode(f.read()).decode()
    cxbl_html = f'<img src="data:image/png;base64,{enc}" class="right-logo">'

st.markdown(f'''
<div class="main-header">
    {logo_html}
    <div class="header-text-container">
        <span class="IEL-logo">VEDA</span>
        <h1>Vaccine Epitope Discovery & Analysis</h1>
    </div>
    <div class="right-logo-container">
        {iel_html}
        {cxbl_html}
    </div>
</div>
''', unsafe_allow_html=True)

# --- Custom Sidebar CSS ---
st.markdown("""
<style>
    /* Logo Styling */
    .institute-logo {
        width: 128px;
        height: 128px;
        object-fit: contain;
        padding: 3px;
        background-color: rgba(255,255,255,1.0);
        border-radius: 50%;
        box-shadow: 0 4px 15px rgba(0,0,0,0.5);
        position: absolute;
        left: 26px;
        top: 58%;
        transform: translateY(-50%);
        z-index: 2;
    }

    /* Right Logos Styling */
    .right-logo-container {
        position: absolute;
        right: 30px;
        top: 50%;
        transform: translateY(-50%);
        display: flex;
        gap: 20px;
        z-index: 2;
    }
    .right-logo {
        width: 115px;
        height: 115px;
        object-fit: full;
        background-color: rgba(255,255,255,0.1);
        border-radius: 12px; /* Square with rounded corners */
        margin-top: 20px;
        box-shadow: 0 4px 10px rgba(0,0,0,0.2);
    }

    /* Main Banner Container */
    .main-header {
        position: relative;
        background: linear-gradient(135deg, #00acc1 0%, #00796b 100%); /* Deeper Teal Gradient */
        min-height: 160px;
        border-radius: 15px;
        display: flex;
        align-items: center;
        justify-content: center;
        box-shadow: 0 10px 25px rgba(0, 121, 107, 0.3); /* Colored shadow match */
        overflow: hidden;
        margin-bottom: 2rem;
        min-height: 180px;
    }
    
    /* Background Pattern (Optional Aesthetic) */
    .main-header::before {
        content: "";
        position: absolute;
        top: 0; left: 0; right: 0; bottom: 0;
        background-image: radial-gradient(circle at 20% 50%, rgba(255,255,255,0.1) 2px, transparent 2px),
                          radial-gradient(circle at 80% 30%, rgba(255,255,255,0.1) 2px, transparent 2px);
        background-size: 40px 40px;
        opacity: 0.6;
    }

    /* Text Container */
    .header-text-container {
        text-align: center;
        z-index: 1;
        margin-top: 30px;
        margin-left: -160px;
    }

    /* VEDA Text (Fixed Color) */
    .IEL-logo {
        font-family: 'Orbitron';
        font-size: 3rem;
        font-weight: 950;
        letter-spacing: 6px;
        color: #ffffff !important;
        background: none !important; 
        -webkit-text-fill-color: rgba(255,255,255,0.95) !important;
        text-shadow: 0 4px 12px rgba(0,0,0,0.4);
        display: block;
        line-height: 1;
        margin-bottom: 5px;
    }

    /* Full Title (Increased Size) */
    .main-header h1 {
        font-family: 'Lato';
        font-size: 2.5rem !important; /* Increased from default */
        font-weight: 500; /* Lighter weight for elegance */
        color: #e0f2f1 !important; /* Soft white */
        margin: 5;
        padding: 0;
        letter-spacing: 1px;
        text-shadow: 0 2px 4px rgba(0,0,0,0.2);
        text-transform: uppercase;
    }

    /* Sidebar styling */
    [data-testid="stSidebar"] {
        background: 
            radial-gradient(circle at top left, #e0f7fa, transparent 50%),
            radial-gradient(circle at bottom right, #e0f2f1, transparent 50%),
            #ffffff;
        border-right: 1px solid rgba(0, 172, 193, 0.2);
    }
    
    /* Radio Button Styling (Cards) */
    [data-testid="stSidebar"] .stRadio > div[role="radiogroup"] > label {
        background: #ffffff;
        padding: 10px 15px;
        border-radius: 8px;
        border: 1px solid #e2e8f0;
        margin-bottom: 6px;
        transition: all 0.2s ease;
        box-shadow: 0 1px 2px rgba(0,0,0,0.05);
        color: #475569;
        width: 100%;
        box-sizing: border-box;
        display: flex;
    }
    
    /* Hover Effect */
    [data-testid="stSidebar"] .stRadio > div[role="radiogroup"] > label:hover {
        border-color: #3b82f6;
        background: #eff6ff;
        color: #1d4ed8;
        transform: translateX(4px);
    }
    
    /* Active Selection Highlight */
    [data-testid="stSidebar"] .stRadio > div[role="radiogroup"] > label:has(input:checked) {
        background-color: #eff6ff;
        border-color: #3b82f6;
        color: #1d4ed8;
        font-weight: 600;
        box-shadow: 0 4px 6px -1px rgba(59, 130, 246, 0.1);
    }
    
    /* Sidebar Header */
    .sidebar-logo {
        font-size: 1.5rem;
        font-weight: 800;
        background: -webkit-linear-gradient(135deg, #00acc1 0%, #00796b 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        margin-bottom: 1rem;
        text-align: center;
        padding-bottom: 1rem;
        border-bottom: 1px solid #e2e8f0;
    }

    /* Reduce Streamlit Toolbar Height */
    header[data-testid="stHeader"] {
        height: 0px !important;
    }
    
    [data-testid="stToolbar"] {
        height: 40px !important;
        min-height: 40px !important;
        background-color: rgba(255, 255, 255, 0.2) !important;
    }
</style>
""", unsafe_allow_html=True)

# -----------------------------
# Session State Initialization
# -----------------------------
if 'protein_df' not in st.session_state:
    st.session_state['protein_df'] = None
if 'filtered_df' not in st.session_state:
    st.session_state['filtered_df'] = None
if 'fetch_failed_ids' not in st.session_state:
    st.session_state['fetch_failed_ids'] = []
if 'pipeline_step_radio' not in st.session_state:
    st.session_state['pipeline_step_radio'] = "1. Upload & Filter"
if 'peptide_count' not in st.session_state:
    st.session_state['peptide_count'] = 0
if 'is_fetching' not in st.session_state:
    st.session_state['is_fetching'] = False
if 'fetch_progress_idx' not in st.session_state:
    st.session_state['fetch_progress_idx'] = 0
if 'fetched_seq_cache' not in st.session_state:
    st.session_state['fetched_seq_cache'] = {}
if 'is_refetching' not in st.session_state:
    st.session_state['is_refetching'] = False
if 'refetch_seq_cache' not in st.session_state:
    st.session_state['refetch_seq_cache'] = {}

# -----------------------------
# Resource Monitor (Top Right / Top Area)
# -----------------------------
render_resource_monitor()

# -----------------------------
# Main App Logic
# -----------------------------
PROJECT_PATH = config.DATA_DIR
PROJECT_PATH.mkdir(parents=True, exist_ok=True)

UPLOAD_DIR = PROJECT_PATH
INTERMEDIATE_DIR = PROJECT_PATH
RESULTS_DIR = PROJECT_PATH
PROJECT_CONFIG = PROJECT_PATH / "project_config.json"
PROJECT_DATA = PROJECT_PATH / "project_data.json"

# --- Standardized Background Execution ---
def launch_background_worker(status_file, stop_signal_file, func, *args, **kwargs):
    """Standardized background worker launcher."""
    status_file.parent.mkdir(parents=True, exist_ok=True)
    with open(status_file, "w") as f: f.write("running")
    if stop_signal_file.exists(): stop_signal_file.unlink()
    
    def bg_wrapper():
        start_t = time.time()
        success = False
        try:
            success = func(*args, **kwargs)
        except Exception as e:
            print(f"Error in background worker: {e}")
            traceback.print_exc()
        
        elapsed = time.time() - start_t
        if stop_signal_file.exists():
            with open(status_file, "w") as f: f.write("stopped")
        else:
            if success:
                with open(status_file, "w") as f: f.write(f"completed:{elapsed:.2f}")
            else:
                with open(status_file, "w") as f: f.write("failed")
    
    import threading
    t = threading.Thread(target=bg_wrapper)
    t.start()

def split_fasta_batches(input_fasta, output_dir, batch_size=90):
    """Splits a FASTA file into batches."""
    output_dir.mkdir(parents=True, exist_ok=True)
    
    batch_files = []
    with open(input_fasta, 'r') as f:
        batch_idx = 1
        curr_batch_count = 0
        curr_file = None
        curr_id = None
        curr_seq = []
        
        def write_seq(pid, seq_list):
            nonlocal curr_batch_count, batch_idx, curr_file
            if curr_batch_count == 0:
                b_file = output_dir / f"batch_{batch_idx}.fasta"
                curr_file = open(b_file, 'w')
                batch_files.append(b_file)
            curr_file.write(f"{pid}\n{''.join(seq_list)}\n")
            curr_batch_count += 1
            if curr_batch_count >= batch_size:
                curr_file.close()
                curr_file = None
                curr_batch_count = 0
                batch_idx += 1

        for line in f:
            line = line.strip()
            if not line: continue
            if line.startswith(">"):
                if curr_id: write_seq(curr_id, curr_seq)
                curr_id = line
                curr_seq = []
            else:
                curr_seq.append(line)
        if curr_id: write_seq(curr_id, curr_seq)
        if curr_file and not curr_file.closed: curr_file.close()

    return batch_files

def filter_fasta_by_length(input_path, output_dir, min_len=None, max_len=None):
    """Filters FASTA file for sequences within length range."""
    try:
        input_path = Path(input_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "input_filtered.fasta"
        
        valid_count = 0
        total_count = 0
        
        with open(input_path, 'r') as f_in, open(output_path, 'w') as f_out:
            header = None
            seq = []
            
            def process_entry(h, s):
                nonlocal valid_count, total_count
                if not h: return
                total_count += 1
                s_str = "".join(s).strip().replace("*", "").upper()
                length = len(s_str)
                
                if min_len is not None and length < min_len: return
                if max_len is not None and length > max_len: return
                
                f_out.write(f">{h}\n{s_str}\n")
                valid_count += 1

            for line in f_in:
                line = line.strip()
                if not line: continue
                if line.startswith(">"):
                    if header: process_entry(header, seq)
                    header = line[1:]
                    seq = []
                else: seq.append(line)
            if header: process_entry(header, seq)
                
        return output_path, valid_count, total_count
    except Exception as e:
        st.error(f"❌ Error preparing input file: {e}")
        return None, 0, 0

def run_batched_mhc(tool_name, input_fasta, output_dir, alleles, species="human", batch_size=90, threads=4, length=9):
    """Runs MHC prediction in batches to save RAM."""
    start_t = time.time()
    # Set threads env var since pipeline.run_mhc doesn't accept the arg
    os.environ['OMP_NUM_THREADS'] = str(threads)
    os.environ['MKL_NUM_THREADS'] = str(threads)

    # Set NMHOME env var (Required for NetMHC tools, especially if bypassing wrapper)
    if "ii" in tool_name.lower():
        os.environ['NMHOME'] = str(config.NETMHCIIPAN_DIR)
        os.environ['NETMHCIIpan'] = str(config.NETMHCIIPAN_DIR)
    else:
        os.environ['NMHOME'] = str(config.NETMHCPAN_DIR)
        os.environ['NETMHCpan'] = str(config.NETMHCPAN_DIR)

    # Set TMPDIR to system /tmp to avoid buffer overflow issues with long paths in NetMHCpan
    os.environ['TMPDIR'] = '/tmp'

    batches = split_fasta_batches(input_fasta, output_dir, batch_size)
    log_file = output_dir / f"{tool_name}.log"
    status_file = output_dir / "status.txt"
    stop_file = output_dir / "STOP"
    total_batches = len(batches)

    # Chunk alleles to prevent command line overflow (limit ~20 per call)
    ALLELE_LIMIT = 20
    allele_chunks = [alleles[i:i + ALLELE_LIMIT] for i in range(0, len(alleles), ALLELE_LIMIT)]
    if not allele_chunks: allele_chunks = [[]]
    
    with open(status_file, "w") as f: f.write(f"running:{os.getpid()}:0:{total_batches}:{len(allele_chunks)}")
    
    with open(log_file, "w") as f:
        f.write(f"Starting batched {tool_name} (Batch Size: {batch_size}, Threads: {threads})\n")
        f.write(f"Total batches: {len(batches)}\n")
        f.write(f"Allele chunks: {len(allele_chunks)} (Total alleles: {len(alleles)})\n")

    tool_key = "netmhciipan" if "ii" in tool_name.lower() else "netmhcpan"

    for i, b_file in enumerate(batches):
        if stop_file.exists():
            with open(log_file, "a") as f: f.write(f"\n!!! Process Stopped by User at Batch {i+1}\n")
            with open(status_file, "w") as f: f.write("stopped")
            try: stop_file.unlink(); 
            except OSError: pass
            return False

        # Iterate over allele chunks
        for chunk_idx, allele_chunk in enumerate(allele_chunks):
            part_suffix = f" [Part {chunk_idx+1}/{len(allele_chunks)}]" if len(allele_chunks) > 1 else ""
            
            current_fasta = b_file
            is_temp_file = False
            
            if len(allele_chunks) > 1:
                # Define the part filename
                current_fasta = b_file.parent / f"{b_file.stem}_part{chunk_idx}{b_file.suffix}"

            # --- RESUME LOGIC: Check if output exists ---
            check_stem = current_fasta.stem
            expected_json = output_dir / f"{check_stem}.json"

            if expected_json.exists():
                with open(log_file, "a") as f: 
                    f.write(f"⏩ Skipping Batch {i+1}{part_suffix}: Results exist ({expected_json.name}).\n")
                # Update status so progress bar reflects this batch is 'done'
                with open(status_file, "w") as f: f.write(f"running:{os.getpid()}:{i+1}:{total_batches}:{len(allele_chunks)}")
                continue
            # --------------------------------------------

            if len(allele_chunks) > 1:
                # Create the temporary part file only if we are actually running
                if not current_fasta.exists():
                    shutil.copy(b_file, current_fasta)
                is_temp_file = True

            try:
                success = pipeline.run_mhc(fasta_file=current_fasta, tool=tool_key, species=species, alleles=allele_chunk, max_jobs=threads, log_file=log_file, length=length)
                if not success:
                    with open(log_file, "a") as f: 
                        f.write(f"!!! Batch {i+1}{part_suffix} Failed: NetMHC tool returned an error or empty output.\n")
            except Exception as e:
                err_msg = str(e)
                with open(log_file, "a") as f: 
                    f.write(f"!!! Batch {i+1}{part_suffix} Failed: {err_msg}\n")
                    
                    # Enhanced Error Diagnostics
                    if "interpreter shutdown" in err_msg:
                        f.write("\n[DIAGNOSIS] 🛑 Interpreter Shutdown\n")
                        f.write("The background worker thread was terminated. This usually occurs if the application restarts while a job is initializing.\n")
                        f.write("-> ACTION: Please restart the run. If persistent, try reducing threads.\n")
                        # Mark as stopped so UI doesn't hang
                        with open(status_file, "w") as sf: sf.write("stopped")
                        return False
                    elif "No match" in err_msg or "not found" in err_msg:
                        f.write("\n[DIAGNOSIS] 🔍 Binary Not Found\n")
                        f.write(f"The system could not locate the executable for {tool_name}.\n")
                        f.write("-> ACTION: Verify the paths in 'src/config.py' match your installation.\n")
                    elif "Permission denied" in err_msg:
                        f.write("\n[DIAGNOSIS] 🔒 Permission Denied\n")
                        f.write("The tool binary is not executable.\n")
                        f.write(f"-> ACTION: Run 'chmod +x' on the {tool_name} binary.\n")
                    else:
                        f.write(f"\n[DIAGNOSIS] ⚠️ Unknown Error: {err_msg}\n")
            finally:
                if is_temp_file and current_fasta.exists():
                    current_fasta.unlink()
        
        # Update status with progress
        with open(status_file, "w") as f: f.write(f"running:{os.getpid()}:{i+1}:{total_batches}:{len(allele_chunks)}")
        
        import gc
        gc.collect()
        time.sleep(1)

    # Concatenate Results, Generate Stats, and Cleanup
    with open(log_file, "a") as f: f.write("\nFinalizing results...\n")
    
    glob_pattern = "batch_*_part*.json"
    json_files = sorted(list(output_dir.glob(glob_pattern)))
    if not json_files:
        glob_pattern = "batch_*.json"
        json_files = sorted(list(output_dir.glob(glob_pattern)))

    if not json_files:
        with open(log_file, "a") as f: f.write("❌ No valid JSON results were generated. All batches failed.\n")
        with open(status_file, "w") as f: f.write("stopped")
        return False

    if json_files:
        final_json_path = output_dir / "netmhc_results.json"
        stats_json_path = output_dir / "netmhc_stats.json"
        
        unique_peptides, alleles, sb_peps, wb_peps = set(), set(), set(), set()
        total_json_files = len(json_files)

        # 1. Parallel Stats Extraction (High RAM & CPU Utilization)
        def _extract_stats(file_path):
            lp, ls, lw, la = set(), set(), set(), set()
            e_sb, e_wb = 0, 0
            try:
                with open(file_path, 'r') as f_in:
                    data = json.load(f_in)
                if isinstance(data, list):
                    for item in data:
                        p, a, l = item.get('peptide'), item.get('allele'), item.get('level')
                        if p:
                            lp.add(p)
                            if l == 'SB': 
                                ls.add(p)
                                e_sb += 1
                            elif l == 'WB': 
                                lw.add(p)
                                e_wb += 1
                        if a: la.add(a)
            except Exception: pass
            return lp, ls, lw, la, e_sb, e_wb

        with open(log_file, "a") as f: f.write("Extracting statistics in parallel...\n")
        total_sb_events, total_wb_events = 0, 0
        with ThreadPoolExecutor(max_workers=min(32, threads * 2)) as executor:
            futures = [executor.submit(_extract_stats, jf) for jf in json_files]
            for idx, future in enumerate(futures):
                if idx % 10 == 0:
                    with open(status_file, "w") as f: f.write(f"finalizing:{os.getpid()}:{idx}:{total_json_files}")
                
                res = future.result()
                unique_peptides.update(res[0])
                sb_peps.update(res[1])
                wb_peps.update(res[2])
                alleles.update(res[3])
                total_sb_events += res[4]
                total_wb_events += res[5]
                
                if psutil.virtual_memory().percent > 80.0:
                    import gc; gc.collect()

        with open(status_file, "w") as f: f.write(f"finalizing:{os.getpid()}:{total_json_files}:{total_json_files}")

        n_peptides = len(unique_peptides)
        stats_data = {
            "n_peptides": n_peptides, "n_alleles": len(alleles), "n_sb": len(sb_peps),
            "n_wb": len(wb_peps - sb_peps), "n_nb": n_peptides - len(sb_peps | wb_peps),
            "n_sb_events": total_sb_events, "n_wb_events": total_wb_events,
            "n_batches": total_batches, "n_parts": len(allele_chunks)
        }
        with open(stats_json_path, "w") as f_stats:
            json.dump(stats_data, f_stats, indent=4)
        with open(log_file, "a") as f: f.write(f"✅ Stats summary saved to {stats_json_path.name}\n")

        # 2. Fast I/O String Concatenation
        with open(log_file, "a") as f: f.write("Concatenating files natively...\n")
        with open(final_json_path, "w") as f_out:
            f_out.write("[\n")
            is_first_item = True
            for jf in json_files:
                try:
                    with open(jf, 'r') as f_in:
                        content = f_in.read().strip()
                    if content.startswith("["): content = content[1:]
                    if content.endswith("]"): content = content[:-1]
                    content = content.strip()
                    if not content: continue
                    
                    if not is_first_item:
                        f_out.write(",\n")
                    f_out.write(content)
                    is_first_item = False
                except Exception as e:
                    with open(log_file, "a") as f: f.write(f"  - Warning: Could not concatenate {jf.name}: {e}\n")
            f_out.write("\n]\n")

        # Combine TXT files natively as well
        final_txt_path = output_dir / "netmhc_out.txt"
        txt_files = [jf.with_suffix('.txt') for jf in json_files if jf.with_suffix('.txt').exists()]
        if txt_files:
            with open(final_txt_path, "w") as f_txt_out:
                for tf in txt_files:
                    try:
                        with open(tf, 'r') as f_in:
                            f_txt_out.write(f_in.read())
                            f_txt_out.write("\n")
                    except Exception as e:
                        with open(log_file, "a") as f: f.write(f"  - Warning: Could not concatenate {tf.name}: {e}\n")

        # 3. Cleanup of intermediate files
        with open(log_file, "a") as f: f.write("Cleaning up intermediate files...\n")
        files_to_keep = {f"{tool_name}.log", "status.txt", "netmhc_results.json", "netmhc_stats.json", "netmhc_out.txt"}
        for item in output_dir.iterdir():
            if item.is_file() and item.name not in files_to_keep:
                try: item.unlink()
                except OSError as e:
                    with open(log_file, "a") as f: f.write(f"  - Warning: Could not delete {item.name}: {e}\n")
        with open(log_file, "a") as f: f.write("Cleanup complete.\n")

    elapsed_total = time.time() - start_t
    with open(log_file, "a") as f: f.write(f"\n✅ Finished in {elapsed_total:.2f}s\n")
    with open(log_file, "a") as f: f.write("Batch processing complete.\n")
    with open(status_file, "w") as f: f.write("completed")
    return True

def generate_mhc_filtered_sets(binding_types, mhc_tools, use_promiscuity, promiscuity_threshold):
    """
    Parses NetMHC(II)pan JSON results to generate separate SB, WB, and NB
    peptide and protein FASTA files based on binding strength and promiscuity.
    """
    st.info("Parsing MHC results... This may take a moment for large datasets.")
    
    # Data structure to hold all peptide information
    # peptide_seq -> {'levels': {'SB', 'WB'}, 'alleles': {'HLA-A...', 'HLA-B...'}}
    peptide_data = {}

    # 1. Load data from JSONs
    for tool in mhc_tools:
        tool_key = "netmhcpan" if "pan" in tool.lower() else "netmhciipan"
        json_path = INTERMEDIATE_DIR / tool_key / "netmhc_results.json"
        if not json_path.exists():
            st.warning(f"Results for {tool} not found. Skipping.")
            continue
        
        try:
            with open(json_path, 'r') as f:
                results = json.load(f)
            
            for res in results:
                pep, lvl, allele = res.get('peptide'), res.get('level'), res.get('allele')
                if not pep: continue
                
                if pep not in peptide_data:
                    peptide_data[pep] = {'SB_alleles': set(), 'WB_alleles': set(), 'NB_alleles': set()}

                if lvl == 'SB':
                    peptide_data[pep]['SB_alleles'].add(allele)
                elif lvl == 'WB':
                    peptide_data[pep]['WB_alleles'].add(allele)
                else:
                    peptide_data[pep]['NB_alleles'].add(allele)
        except Exception as e:
            st.error(f"Failed to load or parse {json_path.name}: {e}")
            return False

    if not peptide_data:
        st.error("No peptide data could be loaded from the selected MHC results.")
        return False

    # 2. Filter peptides based on user criteria
    sb_peps, wb_peps, nb_peps = set(), set(), set()

    for pep, data in peptide_data.items():
        sb_count = len(data['SB_alleles'])
        wb_count = len(data['WB_alleles'])
        nb_count = len(data['NB_alleles'])
        
        if use_promiscuity:
            if 'Strong Binders (SB)' in binding_types and sb_count >= promiscuity_threshold: sb_peps.add(pep)
            if 'Weak Binders (WB)' in binding_types and wb_count >= promiscuity_threshold: wb_peps.add(pep)
            if 'Non-Binders (NB)' in binding_types and nb_count >= promiscuity_threshold: nb_peps.add(pep)
        else:
            if 'Strong Binders (SB)' in binding_types and sb_count > 0: sb_peps.add(pep)
            if 'Weak Binders (WB)' in binding_types and wb_count > 0: wb_peps.add(pep)
            if 'Non-Binders (NB)' in binding_types and nb_count > 0: nb_peps.add(pep)

    if not (sb_peps or wb_peps or nb_peps):
        st.warning("No peptides matched the selected criteria.")
        return False

    # 3. Get original headers for the final peptides
    peptide_to_header = {}
    pep_fasta_path = PROJECT_PATH / "peptide.fasta"
    if pep_fasta_path.exists():
        with open(pep_fasta_path, 'r') as f:
            curr_h = None
            for line in f:
                line = line.strip()
                if line.startswith(">"): curr_h = line[1:]
                elif curr_h and line in peptide_data: peptide_to_header[line] = curr_h
    
    def write_pep_fasta(pep_set, filename):
        if not pep_set: return 0
        path = INTERMEDIATE_DIR / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w') as f:
            for pep in sorted(list(pep_set)):
                header = peptide_to_header.get(pep, pep)
                f.write(f">{header}\n{pep}\n")
        return len(pep_set)
        
    c_sb = write_pep_fasta(sb_peps, "inputs/SB_peptides.fasta")
    c_wb = write_pep_fasta(wb_peps, "inputs/WB_peptides.fasta")
    c_nb = write_pep_fasta(nb_peps, "inputs/NB_peptides.fasta")
    
    master_fasta = INTERMEDIATE_DIR / "master_proteins.fasta"
    pc_sb, pc_wb, pc_nb = 0, 0, 0
    
    if master_fasta.exists() and (sb_peps or wb_peps or nb_peps):
        f_sb = open(INTERMEDIATE_DIR / "inputs" / "SB_proteins.fasta", 'w') if sb_peps else None
        f_wb = open(INTERMEDIATE_DIR / "inputs" / "WB_proteins.fasta", 'w') if wb_peps else None
        f_nb = open(INTERMEDIATE_DIR / "inputs" / "NB_proteins.fasta", 'w') if nb_peps else None
        
        seen_sb, seen_wb, seen_nb = set(), set(), set()
        
        with open(master_fasta, 'r') as f_in:
            pid, seq_lines = None, []
            def process_protein(p_id, p_seq_lines):
                nonlocal pc_sb, pc_wb, pc_nb
                full_seq = "".join(p_seq_lines)
                
                if f_sb and p_id not in seen_sb:
                    if any(pep in full_seq for pep in sb_peps):
                        f_sb.write(f">{p_id}\n{full_seq}\n")
                        seen_sb.add(p_id); pc_sb += 1
                
                if f_wb and p_id not in seen_wb:
                    if any(pep in full_seq for pep in wb_peps):
                        f_wb.write(f">{p_id}\n{full_seq}\n")
                        seen_wb.add(p_id); pc_wb += 1
                        
                if f_nb and p_id not in seen_nb:
                    if any(pep in full_seq for pep in nb_peps):
                        f_nb.write(f">{p_id}\n{full_seq}\n")
                        seen_nb.add(p_id); pc_nb += 1

            for line in f_in:
                line = line.strip()
                if line.startswith(">"):
                    if pid: process_protein(pid, seq_lines)
                    pid, seq_lines = line[1:].split()[0], []
                else: seq_lines.append(line)
            if pid: process_protein(pid, seq_lines)
            
        if f_sb: f_sb.close()
        if f_wb: f_wb.close()
        if f_nb: f_nb.close()

    # --- Generate Promiscuity / Binding Summary ---
    summary_rows = []
    for pep in (sb_peps | wb_peps | nb_peps):
        data = peptide_data[pep]
        
        levels = [lvl for lvl, p_set in [("SB", sb_peps), ("WB", wb_peps), ("NB", nb_peps)] if pep in p_set]
        
        summary_rows.append({
            "Peptide": pep,
            "Binding_Levels": ", ".join(levels),
            "SB_Allele_Count": len(data['SB_alleles']),
            "SB_Allele_List": ", ".join(sorted(data['SB_alleles'])),
            "WB_Allele_Count": len(data['WB_alleles']),
            "WB_Allele_List": ", ".join(sorted(data['WB_alleles'])),
            "NB_Allele_Count": len(data['NB_alleles']),
            "NB_Allele_List": ", ".join(sorted(data['NB_alleles'])),
        })
    if summary_rows:
        summary_path = INTERMEDIATE_DIR / "inputs" / "mhc_filtered_summary.csv"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        # Sort by the sum of allele counts to bring most promiscuous to top in raw file
        df_summary = pd.DataFrame(summary_rows)
        df_summary['Total_Events'] = df_summary['SB_Allele_Count'] + df_summary['WB_Allele_Count']
        df_summary = df_summary.sort_values(by="Total_Events", ascending=False).drop(columns=['Total_Events'])
        df_summary.insert(0, 'S.No.', range(1, 1 + len(df_summary)))
        df_summary.to_csv(summary_path, index=False)

    if c_sb > 0: st.success(f"✅ Generated `inputs/SB_peptides.fasta` ({c_sb}) and `inputs/SB_proteins.fasta` ({pc_sb})")
    if c_wb > 0: st.success(f"✅ Generated `inputs/WB_peptides.fasta` ({c_wb}) and `inputs/WB_proteins.fasta` ({pc_wb})")
    if c_nb > 0: st.success(f"✅ Generated `inputs/NB_peptides.fasta` ({c_nb}) and `inputs/NB_proteins.fasta` ({pc_nb})")

    # Save filter choices to metadata
    config.update_project_metadata(PROJECT_PATH, "mhc_filtering",
        inputs={"mhc_tools": mhc_tools},
        outputs={
            "summary_csv": str(summary_path),
            "SB_peptides": str(INTERMEDIATE_DIR / "inputs" / "SB_peptides.fasta"),
            "WB_peptides": str(INTERMEDIATE_DIR / "inputs" / "WB_peptides.fasta"),
            "NB_peptides": str(INTERMEDIATE_DIR / "inputs" / "NB_peptides.fasta"),
            "SB_proteins": str(INTERMEDIATE_DIR / "inputs" / "SB_proteins.fasta"),
            "WB_proteins": str(INTERMEDIATE_DIR / "inputs" / "WB_proteins.fasta"),
            "NB_proteins": str(INTERMEDIATE_DIR / "inputs" / "NB_proteins.fasta"),
        },
        parameters={
            "binding_types": list(binding_types),
            "mhc_sources": list(mhc_tools),
            "use_promiscuity": use_promiscuity,
            "promiscuity_threshold": promiscuity_threshold,
            "counts": {"SB_peptides": c_sb, "WB_peptides": c_wb, "NB_peptides": c_nb}
        }
    )

    return True

# Sidebar Navigation
with st.sidebar:
    st.markdown('<div class="sidebar-logo">VEDA PIPELINE</div>', unsafe_allow_html=True)
    
    step = st.radio(
        "Pipeline Steps",
        [
            "1. Upload & Filter",
            "2. NetMHCpan",
            "3. NetMHCIIpan",
            "4. GibbsCluster",
            "5. DeepLoc2",
            # "6. DeepTMHMM",  # Temporarily disabled
            "7. Vaxign-ML",
            "8. AlgPred",
            "9. ToxinPred",
            "10. IL2Pred",
            "11. IFNEpitope",
            # "12. Dashboard",  # Temporarily disabled
            "13. Publication Figures"
        ],
        key="pipeline_step_radio",
        label_visibility="collapsed"
    )

# ---------------------------------------------------------
# STEP 1: Upload & Filter
# ---------------------------------------------------------
if step == "1. Upload & Filter":
    st.header("Step 1: Upload & Filter")

    auto_load_errs = []
    # 1. Load previous state from file if session is empty
    if st.session_state['protein_df'] is None and st.session_state['filtered_df'] is None and PROJECT_CONFIG.exists():
        try:
            with open(PROJECT_CONFIG, 'r') as f:
                data = json.load(f)
            
            # Check if we have an uploaded file recorded
            if "uploaded_filename" in data:
                original_file = UPLOAD_DIR / data["uploaded_filename"]
                if original_file.exists():
                    input_type = data.get("input_type", "fragpipe")
                    
                    if input_type == "fasta":
                        # FASTA Load
                        with open(original_file, "rb") as f:
                            df = parse_fasta_content(f.read())
                        st.session_state['filtered_df'] = df
                        st.session_state['protein_df'] = df
                        st.session_state['peptide_count'] = len(df)
                        st.session_state['selected_species_list'] = data.get("selected_organisms_keywords", [])
                        safe_rerun()
                    else:
                        # FragPipe Load
                        base_df, logs, stats = proteins.load_and_filter_fragpipe(
                            original_file, data.get("selected_organisms_keywords", [])
                        )
                        
                        if base_df is not None:
                            st.session_state['filtered_df'] = base_df.copy()
                            st.session_state['peptide_count'] = stats.get('n_peptides', 0)
                            st.session_state['selected_species_list'] = data.get("selected_organisms_keywords", [])
                            
                            # If we ALSO have fetched sequences, restore the full protein_df
                            if "fetched_sequences" in data:
                                base_df['Sequence'] = base_df['UniProtID'].map(data["fetched_sequences"])
                                st.session_state['protein_df'] = base_df.dropna(subset=['Sequence'])
                            
                            safe_rerun()
                        else:
                            for msg in logs:
                                if "❌" in msg: auto_load_errs.append(msg)
        except Exception as e:
            st.warning(f"Could not auto-load project state: {e}")

    # Display Project Metadata (File Name, Size, Species)
    if PROJECT_CONFIG.exists():
        try:
            with open(PROJECT_CONFIG, 'r') as f:
                meta = json.load(f)
            
            if "uploaded_filename" in meta:
                f_name = meta["uploaded_filename"]
                f_path = UPLOAD_DIR / f_name
                f_size_str = "Unknown"
                if f_path.exists():
                    sz = f_path.stat().st_size
                    f_size_str = f"{sz/1024:.2f} KB" if sz < 1024*1024 else f"{sz/(1024*1024):.2f} MB"
                
                species_list = meta.get("selected_organisms_keywords", [])
                species_disp = ", ".join(species_list) if species_list else "None"
                st.info(f"📂 **Active File:** `{f_name}` | 📏 **Size:** `{f_size_str}` | 🧬 **Species:** `{species_disp}`")
        except Exception:
            pass

    # 2. File Upload UI
    st.subheader("Input Data")
    input_source = st.radio("Source Type:", ["FragPipe Output", "Protein FASTA"], horizontal=True)

    if input_source == "Protein FASTA":
        uploaded_file = st.file_uploader("Upload Protein FASTA", type=["fasta", "fa"], key="fasta_uploader")
        if uploaded_file:
            if st.button("✅ Process FASTA", use_container_width=True):
                with st.spinner("Processing FASTA..."):
                    # Save File
                    save_path = UPLOAD_DIR / uploaded_file.name
                    with open(save_path, "wb") as f: f.write(uploaded_file.getbuffer())
                    
                    # Parse
                    df = parse_fasta_content(uploaded_file.getvalue())
                    timestamp = datetime.datetime.now().isoformat()
                    
                    # Update Config
                    cfg = {
                        "uploaded_filename": uploaded_file.name, 
                        "input_type": "fasta", 
                        "upload_timestamp": timestamp,
                        "selected_organisms_keywords": [],
                        "found_species_in_input": ["FASTA (Unknown)"],
                        "validation_status": "success",
                        "validation_timestamp": timestamp,
                        "validation_logs": ["✅ FASTA processed successfully."]
                    }
                    with open(PROJECT_CONFIG, 'w') as f: json.dump(cfg, f, indent=4)
                    
                    # Update State
                    st.session_state['filtered_df'] = df
                    st.session_state['protein_df'] = df
                    st.session_state['peptide_count'] = len(df)
                    
                    # Generate peptide.fasta (copy of input for FASTA mode)
                    pep_fasta = PROJECT_PATH / "peptide.fasta"
                    with open(pep_fasta, "wb") as f: f.write(uploaded_file.getvalue())
                    
                    config.update_project_metadata(PROJECT_PATH, "upload_and_filter",
                        inputs={"filename": uploaded_file.name},
                        outputs={"n_sequences": len(df)},
                        parameters={"type": "fasta"}
                    )

                    st.success(f"✅ Loaded {len(df)} sequences.")
                    time.sleep(1)
                    safe_rerun()
        
        # Set validation status for downstream logic
        is_validated = st.session_state.get('protein_df') is not None

    else:
        # FragPipe Logic
        uploaded_file = st.file_uploader("Upload FragPipe Output (TSV/Excel)", type=["tsv", "xlsx", "csv"], key="fragpipe_uploader")
        
        # Species Selector
        species_labels = list(config.PipelineSettings.SPECIES_MAP.keys())
        
        if 'selected_species_list' not in st.session_state:
            st.session_state.selected_species_list = []

        def add_species():
            val = st.session_state.get("species_adder")
            if val and val != "Select species..." and val not in st.session_state.selected_species_list:
                st.session_state.selected_species_list.append(val)
            # Reset to placeholder
            st.session_state.species_adder = "Select species..."

        available_species = ["Select species..."] + [s for s in species_labels if s not in st.session_state.selected_species_list]
        st.selectbox("Filter Target Species:", available_species, key="species_adder", on_change=add_species)

        # Custom Species Input
        c_cust1, c_cust2 = st.columns([3, 1])
        with c_cust1:
            st.text_input("Or add custom species keyword (e.g., PIG):", key="custom_species_input")
        with c_cust2:
            st.markdown("<div style='margin-top: 28px;'></div>", unsafe_allow_html=True)
            if st.button("Add Custom"):
                val = st.session_state.get("custom_species_input", "").strip().upper()
                if val and val not in st.session_state.selected_species_list:
                    st.session_state.selected_species_list.append(val)
                    safe_rerun()

        if st.session_state.selected_species_list:
            st.markdown(f"**Selected Species:** {', '.join(st.session_state.selected_species_list)}")
            if st.button("Clear Selection", key="clear_species"):
                st.session_state.selected_species_list = []
                safe_rerun()

        selected_labels = st.session_state.selected_species_list
        selected_keywords = [config.PipelineSettings.SPECIES_MAP.get(k, k) for k in selected_labels]

        # Determine validation status
        is_validated = st.session_state.get('filtered_df') is not None

        # Action Buttons
        if uploaded_file:
            col_val, col_gen = st.columns([1, 1])
            
            with col_val:
                if st.button("✅ Validate & Filter", use_container_width=True):
                    if not selected_labels:
                        st.error("Please select a species.")
                    else:
                        with st.spinner("Processing..."):
                            # Save File
                            save_path = UPLOAD_DIR / uploaded_file.name
                            with open(save_path, "wb") as f:
                                f.write(uploaded_file.getbuffer())
                            
                            # Update Config
                            try:
                                with open(PROJECT_CONFIG, 'r') as f: cfg = json.load(f)
                            except (FileNotFoundError, json.JSONDecodeError): 
                                cfg = {}
                            cfg["uploaded_filename"] = uploaded_file.name
                            cfg["input_type"] = "fragpipe"
                            cfg["selected_organisms_keywords"] = selected_keywords
                            with open(PROJECT_CONFIG, 'w') as f: json.dump(cfg, f, indent=4)

                            # Run Logic
                            df, logs, stats = proteins.load_and_filter_fragpipe(save_path, selected_keywords)
                            for msg in logs:
                                if "❌" in msg: st.error(msg)
                                else: st.success(msg)
                            
                            st.session_state['filtered_df'] = df
                            st.session_state['peptide_count'] = stats.get('n_peptides', 0)
                            
                            config.update_project_metadata(PROJECT_PATH, "upload_and_filter", 
                                inputs={"filename": uploaded_file.name}, 
                                outputs={"n_peptides": stats.get('n_peptides'), "n_proteins": stats.get('n_proteins')},
                                parameters={"species_keywords": selected_keywords}
                            )
                            safe_rerun()

            with col_gen:
                if st.button("📄 Generate peptide.fasta", use_container_width=True, disabled=not is_validated):
                    with st.spinner("Generating peptide.fasta..."):
                        # Save File (ensure it exists)
                        save_path = UPLOAD_DIR / uploaded_file.name
                        with open(save_path, "wb") as f:
                            f.write(uploaded_file.getbuffer())
                        
                        output_fasta = PROJECT_PATH / "peptide.fasta"
                        # Use selected_keywords for filtering to match the pipeline context
                        success, msg = proteins.generate_peptide_fasta(save_path, output_fasta, selected_keywords)
                        
                        if success:
                            st.success(msg)
                        else:
                            st.error(msg)
            
            if is_validated:
                st.success("✅ File has been validated and filtered.")
                
                df_val = st.session_state.get('filtered_df')
                if df_val is not None:
                    n_prots = len(df_val)
                    n_total_peps = 0
                    n_unique_peps = 0
                    
                    if 'Initial_Peptides' in df_val.columns:
                        all_peps = []
                        for peps in df_val['Initial_Peptides'].dropna():
                            if isinstance(peps, list):
                                all_peps.extend(peps)
                        
                        valid_peps = [str(p).strip() for p in all_peps if p is not None and str(p).strip() and str(p).lower() != 'nan']
                        n_total_peps = len(valid_peps)
                        n_unique_peps = len(set(valid_peps))
                    else:
                        n_total_peps = n_prots
                        n_unique_peps = n_prots

                    st.markdown("#### 📊 Target Species Statistics")
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Total Peptides", n_total_peps, help="Total peptides matched in target species")
                    c2.metric("Unique Peptides", n_unique_peps, help="Unique peptide sequences")
                    c3.metric("Proteins to Fetch", n_prots, help="Unique UniProt IDs to fetch")
                    st.markdown("<br>", unsafe_allow_html=True)

        # Display errors from auto-load (e.g. if previous filter settings returned 0 results)
        if auto_load_errs:
            for err in auto_load_errs:
                st.error(err)

    # 3. Fetch Sequences UI
    # Case A: We have filtered data, but no sequences yet
    if (uploaded_file or is_validated) and st.session_state['protein_df'] is None:
        st.divider()
        if is_validated:
            n_peps = st.session_state.get('peptide_count', 0)
            st.info(f"Ready to fetch sequences for {len(st.session_state['filtered_df'])} **unique** proteins (consolidated from {n_peps} total peptide rows).")
            
            # Start Button
            if not st.session_state['is_fetching']:
                if st.button("Fetch Sequences from UniProt"):
                    st.session_state['is_fetching'] = True
                    st.session_state['fetch_progress_idx'] = 0
                    st.session_state['fetch_started'] = True
                    st.session_state['fetched_seq_cache'] = {}
                    safe_rerun()
        else:
            st.info("Please validate and filter the file to enable sequence fetching.")
            st.button("Fetch Sequences from UniProt", disabled=True)

        # Fetching Loop (Chunked)
        if st.session_state['is_fetching']:
            df_all = st.session_state['filtered_df']
            total_prots = len(df_all)
            current_idx = st.session_state['fetch_progress_idx']
            BATCH_SIZE = 5  # Small batch size for responsiveness
            
            # Progress UI
            st.divider()
            st.markdown("### ⏳ Fetching in progress...")
            pbar = st.progress(min(current_idx / total_prots, 1.0))
            status_col, stop_col = st.columns([3, 1])
            
            with stop_col:
                if st.button("🛑 Stop Fetching"):
                    st.session_state['is_fetching'] = False
                    st.warning("Fetching stopped by user. Saving partial results...")
                    time.sleep(1)
                    safe_rerun()

            # Check if done
            if current_idx >= total_prots:
                st.session_state['is_fetching'] = False
                st.success("Fetching complete!")
                time.sleep(1)
                safe_rerun()

            # Process Batch
            batch_df = df_all.iloc[current_idx : current_idx + BATCH_SIZE]
            
            # Callback for intra-batch updates (Speed & Logs)
            def batch_callback(count, speed, uid):
                # count is relative to batch
                overall = current_idx + count
                with status_col:
                    st.write(f"**Fetching:** {overall}/{total_prots} | **ID:** `{uid}`")
                    st.write(f"**Network Speed:** `{speed:.2f} KB/s`")

            # Run Batch
            batch_res, logs, cancelled, failed = proteins.fetch_sequences_batch(
                batch_df, progress_callback=batch_callback
            )
            
            # Cache Results
            if 'Sequence' in batch_res.columns:
                new_seqs = pd.Series(batch_res.Sequence.values, index=batch_res.UniProtID).to_dict()
                st.session_state['fetched_seq_cache'].update(new_seqs)
            
            # Update Index and Rerun
            st.session_state['fetch_progress_idx'] += BATCH_SIZE
            safe_rerun()

        # Finalization (When fetching is done or stopped)
        if not st.session_state['is_fetching'] and st.session_state.get('fetch_started'):
            st.session_state['fetch_started'] = False
            
            # Apply cached sequences to main dataframe
            full_df = st.session_state['filtered_df'].copy()
            full_df['Sequence'] = full_df['UniProtID'].map(st.session_state['fetched_seq_cache'])
            
            # Filter only those we successfully fetched
            final_df = full_df.dropna(subset=['Sequence'])
            del full_df
            import gc; gc.collect()
            
            if final_df.empty:
                st.error("❌ No sequences were successfully fetched. The Protein IDs might not be valid UniProt accessions (e.g., custom genome IDs). Please use the 'Protein FASTA' upload mode instead.")
                if st.button("Try Again"):
                    st.session_state['fetched_seq_cache'] = {}
                    safe_rerun()
                st.stop()

            # Save Results
            sequences_dict = pd.Series(final_df['Sequence'].values, index=final_df['UniProtID']).to_dict()
            
            initial_peptides_dict = {}
            if 'Initial_Peptides' in final_df.columns:
                initial_peptides_dict = pd.Series(final_df['Initial_Peptides'].values, index=final_df['UniProtID']).to_dict()

            with open(PROJECT_CONFIG, 'r') as f: cfg = json.load(f)
            cfg["fetched_sequences"] = sequences_dict
            if initial_peptides_dict: cfg["initial_peptides"] = initial_peptides_dict
            with open(PROJECT_CONFIG, 'w') as f: json.dump(cfg, f, indent=4)

            st.session_state['protein_df'] = final_df
            st.session_state['fetched_seq_cache'] = {}
            safe_rerun()

    # Case B: We have the final protein dataframe
    if st.session_state['protein_df'] is not None:
        st.divider()
        
        # Try to read timestamp from project_data.json
        last_updated = ""
        if PROJECT_DATA.exists():
            try:
                with open(PROJECT_DATA, 'r') as f: pdata = json.load(f)
                if "fetch_sequences" in pdata:
                    ts = pdata["fetch_sequences"].get("timestamp", "")
                    if ts: last_updated = f" (Last Run: {ts.split('T')[0]})"
            except (FileNotFoundError, json.JSONDecodeError):
                pass

        st.success(f"✅ Sequences Ready: {len(st.session_state['protein_df'])} proteins loaded.{last_updated}")
        
        # --- Dataset Statistics ---
        n_proteins = len(st.session_state['protein_df'])
        n_peptides = 0
        min_len = 0
        max_len = 0
        
        df_for_stats = st.session_state.get('filtered_df')
        if df_for_stats is not None and 'Initial_Peptides' in df_for_stats.columns:
            all_peps = set()
            for peps in df_for_stats['Initial_Peptides'].dropna():
                if isinstance(peps, list):
                    all_peps.update(peps)
            
            if all_peps:
                valid_peps = [str(p).strip() for p in all_peps if p is not None and str(p).strip() and str(p).lower() != 'nan']
                n_peptides = len(valid_peps)
                lengths = [len(p) for p in valid_peps]
                if lengths:
                    min_len, max_len = min(lengths), max(lengths)
        else:
            n_peptides = n_proteins
            if 'Sequence' in st.session_state['protein_df'].columns:
                lengths = st.session_state['protein_df']['Sequence'].dropna().astype(str).str.strip().apply(len)
                if not lengths.empty:
                    min_len, max_len = lengths.min(), lengths.max()

        st.markdown("#### 📊 Dataset Statistics")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Proteins", n_proteins)
        c2.metric("Unique Peptides", n_peptides)
        c3.metric("Min Peptide Length", min_len)
        c4.metric("Max Peptide Length", max_len)
        st.markdown("<br>", unsafe_allow_html=True)

        # SAVE MASTER FASTA (Required for Step 2)
        master_fasta = INTERMEDIATE_DIR / "master_proteins.fasta"
        with open(master_fasta, "w") as f:
            for _, row in st.session_state['protein_df'].iterrows():
                f.write(f">{row['UniProtID']}\n{row['Sequence']}\n")
        
        config.update_project_metadata(
            PROJECT_PATH, 
            "fetch_sequences", 
            inputs={"count": len(st.session_state['protein_df'])}, 
            outputs={"fasta_path": str(master_fasta)}
        )

        st.markdown("#### 📂 File Status")
        show_file_info("Master Proteins FASTA", master_fasta)

        with st.expander("🔄 Re-fetch Sequences"):
            st.warning("This will overwrite existing sequences.")
            
            # 1. Start Button
            if not st.session_state['is_refetching']:
                if st.button("Fetch from UniProt Again"):
                    st.session_state['is_refetching'] = True
                    st.session_state['fetch_progress_idx'] = 0
                    st.session_state['refetch_started'] = True
                    st.session_state['refetch_seq_cache'] = {}
                    safe_rerun()

            # 2. Re-fetching Loop
            if st.session_state['is_refetching']:
                df_target = st.session_state.get('filtered_df')
                if df_target is None:
                    df_target = st.session_state['protein_df'].drop(columns=['Sequence'], errors='ignore')

                total_prots = len(df_target)
                current_idx = st.session_state['fetch_progress_idx']
                BATCH_SIZE = 5

                st.markdown("### ⏳ Re-fetching in progress...")
                pbar = st.progress(min(current_idx / total_prots, 1.0))
                status_col, stop_col = st.columns([3, 1])

                with stop_col:
                    if st.button("🛑 Stop"):
                        st.session_state['is_refetching'] = False
                        st.warning("Re-fetching stopped. Saving partial results...")
                        time.sleep(1)
                        safe_rerun()

                # Check completion
                if current_idx >= total_prots:
                    st.session_state['is_refetching'] = False
                    st.success("Re-fetch complete!")
                    safe_rerun()

                # Process Batch
                batch_df = df_target.iloc[current_idx : current_idx + BATCH_SIZE]
                
                def batch_callback(count, speed, uid):
                    overall = current_idx + count
                    with status_col:
                        st.write(f"**Fetching:** {overall}/{total_prots} | **ID:** `{uid}`")
                        st.write(f"**Network Speed:** `{speed:.2f} KB/s`")

                batch_res, logs, cancelled, failed = proteins.fetch_sequences_batch(
                    batch_df, progress_callback=batch_callback
                )

                if 'Sequence' in batch_res.columns:
                    new_seqs = pd.Series(batch_res.Sequence.values, index=batch_res.UniProtID).to_dict()
                    st.session_state['refetch_seq_cache'].update(new_seqs)
                
                st.session_state['fetch_progress_idx'] += BATCH_SIZE
                safe_rerun()

            # 3. Finalize / Save (Only if we have a cache and not fetching)
            if not st.session_state['is_refetching'] and st.session_state.get('refetch_started'):
                st.session_state['refetch_started'] = False
                
                # Determine target DF again to map sequences
                df_target = st.session_state.get('filtered_df')
                if df_target is None:
                    df_target = st.session_state['protein_df'].drop(columns=['Sequence'], errors='ignore')
                
                full_df = df_target.copy()
                full_df['Sequence'] = full_df['UniProtID'].map(st.session_state['refetch_seq_cache'])
                final_df = full_df.dropna(subset=['Sequence'])
                del full_df
                import gc; gc.collect()
                
                if final_df.empty:
                    st.error("❌ No sequences were successfully fetched. The Protein IDs might not be valid UniProt accessions.")
                    if st.button("Acknowledge"):
                        st.session_state['refetch_seq_cache'] = {}
                        safe_rerun()
                    st.stop()

                sequences_dict = pd.Series(final_df.Sequence.values, index=final_df.UniProtID).to_dict()
                
                initial_peptides_dict = {}
                if 'Initial_Peptides' in final_df.columns:
                    initial_peptides_dict = pd.Series(final_df['Initial_Peptides'].values, index=final_df['UniProtID']).to_dict()

                with open(PROJECT_CONFIG, 'r') as f: cfg = json.load(f)
                cfg["fetched_sequences"] = sequences_dict
                if initial_peptides_dict: cfg["initial_peptides"] = initial_peptides_dict
                with open(PROJECT_CONFIG, 'w') as f: json.dump(cfg, f, indent=4)

                st.session_state['protein_df'] = final_df
                # Clear cache to prevent re-saving on next reload
                st.session_state['refetch_seq_cache'] = {}
                safe_rerun()
        
        st.divider()
        _, col_proceed = st.columns([3, 2])
        with col_proceed:
            st.button("➡️ Proceed to NetMHCpan", on_click=set_step, args=("2. NetMHCpan",), use_container_width=True)

# ---------------------------------------------------------
# STEP 2: NetMHCpan
# ---------------------------------------------------------
elif step == "2. NetMHCpan":
    st.header("Step 2: NetMHCpan")
    st.markdown("T-cell Epitope Discovery (MHC Class I Binding Prediction)")
        
    # 1. Species Filter
    all_species = list(config.NETMHCPAN_SPECIES_ALLELES.keys())
    st.markdown("**1. Filter by Target Species (Available Alleles)**")
    selected_species = []
    sp_cols = st.columns(5)
    for i, sp in enumerate(all_species):
        allele_count = len(config.NETMHCPAN_SPECIES_ALLELES.get(sp, []))
        if sp_cols[i % 5].checkbox(f"{sp} ({allele_count})", key=f"sp_chk_{i}"):
            selected_species.append(sp)
    
    # 2. Filter Available Alleles
    available_alleles = []
    for s in selected_species:
        available_alleles.extend(config.NETMHCPAN_SPECIES_ALLELES.get(s, []))
        
    if "selected_alleles_i" not in st.session_state: st.session_state.selected_alleles_i = []
    def select_all_alleles_i(): st.session_state.selected_alleles_i = available_alleles
    def clear_all_alleles_i(): st.session_state.selected_alleles_i = []
    col_sel, col_clr, _ = st.columns([1, 1, 4])
    with col_sel: st.button("Select All Visible", on_click=select_all_alleles_i, use_container_width=True, key="mhc1_select")
    with col_clr: st.button("Clear Selection", on_click=clear_all_alleles_i, use_container_width=True, key="mhc1_clear")
    
    selected_alleles = st.multiselect("2. Select Target Alleles (MHC-I)", available_alleles, key="selected_alleles_i")
    st.markdown(f"**Selected Alleles:** `{len(selected_alleles)}`")
    
    max_cpus = os.cpu_count() or 4
    c1, c2 = st.columns(2)
    with c1: num_threads = st.slider("Number of Threads", 1, max_cpus, min(4, max_cpus), key="mhc1_threads")
    with c2: pep_length = st.slider("Peptide Max Window Size", 8, 25, 9, key="mhc1_length", help="Sequences longer than this will be chopped into sliding windows. Shorter sequences are evaluated as-is. (Default: 9)")
    
    target_fasta = PROJECT_PATH / "peptide.fasta"
    res_dir = INTERMEDIATE_DIR / "netmhcpan"
    json_out = res_dir / "netmhc_results.json"
    log_file = res_dir / "NetMHCpan.log"
    status_file = res_dir / "status.txt"
    stop_file = res_dir / "STOP"
    
    st.markdown("#### 📂 File Status")
    show_file_info("Input Peptides", target_fasta)
    show_file_info("Result JSON", json_out)
    show_file_info("Log File", log_file)

    if (res_dir / "netmhc_out.txt").exists() and not json_out.exists():
        with st.spinner("Processing results..."):
            pipeline.process_netmhc_results(res_dir)
            st.session_state['netmhcpan_complete'] = True; safe_rerun()
    if json_out.exists(): st.session_state['netmhcpan_complete'] = True

    # Check Status
    status_content = "idle"
    if status_file.exists():
        with open(status_file, "r") as f: status_content = f.read().strip()
    
    # Parse status and check for stale PID (fixes auto-start after crash)
    is_running = False
    is_finalizing = False
    if status_content.startswith("running") or status_content.startswith("finalizing"):
        try:
            pid = int(status_content.split(":")[1])
            if pid == os.getpid():
                if status_content.startswith("running"):
                    is_running = True
                else:
                    is_finalizing = True
            else:
                status_content = "stale" # PID mismatch means server restarted
        except (ValueError, IndexError):
            status_content = "stale" # Old format or corrupted

    if status_content == "completed":
        st.session_state['netmhcpan_complete'] = True
    
    if is_running or is_finalizing:
        if is_finalizing:
            st.info("⏳ Finalizing results and generating stats...")
        else:
            st.info("⏳ NetMHCpan is running...")
        
        # Parse Progress
        parts = status_content.split(":")
        if len(parts) >= 4:
            try:
                completed = int(parts[2])
                total = int(parts[3])
                pending = total - completed
                
                parts_per_batch = parts[4] if len(parts) >= 5 else "N/A"
                
                if is_finalizing:
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Total Files", total)
                    c2.metric("Processed", completed)
                    c3.metric("Pending", pending)
                else:
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Total Batches", total)
                    c2.metric("Parts per Batch", parts_per_batch)
                    c3.metric("Completed", completed)
                    c4.metric("Pending", pending)
                if total > 0: st.progress(completed / total)
            except ValueError: pass

        if is_running and st.button("🛑 Stop NetMHCpan"):
            with open(stop_file, "w") as f: f.write("stop")
            st.warning("Stopping...")
        
        if log_file.exists():
            with st.expander("View Live Log", expanded=True):
                with open(log_file, "r") as f: 
                    lines = f.readlines()
                    # Show last 50 lines to prevent UI lag
                    content = "".join(lines[-50:]) if len(lines) > 50 else "".join(lines)
                    st.code(content)
        time.sleep(2)
        safe_rerun()

    elif status_content == "stale":
        st.warning("⚠️ Detected interrupted run from previous session. Resetting state.")
        with open(status_file, "w") as f: f.write("stopped")
        safe_rerun()

    else:
        # Check for partial results
        partial_files = list(res_dir.glob("batch_*.json")) if res_dir.exists() else []
        has_partial = len(partial_files) > 0 and not json_out.exists()

        if has_partial:
            st.info("📋 **Resume Analysis?**")
            st.markdown(f"""
            We found **{len(partial_files)} completed batches** from a previous run.
            
            *   **Resume Run:** Continue processing from the last successful batch.
            *   **Start Fresh:** Delete previous results and start over.
            """)
            c_resume, c_reset = st.columns([1, 1])
            do_resume = c_resume.button("▶️ Resume Run", key="mhc1_resume", use_container_width=True)
            do_reset = c_reset.button("🔄 Start Fresh", key="mhc1_reset", use_container_width=True)
            
            if do_resume or do_reset:
                if not selected_alleles: st.error("Please select at least one allele.")
                elif not target_fasta.exists(): st.error("'peptide.fasta' not found.")
                else:
                    if do_reset:
                        if res_dir.exists(): shutil.rmtree(res_dir)
                        res_dir.mkdir(parents=True, exist_ok=True)
                    
                    # Ensure clean fasta exists
                    clean_fasta = res_dir / "input_clean.fasta"
                    if not clean_fasta.exists():
                        res_dir.mkdir(parents=True, exist_ok=True)
                        if pipeline.clean_fasta_by_length(target_fasta, clean_fasta, min_length=8) == 0:
                            st.error("No valid sequences >= 8 AA found.")
                            st.stop()

                    # Start background thread
                    import threading
                    t = threading.Thread(target=run_batched_mhc, args=("NetMHCpan", clean_fasta, res_dir, selected_alleles, "human", 90, num_threads, pep_length))
                    t.start()
                    safe_rerun()
        else:
            run_btn_label = "🔄 Re-run NetMHCpan" if st.session_state.get('netmhcpan_complete') else "🚀 Run NetMHCpan"
            if st.button(run_btn_label, key="mhc1_run"):
                if not selected_alleles: st.error("Please select at least one allele.")
                elif not target_fasta.exists(): st.error("'peptide.fasta' not found. Please complete Step 1.")
                else:
                    if res_dir.exists(): shutil.rmtree(res_dir)
                    res_dir.mkdir(parents=True, exist_ok=True)
                    
                    clean_fasta, count, _ = filter_fasta_by_length(target_fasta, res_dir, min_len=8)
                    if count == 0:
                        st.error("No valid sequences >= 8 AA found.")
                    else:
                        # Start background thread
                        import threading
                        t = threading.Thread(target=run_batched_mhc, args=("NetMHCpan", clean_fasta, res_dir, selected_alleles, "human", 90, num_threads, pep_length))
                        t.start()
                        safe_rerun()

    if st.session_state.get('netmhcpan_complete') and json_out.exists():
        # Check file size to prevent OOM during rendering
        file_size_mb = json_out.stat().st_size / (1024 * 1024)
        st.success("✅ NetMHCpan Results Available.")
        display_netmhc_stats("netmhcpan", json_out, res_dir, parameters={"alleles": selected_alleles, "threads": num_threads, "length": pep_length})

    st.divider()
    _, col_proceed = st.columns([3, 2])
    with col_proceed:
        st.button("➡️ Proceed to NetMHCIIpan", on_click=set_step, args=("3. NetMHCIIpan",), use_container_width=True)

# ---------------------------------------------------------
# STEP 3: NetMHCIIpan
# ---------------------------------------------------------
elif step == "3. NetMHCIIpan":
    st.header("Step 3: NetMHCIIpan")
    st.markdown("T-cell Epitope Discovery (MHC Class II Binding Prediction)")
        
    # 1. Species Filter
    all_species_ii = list(config.NETMHCIIPAN_SPECIES_ALLELES.keys())
    st.markdown("**1. Filter by Target Species (Available Alleles)**")
    selected_species_ii = []
    sp_cols_ii = st.columns(5)
    for i, sp in enumerate(all_species_ii):
        allele_count = len(config.NETMHCIIPAN_SPECIES_ALLELES.get(sp, []))
        if sp_cols_ii[i % 5].checkbox(f"{sp} ({allele_count})", key=f"sp_chk_ii_{i}"):
            selected_species_ii.append(sp)

    # 2. Filter Available Alleles
    available_alleles_ii = []
    for s in selected_species_ii:
        available_alleles_ii.extend(config.NETMHCIIPAN_SPECIES_ALLELES.get(s, []))

    if "selected_alleles_ii" not in st.session_state: st.session_state.selected_alleles_ii = []
    def select_all_alleles_ii(): st.session_state.selected_alleles_ii = available_alleles_ii
    def clear_all_alleles_ii(): st.session_state.selected_alleles_ii = []
    
    col_sel, col_clr, _ = st.columns([1, 1, 4])
    with col_sel: st.button("Select All Visible", on_click=select_all_alleles_ii, use_container_width=True, key="mhc2_select")
    with col_clr: st.button("Clear Selection", on_click=clear_all_alleles_ii, use_container_width=True, key="mhc2_clear")
    
    selected_alleles_ii = st.multiselect("2. Select Target Alleles (MHC-II)", available_alleles_ii, key="selected_alleles_ii")
    st.markdown(f"**Selected Alleles:** `{len(selected_alleles_ii)}`")

    max_cpus = os.cpu_count() or 4
    c1, c2 = st.columns(2)
    with c1: num_threads_ii = st.slider("Number of Threads", 1, max_cpus, min(4, max_cpus), key="mhc2_threads")
    with c2: pep_length_ii = st.slider("Peptide Max Window Size", 13, 25, 15, key="mhc2_length", help="Sequences longer than this will be chopped into sliding windows. Shorter sequences are evaluated as-is. (Default: 15)")

    # Setup Paths
    target_fasta = PROJECT_PATH / "peptide.fasta"
    res_dir_ii = INTERMEDIATE_DIR / "netmhciipan"
    json_out_ii = res_dir_ii / "netmhc_results.json"
    log_file_ii = res_dir_ii / "NetMHCIIpan.log"
    status_file_ii = res_dir_ii / "status.txt"
    stop_file_ii = res_dir_ii / "STOP"

    st.markdown("#### 📂 File Status")
    show_file_info("Input Peptides", target_fasta)
    show_file_info("Result JSON", json_out_ii)
    show_file_info("Log File", log_file_ii)

    # Auto-load results
    if (res_dir_ii / "netmhc_out.txt").exists() and not json_out_ii.exists():
        with st.spinner("Processing results..."):
            pipeline.process_netmhc_results(res_dir_ii)
            st.session_state['netmhciipan_complete'] = True; safe_rerun()
    if json_out_ii.exists(): st.session_state['netmhciipan_complete'] = True

    # Check Status
    status_content = "idle"
    if status_file_ii.exists():
        with open(status_file_ii, "r") as f: status_content = f.read().strip()
    
    is_running = False
    is_finalizing = False
    if status_content.startswith("running") or status_content.startswith("finalizing"):
        try:
            pid = int(status_content.split(":")[1])
            if pid == os.getpid():
                if status_content.startswith("running"):
                    is_running = True
                else:
                    is_finalizing = True
            else:
                status_content = "stale"
        except:
            status_content = "stale"

    if status_content == "completed":
        st.session_state['netmhciipan_complete'] = True

    if is_running or is_finalizing:
        if is_finalizing:
            st.info("⏳ Finalizing results and generating stats...")
        else:
            st.info("⏳ NetMHCIIpan is running...")
        
        # Parse Progress
        parts = status_content.split(":")
        if len(parts) >= 4:
            try:
                completed = int(parts[2])
                total = int(parts[3])
                pending = total - completed
                
                parts_per_batch = parts[4] if len(parts) >= 5 else "N/A"
                
                if is_finalizing:
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Total Files", total)
                    c2.metric("Processed", completed)
                    c3.metric("Pending", pending)
                else:
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Total Batches", total)
                    c2.metric("Parts per Batch", parts_per_batch)
                    c3.metric("Completed", completed)
                    c4.metric("Pending", pending)
                if total > 0: st.progress(completed / total)
            except: pass

        if is_running and st.button("🛑 Stop NetMHCIIpan"):
            with open(stop_file_ii, "w") as f: f.write("stop")
            st.warning("Stopping...")
        
        st.markdown("### 📜 Live Logs")
        log_container = st.container()
        if log_file_ii.exists():
            with log_container:
                with open(log_file_ii, "r") as f: 
                    lines = f.readlines()
                    # Show last 50 lines to prevent UI lag
                    content = "".join(lines[-50:]) if len(lines) > 50 else "".join(lines)
                    st.code(content, language="bash")
        time.sleep(2)
        safe_rerun()
    
    elif status_content == "stale":
        st.warning("⚠️ Detected interrupted run from previous session. Resetting state.")
        with open(status_file_ii, "w") as f: f.write("stopped")
        safe_rerun()

    else:
        # Check for partial results
        partial_files = list(res_dir_ii.glob("batch_*.json")) if res_dir_ii.exists() else []
        has_partial = len(partial_files) > 0 and not json_out_ii.exists()
        
        if has_partial:
            st.info("📋 **Resume Analysis?**")
            st.markdown(f"""
            We found **{len(partial_files)} completed batches** from a previous run.
            
            *   **Resume Run:** Continue processing from the last successful batch.
            *   **Start Fresh:** Delete previous results and start over.
            """)
            c_resume, c_reset = st.columns([1, 1])
            do_resume = c_resume.button("▶️ Resume Run", key="mhc2_resume", use_container_width=True)
            do_reset = c_reset.button("🔄 Start Fresh", key="mhc2_reset", use_container_width=True)
            
            if do_resume or do_reset:
                if not selected_alleles_ii: st.error("Please select at least one allele.")
                elif not target_fasta.exists(): st.error("'peptide.fasta' not found.")
                else:
                    if do_reset:
                        if res_dir_ii.exists(): shutil.rmtree(res_dir_ii)
                        res_dir_ii.mkdir(parents=True, exist_ok=True)
                    
                    # Ensure clean fasta exists
                    clean_fasta = res_dir_ii / "input_clean.fasta"
                    if not clean_fasta.exists():
                        res_dir_ii.mkdir(parents=True, exist_ok=True)
                        if pipeline.clean_fasta_by_length(target_fasta, clean_fasta, min_length=9) == 0:
                            st.error("No valid sequences >= 9 AA found.")
                            st.stop()

                    import threading
                    t = threading.Thread(target=run_batched_mhc, args=("NetMHCIIpan", clean_fasta, res_dir_ii, selected_alleles_ii, "human", 90, num_threads_ii, pep_length_ii))
                    t.start()
                    safe_rerun()
        else:
            run_btn_label_ii = "🔄 Re-run NetMHCIIpan" if st.session_state.get('netmhciipan_complete') else "🚀 Run NetMHCIIpan"
            if st.button(run_btn_label_ii, key="mhc2_run"):
                if not selected_alleles_ii: st.error("Please select at least one allele.")
                elif not target_fasta.exists(): st.error("'peptide.fasta' not found. Please complete Step 1.")
                else:
                    if res_dir_ii.exists(): shutil.rmtree(res_dir_ii)
                    res_dir_ii.mkdir(parents=True, exist_ok=True)
                    
                    clean_fasta, count, _ = filter_fasta_by_length(target_fasta, res_dir_ii, min_len=9)
                    if count == 0:
                        st.error("No valid sequences >= 9 AA found.")
                    else:
                        # Start background thread
                        import threading
                        t = threading.Thread(target=run_batched_mhc, args=("NetMHCIIpan", clean_fasta, res_dir_ii, selected_alleles_ii, "human", 90, num_threads_ii, pep_length_ii))
                        t.start()
                        safe_rerun()

    if st.session_state.get('netmhciipan_complete') and json_out_ii.exists():
        file_size_mb = json_out_ii.stat().st_size / (1024 * 1024)
        st.success("✅ NetMHCIIpan Results Available.")
        display_netmhc_stats("netmhciipan", json_out_ii, res_dir_ii, parameters={"alleles": selected_alleles_ii, "threads": num_threads_ii, "length": pep_length_ii})

    st.divider()
    _, col_proceed = st.columns([3, 2])
    with col_proceed:
        st.button("➡️ Proceed to GibbsCluster", on_click=set_step, args=("4. GibbsCluster",), use_container_width=True)

# ---------------------------------------------------------
# STEP 4: GibbsCluster
# ---------------------------------------------------------
elif step == "4. GibbsCluster":
    st.header("Step 4: GibbsCluster")
    st.markdown("Clustering of peptides based on sequence motifs.")

    pep_options = [f for f in ["inputs/SB_peptides.fasta", "inputs/WB_peptides.fasta", "inputs/NB_peptides.fasta", "peptide.fasta"] if (PROJECT_PATH / f).exists() or (INTERMEDIATE_DIR / f).exists()]
    if pep_options:
        selected_input = st.selectbox("Select Input Peptides:", pep_options, key="gibbs_input")
        input_fasta = INTERMEDIATE_DIR / selected_input
    else:
        input_fasta = INTERMEDIATE_DIR / "peptide.fasta"

    if not input_fasta.exists(): st.warning("⚠️ Input FASTA not found. Please complete Step 1.")
    else:
        st.info(f"Using input: {input_fasta.name}")
        out_dir = INTERMEDIATE_DIR / "gibbscluster"
        log_file = out_dir / "gibbscluster.log"
        stop_file = out_dir / "STOP"
        status_file = out_dir / "status.txt"
        
        status_content = open(status_file).read().strip() if status_file.exists() else "idle"
        is_running = status_content == "running"
        is_completed = status_content.startswith("completed")
        
        st.markdown("#### 📂 File Status")
        show_file_info("Input", input_fasta)
        show_file_info("Output Dir", out_dir)
        show_file_info("Log File", log_file)

        c1, c2 = st.columns(2)
        with c1: n_clusters = st.slider("Number of Clusters", 1, 10, 5, key="gibbs_clusters")
        with c2: motif_len = st.slider("Motif Length", 8, 20, 9, key="gibbs_len")
        
        if is_running:
            st.info("⏳ GibbsCluster is running...")
            if st.button("🛑 Stop GibbsCluster"):
                with open(stop_file, "w") as f: f.write("stop")
                st.warning("Stop signal sent.")
            if log_file.exists():
                with st.expander("Live Logs", expanded=True):
                    with open(log_file, "r") as f: st.code("".join(f.readlines()[-50:]))
            time.sleep(2); safe_rerun()
        else:
            run_btn_label_gibbs = "🔄 Re-run GibbsCluster" if is_completed else "🚀 Run GibbsCluster"
            if st.button(run_btn_label_gibbs):
                launch_background_worker(status_file, stop_file, gibbscluster_worker.run, peptide_file=input_fasta, output_dir=out_dir, clusters=n_clusters, motif_len=motif_len, stop_file=stop_file, log_file=log_file)
                safe_rerun()

        if is_completed:
            elapsed = status_content.split(":")[1] if ":" in status_content else "N/A"
            st.success("✅ GibbsCluster Results Available.")
            st.caption(f"⏱️ Elapsed Time: {elapsed} seconds")

            # --- Show GibbsCluster Results ---
            all_out_files = sorted(list(out_dir.rglob("*.out")))
            if all_out_files:
                st.markdown("---")
                st.subheader("🧩 GibbsCluster Results")
                st.markdown("The optimal number of clusters is a suggestion. Inspect all solutions below.")

                with st.expander("Result File Status", expanded=False):
                    for f in all_out_files:
                        show_file_info(f.name, f)

                # Interactive table viewer for .ds.out files
                ds_out_files = [f for f in all_out_files if f.name.endswith(".ds.out")]
                if ds_out_files:
                    st.markdown("#### Tabular Clustering Solution")
                    file_map = {f.name: f for f in ds_out_files}
                    selected_file_name = st.selectbox("Select Solution to View:", list(file_map.keys()), key="gibbs_solution_select")

                    if selected_file_name:
                        file_path = file_map[selected_file_name]
                        try:
                            df = pd.read_csv(file_path, sep=r'\s+', comment='#', header=None)
                            std_cols = ["Gn", "Num", "Sequence", "Core", "of", "IP", "IL", "DP", "DL", "Annotation", "Self", "bgG", "bgScore", "cScore"]
                            
                            if len(df.columns) == len(std_cols):
                                df.columns = std_cols
                            elif len(df.columns) == len(std_cols) - 1:
                                df.columns = [c for c in std_cols if c != "Annotation"]
                            
                            st.dataframe(df, use_container_width=True)
                        except Exception as e:
                            st.error(f"Error parsing file: {e}")

            else:
                st.warning("GibbsCluster completed, but no result files (*.out) were found.")

            config.update_project_metadata(PROJECT_PATH, "gibbscluster", inputs={"input_file": str(input_fasta)}, outputs={"output_dir": str(out_dir)}, parameters={"clusters": n_clusters, "motif_len": motif_len, "elapsed_seconds": elapsed})
        elif status_content == "failed":
            st.error("❌ GibbsCluster failed. Check logs.")

    st.divider()
    _, col_proceed = st.columns([3, 2])
    with col_proceed:
        st.button("➡️ Proceed to DeepLoc2", on_click=set_step, args=("5. DeepLoc2",), use_container_width=True)

# ---------------------------------------------------------
# STEP 5: DeepLoc2
# ---------------------------------------------------------
elif step == "5. DeepLoc2":
    st.header("Step 5: DeepLoc2")
    st.markdown("Predicts subcellular localization using DeepLoc 2.0.")

    # --- Mandatory MHC Result Filtering ---
    mhc1_res = INTERMEDIATE_DIR / "netmhcpan" / "netmhc_results.json"
    mhc2_res = INTERMEDIATE_DIR / "netmhciipan" / "netmhc_results.json"
    mhc_results_exist = mhc1_res.exists() or mhc2_res.exists()
    
    # Check if filtered files already exist
    sb_pep = INTERMEDIATE_DIR / "inputs" / "SB_peptides.fasta"
    wb_pep = INTERMEDIATE_DIR / "inputs" / "WB_peptides.fasta"
    nb_pep = INTERMEDIATE_DIR / "inputs" / "NB_peptides.fasta"
    filtered_files_exist = sb_pep.exists() or wb_pep.exists() or nb_pep.exists()
    
    can_proceed = not mhc_results_exist or filtered_files_exist

    if mhc_results_exist and not filtered_files_exist:
        st.info("MHC prediction results found. Please generate filtered peptide/protein sets before proceeding.")
        
        with st.expander("Generate Peptide & Protein Sets from MHC Results", expanded=True):
            binding_types = st.multiselect("1. Select Binding Types to Fetch", ['Strong Binders (SB)', 'Weak Binders (WB)', 'Non-Binders (NB)'], default=['Strong Binders (SB)', 'Weak Binders (WB)', 'Non-Binders (NB)'], key="mhc_filter_types")
            
            default_sources = [src for src, path in [('NetMHCpan', mhc1_res), ('NetMHCIIpan', mhc2_res)] if path.exists()]
            mhc_sources = st.multiselect("2. Select MHC Result Source(s)", ['NetMHCpan', 'NetMHCIIpan'], default=default_sources, key="mhc_filter_sources")
            
            use_promiscuity = st.checkbox("Fetch only promiscuous binders (peptides binding to multiple alleles)", key="mhc_filter_promiscuity")
            promiscuity_threshold = 1
            if use_promiscuity:
                promiscuity_threshold = st.number_input("Minimum number of alleles a peptide must bind to:", min_value=2, value=2, step=1, key="mhc_filter_prom_thresh")

            if st.button("Generate Filtered Files", key="mhc_filter_generate"):
                if not binding_types or not mhc_sources:
                    st.error("Please select at least one binding type and one MHC source.")
                else:
                    with st.spinner("Generating filtered files..."):
                        success = generate_mhc_filtered_sets(binding_types, mhc_sources, use_promiscuity, promiscuity_threshold)
                        if success:
                            st.session_state['mhc_filtered_files_generated'] = True
                            time.sleep(1); safe_rerun()
        
        if st.session_state.get('mhc_filtered_files_generated', False):
            can_proceed = True
            
    elif mhc_results_exist and filtered_files_exist:
        st.success("✅ MHC filtered files already exist. Proceeding to DeepLoc2...")
        
        summary_file = INTERMEDIATE_DIR / "inputs" / "mhc_filtered_summary.csv"
        if summary_file.exists():
            with st.expander("📊 View Promiscuity / Binding Summary", expanded=True):
                st.markdown("Filter the summary table to inspect peptides based on their binding strength and promiscuity.")
                
                try:
                    df_sum = pd.read_csv(summary_file)
                except Exception as e:
                    st.error(f"Could not read summary file: {e}")
                    df_sum = pd.DataFrame()

                if not df_sum.empty:
                    filter_choice = st.radio(
                        "Filter by Binding Level:",
                        ("Strong Binders (SB)", "Weak Binders (WB)", "Non-Binders (NB)"),
                        horizontal=True,
                        key="promiscuity_filter"
                    )

                    if filter_choice == "Strong Binders (SB)":
                        count_col, list_col = 'SB_Allele_Count', 'SB_Allele_List'
                    elif filter_choice == "Weak Binders (WB)":
                        count_col, list_col = 'WB_Allele_Count', 'WB_Allele_List'
                    else: # Non-Binders
                        count_col, list_col = 'NB_Allele_Count', 'NB_Allele_List'
                    
                    if count_col in df_sum.columns and list_col in df_sum.columns:
                        df_filtered = df_sum[df_sum[count_col] > 0].copy()
                        
                        if not df_filtered.empty:
                            # Sort by binding events (descending) and re-assign S.No. starting from 1
                            df_filtered = df_filtered.sort_values(by=count_col, ascending=False).reset_index(drop=True)
                            # Drop existing S.No. if present, then add fresh one
                            if 'S.No.' in df_filtered.columns:
                                df_filtered = df_filtered.drop(columns=['S.No.'])
                            df_filtered.insert(0, 'S.No.', range(1, 1 + len(df_filtered)))
                            
                            display_cols = ['Peptide', count_col, list_col]
                            if 'S.No.' in df_filtered.columns:
                                display_cols.insert(0, 'S.No.')
                            
                            df_display = df_filtered[display_cols].rename(columns={count_col: 'Binding Events', list_col: 'Alleles'})
                            st.dataframe(df_display, use_container_width=True, hide_index=True)
                        else:
                            st.info(f"No peptides found for the '{filter_choice}' category with the selected criteria.")
                    else:
                        st.warning(f"Summary file is missing required columns for '{filter_choice}'. Please regenerate the files.")

                    # Keep the download button for the full summary
                    with open(summary_file, "rb") as f:
                        st.download_button("📥 Download Full Summary CSV", f, file_name="mhc_binding_summary.csv", mime="text/csv")
    
    # --- DeepLoc2 Execution (Conditional) ---
    if can_proceed:
        st.divider()
        st.subheader("Run DeepLoc2")

        prot_options = [f for f in ["inputs/SB_proteins.fasta", "inputs/WB_proteins.fasta", "inputs/NB_proteins.fasta", "master_proteins.fasta"] if (PROJECT_PATH / f).exists() or (INTERMEDIATE_DIR / f).exists()]
        if prot_options:
            selected_input = st.selectbox("Select Input Proteins:", prot_options, key="deeploc_input")
            input_fasta = INTERMEDIATE_DIR / selected_input
        else:
            input_fasta = INTERMEDIATE_DIR / "inputs" / "SB_proteins.fasta" if (INTERMEDIATE_DIR / "inputs" / "SB_proteins.fasta").exists() else INTERMEDIATE_DIR / "master_proteins.fasta"
    else:
        st.markdown("---"); st.markdown("Complete the MHC result filtering above to proceed.")
        input_fasta = None

    dl_model = "Fast"
    dl_plot = False
    if can_proceed:
        c_mod, c_fmt = st.columns(2)
        with c_mod:
            dl_model_display = st.selectbox("Model:", ["High-throughput (Fast)", "High-quality (Slow)"], key="dl_model")
            dl_model = "Accurate" if "Slow" in dl_model_display else "Fast"
        with c_fmt:
            dl_format_display = st.selectbox("Output format:", ["Short output (no figures)", "Long output"], key="dl_format")
            dl_plot = True if "Long" in dl_format_display else False

    deeploc_dir = INTERMEDIATE_DIR / "deeploc2"
    log_file = deeploc_dir / "deeploc2.log"
    stop_file = deeploc_dir / "STOP"
    status_file = deeploc_dir / "status.txt"
    
    status_content = open(status_file).read().strip() if status_file.exists() else "idle"
    is_running = status_content == "running"
    is_completed = status_content.startswith("completed")
    
    # Debug info
    st.caption(f"DEBUG: status='{status_content}', is_completed={is_completed}, can_proceed={can_proceed}")
    
    st.markdown("#### 📂 File Status")
    if input_fasta: show_file_info("Input", input_fasta)
    show_file_info("Output Dir", deeploc_dir)
    show_file_info("Log File", log_file)

    if can_proceed:
        if is_running:
            st.info("⏳ DeepLoc2 is running...")
            if st.button("🛑 Stop DeepLoc2"):
                with open(stop_file, "w") as f: f.write("stop")
                st.warning("Stop signal sent.")
            if log_file.exists():
                with st.expander("Live Logs", expanded=True):
                    with open(log_file, "r") as f: st.code("".join(f.readlines()[-50:]))
            time.sleep(2); safe_rerun()
        else:
            run_label = "🔄 Re-run DeepLoc2" if is_completed else "🚀 Run DeepLoc2"
            if st.button(run_label, disabled=(not input_fasta or not input_fasta.exists())):
                launch_background_worker(status_file, stop_file, deeploc_worker.run, fasta_path=input_fasta, output_dir=deeploc_dir, model=dl_model, plot=dl_plot, log_file=log_file)
                safe_rerun()

        if is_completed:
            elapsed = status_content.split(":")[1] if ":" in status_content else "N/A"
            st.success("✅ DeepLoc2 Results Available.")
            st.caption(f"⏱️ Elapsed Time: {elapsed} seconds")
            res_file = deeploc_dir / "results.csv"
            show_file_info("Output CSV", res_file)
            if res_file.exists():
                try:
                    # Read CSV file (comma-separated)
                    df = pd.read_csv(res_file)
                    
                    # --- Statistical Summary ---
                    st.info("📊 **Result Summary:**")
                    
                    # Define all possible localizations and membrane types
                    all_localizations = ['Cytoplasm', 'Nucleus', 'Extracellular', 'Cell membrane', 
                                         'Mitochondrion', 'Plastid', 'Endoplasmic reticulum', 
                                         'Lysosome/Vacuole', 'Golgi apparatus', 'Peroxisome']
                    all_membrane_types = ['Peripheral', 'Transmembrane', 'Lipid anchor', 'Soluble']
                    
                    # Count each localization from the Localizations column
                    loc_counts = {loc: 0 for loc in all_localizations}
                    for locs in df['Localizations'].dropna():
                        for loc in all_localizations:
                            if loc in str(locs):
                                loc_counts[loc] += 1
                    
                    # Count each membrane type from the Membrane types column
                    mem_counts = {mt: 0 for mt in all_membrane_types}
                    for mtypes in df['Membrane types'].dropna():
                        for mt in all_membrane_types:
                            if mt in str(mtypes):
                                mem_counts[mt] += 1
                    
                    # Display localization counts
                    st.markdown("**Localizations:**")
                    cols = st.columns(5)
                    for i, (loc, count) in enumerate(loc_counts.items()):
                        cols[i % 5].metric(loc, count)
                    
                    # Display membrane type counts
                    st.markdown("**Membrane Types:**")
                    cols2 = st.columns(4)
                    for i, (mt, count) in enumerate(mem_counts.items()):
                        cols2[i % 4].metric(mt, count)
                    
                    # --- Detailed Results Table ---
                    st.markdown("---")
                    st.markdown("### 📋 Detailed Results")
                    
                    # Add Sr.no. column
                    df_display = df.copy()
                    df_display.insert(0, 'Sr.no.', range(1, 1 + len(df_display)))
                    
                    # Select only the key columns for display
                    display_cols = ['Sr.no.', 'Protein_ID', 'Localizations', 'Signals', 'Membrane types']
                    df_display = df_display[display_cols]
                    
                    st.dataframe(df_display, use_container_width=True, hide_index=True)
                    
                    config.update_project_metadata(PROJECT_PATH, "deeploc2",
                        inputs={"input_file": str(input_fasta)},
                        outputs={"output_csv": str(res_file)},
                        parameters={"elapsed_seconds": elapsed, "model": dl_model, "plot": dl_plot, "stats": {**loc_counts, **mem_counts}}
                    )
                except Exception as e:
                    import traceback
                    st.error(f"Error reading results: {e}")
                    st.code(traceback.format_exc())
        elif status_content == "failed":
            st.error("❌ DeepLoc2 failed. Check logs.")

    # --- Filter Proteins by Localization/Membrane Type ---
    if is_completed and res_file.exists():
        st.markdown("---")
        st.subheader("🧬 Filter Proteins for Vaxign-ML")
        st.markdown("Select localization and membrane type criteria to extract specific proteins:")
        
        # Define options
        all_localizations = ['Cytoplasm', 'Nucleus', 'Extracellular', 'Cell membrane', 
                             'Mitochondrion', 'Plastid', 'Endoplasmic reticulum', 
                             'Lysosome/Vacuole', 'Golgi apparatus', 'Peroxisome']
        all_membrane_types = ['Peripheral', 'Transmembrane', 'Lipid anchor', 'Soluble']
        
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Localizations:**")
            selected_locs = st.multiselect(
                "Select localizations (leave empty to select all):",
                all_localizations,
                key="filter_locs"
            )
        with col2:
            st.markdown("**Membrane Types:**")
            selected_mems = st.multiselect(
                "Select membrane types (leave empty to select all):",
                all_membrane_types,
                key="filter_mems"
            )
        
        # Filter mode: AND (both criteria must match) OR (either criteria matches)
        filter_mode = st.radio(
            "Filter Mode:",
            ["AND (match both localization AND membrane type)", "OR (match either localization OR membrane type)"],
            horizontal=True,
            key="filter_mode"
        )
        
        if st.button("📥 Extract Filtered Proteins", key="extract_filtered_proteins"):
            try:
                df = pd.read_csv(res_file)
                
                # Filter by localization
                if selected_locs:
                    mask_loc = df['Localizations'].apply(
                        lambda x: any(loc in str(x) for loc in selected_locs) if pd.notna(x) else False
                    )
                else:
                    mask_loc = pd.Series([True] * len(df), index=df.index)
                
                # Filter by membrane type
                if selected_mems:
                    mask_mem = df['Membrane types'].apply(
                        lambda x: any(mem in str(x) for mem in selected_mems) if pd.notna(x) else False
                    )
                else:
                    mask_mem = pd.Series([True] * len(df), index=df.index)
                
                # Combine filters based on mode
                if "AND" in filter_mode:
                    df_filtered = df[mask_loc & mask_mem].copy()
                else:
                    df_filtered = df[mask_loc | mask_mem].copy()
                
                if not df_filtered.empty:
                    # Get protein IDs
                    protein_ids = df_filtered['Protein_ID'].tolist()
                    
                    # Extract proteins from master_proteins.fasta
                    master_fasta = INTERMEDIATE_DIR / "master_proteins.fasta"
                    filtered_proteins_fasta = INTERMEDIATE_DIR / "inputs" / "deeploc_filtered_proteins.fasta"
                    
                    count = 0
                    with open(filtered_proteins_fasta, 'w') as out_f:
                        if master_fasta.exists():
                            with open(master_fasta, 'r') as in_f:
                                pid, seq = None, []
                                for line in in_f:
                                    line = line.strip()
                                    if line.startswith(">"):
                                        if pid and pid in protein_ids:
                                            out_f.write(f">{pid}\n{''.join(seq)}\n")
                                            count += 1
                                        pid = line[1:].split()[0]
                                        seq = []
                                    else:
                                        seq.append(line)
                                # Last entry
                                if pid and pid in protein_ids:
                                    out_f.write(f">{pid}\n{''.join(seq)}\n")
                                    count += 1
                        else:
                            st.error("master_proteins.fasta not found!")
                            count = -1
                    
                    if count > 0:
                        st.success(f"✅ Extracted {count} proteins to `inputs/deeploc_filtered_proteins.fasta`")
                        # Save filter choices to metadata
                        config.update_project_metadata(PROJECT_PATH, "deeploc_filter",
                            inputs={"source_file": str(res_file), "master_fasta": str(master_fasta)},
                            outputs={"filtered_fasta": str(filtered_proteins_fasta)},
                            parameters={
                                "selected_localizations": selected_locs,
                                "selected_membrane_types": selected_mems,
                                "filter_mode": filter_mode,
                                "extracted_count": count
                            }
                        )
                    else:
                        st.warning("No proteins matched the selected criteria.")
                else:
                    st.warning("No proteins matched the selected criteria.")
            except Exception as e:
                st.error(f"Error extracting proteins: {e}")

    st.divider()
    _, col_proceed = st.columns([3, 2])
    with col_proceed:
        st.button("➡️ Proceed to Vaxign-ML", on_click=set_step, args=("7. Vaxign-ML",), use_container_width=True)

# ---------------------------------------------------------
# STEP 6: DeepTMHMM (Temporarily Disabled)
# ---------------------------------------------------------
elif step == "6. DeepTMHMM":
    st.warning("⚠️ DeepTMHMM is currently disabled.")
    if st.button("➡️ Skip to Vaxign-ML"):
        set_step("7. Vaxign-ML")
        safe_rerun()

# ---------------------------------------------------------
# STEP 7: Vaxign-ML
# ---------------------------------------------------------
elif step == "7. Vaxign-ML":
    st.header("Step 7: Vaxign-ML (Antigenicity)")
    st.markdown("Predicts protective antigens from a set of candidate peptides.")

    st.subheader("Run Vaxign-ML")
    
    prot_options = [f for f in ["inputs/deeploc_filtered_proteins.fasta", "inputs/SB_proteins.fasta", "inputs/WB_proteins.fasta", "inputs/NB_proteins.fasta", "master_proteins.fasta"] if (PROJECT_PATH / f).exists() or (INTERMEDIATE_DIR / f).exists()]
    if prot_options:
        selected_input = st.selectbox("Select Input Proteins:", prot_options, key="vaxign_input")
        input_fasta = INTERMEDIATE_DIR / selected_input
    else:
        input_fasta = INTERMEDIATE_DIR / "inputs" / "deeploc_filtered_proteins.fasta" if (INTERMEDIATE_DIR / "inputs" / "deeploc_filtered_proteins.fasta").exists() else INTERMEDIATE_DIR / "master_proteins.fasta"
    
    if not input_fasta.exists():
        st.warning("⚠️ Input FASTA not found. Please run an earlier step to generate filtered protein files.")
    else:
        st.info(f"Using input: `{input_fasta.name}`")
        
        try:
            found_ambiguous = set()
            with open(input_fasta, 'r') as f:
                for line in f:
                    if not line.startswith(">"):
                        line = line.upper()
                        for aa in ['U', 'B', 'Z', 'J', 'X', 'O']:
                            if aa in line:
                                found_ambiguous.add(aa)
            if found_ambiguous:
                st.warning(f"⚠️ **Sanitization Notice:** Found non-standard amino acids (**{', '.join(sorted(found_ambiguous))}**) in `{input_fasta.name}`. They will be automatically replaced with standard equivalents (e.g. U → C, X → A) during the run to prevent Vaxign-ML errors.")
        except Exception:
            pass

        organism_selection = st.radio("Select Organism Type:", ["Gram-positive Bacteria (gram+)", "Gram-negative Bacteria (gram-)", "Virus (virus)"])
        sudo_password = st.text_input("Sudo Password (for Docker permissions):", type="password")

        vaxign_dir = INTERMEDIATE_DIR / "vaxign"
        log_file = vaxign_dir / "vaxign.log"
        stop_file = vaxign_dir / "STOP"
        status_file = vaxign_dir / "status.txt"
        
        expected_result = vaxign_dir / f"{input_fasta.stem}.result.tsv" if input_fasta else None
        
        status_content = open(status_file).read().strip() if status_file.exists() else "idle"
        is_running = status_content == "running"
        is_completed = status_content.startswith("completed")
        
        st.markdown("#### 📂 File Status")
        show_file_info("Input", input_fasta)
        show_file_info("Output Dir", vaxign_dir)
        show_file_info("Log File", log_file)

        if is_running:
            st.info("⏳ Vaxign-ML is running...")
            if st.button("🛑 Stop Vaxign-ML"):
                with open(stop_file, "w") as f: f.write("stop")
                st.warning("Stop signal sent.")
            if log_file.exists():
                with st.expander("Live Logs", expanded=True):
                    with open(log_file, "r") as f: st.code("".join(f.readlines()[-50:]))
            time.sleep(2); safe_rerun()
        else:
            run_label = "🔄 Re-run Vaxign-ML" if is_completed else "🚀 Run Vaxign-ML"
            if st.button(run_label):
                org_flag = {"Gram-positive": "g+", "Gram-negative": "g-"}.get(organism_selection.split(" ")[0], "virus")
                launch_background_worker(status_file, stop_file, vaxign_worker.run, fasta_path=input_fasta, output_dir=vaxign_dir, organism_type=org_flag, sudo_password=sudo_password if sudo_password else None, progress_callback=None, log_file=log_file, threads=15)
                safe_rerun()

        if is_completed:
            elapsed = status_content.split(":")[1] if ":" in status_content else "N/A"
            st.success("✅ Vaxign-ML Results Available.")
            st.caption(f"⏱️ Elapsed Time: {elapsed} seconds")
            show_file_info("Result File", expected_result)
            if expected_result and expected_result.exists():
                try:
                    df = pd.read_csv(expected_result, sep='\t')
                    df.columns = df.columns.str.strip().str.lower()
                    if 'prediction' in df.columns:
                        df['prediction'] = pd.to_numeric(df['prediction'], errors='coerce').fillna(0)
                        n_protective = (df['prediction'] == 1).sum()
                        st.info(f"📊 **Result Summary:** Found {n_protective} Protective Antigens out of {len(df)} candidates.")
                        
                        # --- Filter Protective Antigens and Extract Peptides ---
                        st.markdown("---")
                        st.subheader("🧬 Extract SB Peptides from Protective Antigens")
                        st.markdown("Filter proteins with prediction=1 and extract their peptides that are Strong Binders (SB) from NetMHC.")
                        
                        protective_proteins = df[df['prediction'] == 1]['sample'].tolist() if 'sample' in df.columns else []
                        
                        if protective_proteins:
                            st.write(f"Found {len(protective_proteins)} protective proteins (prediction=1)")
                            
                            # Extract peptides from these proteins
                            master_pep_fasta = INTERMEDIATE_DIR / "master_peptides.fasta"
                            sb_pep_fasta = INTERMEDIATE_DIR / "inputs" / "SB_peptides.fasta"
                            output_pep_fasta = INTERMEDIATE_DIR / "inputs" / "vaxign_sb_peptides.fasta"
                            
                            # Get SB peptides set - also track which protein they belong to and preserve original headers
                            sb_peptides = set()  # peptide sequence -> protein_id mapping
                            sb_pep_to_protein = {}  # peptide sequence -> protein ID
                            sb_pep_headers = {}  # peptide sequence -> original header
                            if sb_pep_fasta.exists():
                                with open(sb_pep_fasta, 'r') as f:
                                    curr_pep = None
                                    curr_protein = None
                                    curr_header = None
                                    for line in f:
                                        line = line.strip()
                                        if line.startswith(">"):
                                            # Parse header like: >sp|P0DQD2|INLB_LISMOpep1
                                            curr_header = line[1:]  # Keep full header without >
                                            parts = line[1:].split('|')
                                            if len(parts) >= 2:
                                                curr_protein = parts[1]  # Get protein ID (second field)
                                            curr_pep = None
                                        else:
                                            if curr_pep is None and curr_protein:
                                                curr_pep = line
                                                sb_peptides.add(curr_pep)
                                                sb_pep_to_protein[curr_pep] = curr_protein
                                                sb_pep_headers[curr_pep] = curr_header
                                    if curr_pep and curr_protein:
                                        sb_peptides.add(curr_pep)
                                        sb_pep_to_protein[curr_pep] = curr_protein
                                        sb_pep_headers[curr_pep] = curr_header
                            
                            # Extract peptides from SB_peptides.fasta that belong to protective proteins
                            # Use the mapping we already built from SB_peptides.fasta
                            extracted_peptides = set()
                            for pep, prot in sb_pep_to_protein.items():
                                if prot in protective_proteins:
                                    extracted_peptides.add(pep)
                            
                            # Write output file with original headers
                            if extracted_peptides:
                                with open(output_pep_fasta, 'w') as f:
                                    for pep in sorted(extracted_peptides):
                                        header = sb_pep_headers.get(pep, f"peptide_{pep}")
                                        f.write(f">{header}\n{pep}\n")
                                st.success(f"✅ Extracted {len(extracted_peptides)} SB peptides from {len(protective_proteins)} protective proteins -> `inputs/vaxign_sb_peptides.fasta`")
                                
                                # Save to metadata
                                config.update_project_metadata(PROJECT_PATH, "vaxign_sb_extraction",
                                    inputs={"vaxign_result": str(expected_result), "sb_peptides": str(sb_pep_fasta)},
                                    outputs={"extracted_peptides": str(output_pep_fasta)},
                                    parameters={"protective_proteins_count": len(protective_proteins), "extracted_peptides_count": len(extracted_peptides)}
                                )
                            else:
                                st.warning("No SB peptides found in protective proteins. This may indicate that the SB peptides do not belong to the protective proteins.")
                        else:
                            st.warning("No proteins with prediction=1 found.")
                        
                        org_flag = {"Gram-positive": "g+", "Gram-negative": "g-"}.get(organism_selection.split(" ")[0], "virus")
                        config.update_project_metadata(PROJECT_PATH, "vaxign_ml",
                            inputs={"input_file": str(input_fasta)}, outputs={"output_file": str(expected_result)},
                            parameters={"organism": org_flag, "elapsed_seconds": elapsed, "protective_count": int(n_protective)}
                        )
                except Exception as e:
                    import traceback
                    st.error(f"Error processing results: {e}")
                    st.code(traceback.format_exc())
        elif status_content == "failed":
            st.error("❌ Vaxign-ML failed. Check logs.")

    st.divider()
    _, col_proceed = st.columns([3, 1])
    with col_proceed:
        st.button("➡️ Proceed to AlgPred", on_click=set_step, args=("8. AlgPred",), use_container_width=True)

# ---------------------------------------------------------
# STEP 8: AlgPred
# ---------------------------------------------------------
elif step == "8. AlgPred":
    st.header("Step 8: AlgPred (Allergenicity)")
    st.markdown("Checks if candidates are potential allergens.")

    # --- File Status ---
    # Use vaxign_sb_peptides.fasta as default (generated after Vaxign-ML with SB peptides from protective antigens)
    pep_options = [f for f in ["inputs/vaxign_sb_peptides.fasta", "inputs/vaxign_peptides.fasta", "inputs/SB_peptides.fasta", "inputs/WB_peptides.fasta", "inputs/NB_peptides.fasta", "peptide.fasta"] if (PROJECT_PATH / f).exists() or (INTERMEDIATE_DIR / f).exists()]
    if pep_options:
        selected_input = st.selectbox("Select Input Peptides:", pep_options, key="algpred_input")
        input_fasta = INTERMEDIATE_DIR / selected_input
    else:
        input_fasta = INTERMEDIATE_DIR / "inputs" / "vaxign_sb_peptides.fasta"

    st.markdown("#### 📂 File Status")
    alg_dir = INTERMEDIATE_DIR / "algpred"
    results_file = alg_dir / "algpred" / "algpred_results.csv"
    log_file = alg_dir / "algpred" / "algpred.log"
    status_file = alg_dir / "status.txt"
    
    status_content = open(status_file).read().strip() if status_file.exists() else "idle"
    is_running = status_content == "running"
    is_completed = status_content.startswith("completed")

    show_file_info("Input FASTA", input_fasta)
    show_file_info("Log File", log_file)

    # --- Safety Tool Execution ---
    st.subheader("Run Predictions")
    
    if not input_fasta.exists():
        st.info("Please generate the input FASTA file above to run safety checks.")
    else:
        col1, col2 = st.columns(2)
        with col1: threshold = st.slider("Threshold (Higher = More Specific)", 0.1, 1.0, 0.3, key="alg_thresh")
        with col2: model_type = st.selectbox("Model Type", [2, 1], format_func=lambda x: "Hybrid (BLAST+ARPs)" if x == 2 else "AAC (Fast)", key="alg_model")
        
        alg_dir.mkdir(parents=True, exist_ok=True)
        stop_file = alg_dir / "STOP"

        if is_running:
            st.info("⏳ AlgPred 2.0 is running...")
            if st.button("🛑 Stop AlgPred"):
                with open(stop_file, "w") as f: f.write("stop")
                st.warning("Stop signal sent.")
            if log_file.exists():
                with st.expander("Live Logs", expanded=True):
                    with open(log_file, "r") as f: st.code("".join(f.readlines()[-50:]))
            time.sleep(2); safe_rerun()
        else:
            run_label = "🔄 Re-run AlgPred 2.0" if is_completed else "🚀 Run AlgPred 2.0"
            if st.button(run_label):
                # Filter peptides (AlgPred generally handles >4 AA)
                filtered_fasta, valid_count, _ = filter_fasta_by_length(input_fasta, alg_dir, min_len=5)
                if valid_count == 0: st.error("No valid peptides (>4 AA) found.")
                else:
                    launch_background_worker(status_file, stop_file, pipeline.run_algpred, fasta_file=filtered_fasta, threshold=threshold, model=model_type)
                    safe_rerun()

        if is_completed:
            elapsed = status_content.split(":")[1] if ":" in status_content else "N/A"
            st.success("✅ AlgPred Results Available.")
            st.caption(f"⏱️ Elapsed Time: {elapsed} seconds")
            show_file_info("Result CSV", results_file)
            if results_file.exists():
                try:
                    import csv
                    total, n_safe = 0, 0
                    with open(results_file, 'r') as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            total += 1
                            if row.get('Prediction') == 'Non-Allergen': n_safe += 1
                    st.info(f"📊 **Result Summary:** Found {n_safe} Non-Allergenic peptides (Safe) out of {total}.")
                    config.update_project_metadata(PROJECT_PATH, "algpred",
                        inputs={"input_file": str(input_fasta)}, outputs={"result_file": str(results_file)},
                        parameters={"threshold": threshold, "model": model_type, "elapsed_seconds": elapsed, "safe_count": n_safe}
                    )
                except: pass
        elif status_content == "failed":
            st.error("❌ AlgPred failed. Check logs.")

    st.divider()
    _, col_proceed = st.columns([3, 2])
    with col_proceed:
        st.button("➡️ Proceed to ToxinPred", on_click=set_step, args=("9. ToxinPred",), use_container_width=True)

# ---------------------------------------------------------
# STEP 9: ToxinPred
# ---------------------------------------------------------
elif step == "9. ToxinPred":
    st.header("Step 9: ToxinPred (Toxicity)")
    st.markdown("Checks if candidates are potential toxins.")
    
    pep_options = [f for f in ["inputs/vaxign_sb_peptides.fasta", "inputs/vaxign_peptides.fasta", "inputs/SB_peptides.fasta", "inputs/WB_peptides.fasta", "inputs/NB_peptides.fasta", "peptide.fasta"] if (PROJECT_PATH / f).exists() or (INTERMEDIATE_DIR / f).exists()]
    if pep_options:
        selected_input = st.selectbox("Select Input Peptides:", pep_options, key="toxinpred_input")
        input_fasta = INTERMEDIATE_DIR / selected_input
    else:
        input_fasta = INTERMEDIATE_DIR / "vaxign_peptides.fasta"

    tox_dir = INTERMEDIATE_DIR / "toxinpred"
    log_file = tox_dir / "toxinpred" / "toxinpred.log"
    results_file = tox_dir / "toxinpred" / "toxinpred_results.csv"
    status_file = tox_dir / "status.txt"

    status_content = open(status_file).read().strip() if status_file.exists() else "idle"
    is_running = status_content == "running"
    is_completed = status_content.startswith("completed")

    st.markdown("#### 📂 File Status")
    show_file_info("Input FASTA", input_fasta)
    show_file_info("Log File", log_file)

    if not input_fasta.exists():
        st.warning("⚠️ Input FASTA not found. Please run an earlier step to generate it.")
    else:
        col1, col2 = st.columns(2)
        with col1: threshold = st.slider("Threshold", 0.0, 1.0, 0.6, help="Score is proportional to toxic potential.", key="tox_thresh")
        with col2: model_type = st.selectbox("Model Type", [1, 2], format_func=lambda x: "Model 1: AAC based RF" if x == 1 else "Model 2: Hybrid", key="tox_model")
        
        tox_dir.mkdir(parents=True, exist_ok=True)
        stop_file = tox_dir / "STOP"

        if is_running:
            st.info("⏳ ToxinPred 2.0 is running...")
            if st.button("🛑 Stop ToxinPred"):
                with open(stop_file, "w") as f: f.write("stop")
                st.warning("Stop signal sent.")
            if log_file.exists():
                with st.expander("Live Logs", expanded=True):
                    with open(log_file, "r") as f: st.code("".join(f.readlines()[-50:]))
            time.sleep(2); safe_rerun()
        else:
            run_label = "🔄 Re-run ToxinPred 2.0" if is_completed else "🚀 Run ToxinPred 2.0"
            if st.button(run_label):
                # Filter peptides
                filtered_fasta, valid_count, _ = filter_fasta_by_length(input_fasta, tox_dir, min_len=4)
                if valid_count == 0: st.error("No valid peptides found.")
                else:
                    launch_background_worker(status_file, stop_file, pipeline.run_toxinpred, fasta_file=filtered_fasta, threshold=threshold, model=model_type)
                    safe_rerun()

        if is_completed:
            elapsed = status_content.split(":")[1] if ":" in status_content else "N/A"
            st.success("✅ ToxinPred Results Available.")
            st.caption(f"⏱️ Elapsed Time: {elapsed} seconds")
            show_file_info("Result CSV", results_file)
            if results_file.exists():
                try:
                    import csv
                    total, n_safe = 0, 0
                    with open(results_file, 'r') as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            total += 1
                            if row.get('Prediction') == 'Non-Toxin': n_safe += 1
                    st.info(f"📊 **Result Summary:** Found {n_safe} Non-Toxic peptides (Safe) out of {total}.")
                    config.update_project_metadata(PROJECT_PATH, "toxinpred",
                        inputs={"input_file": str(input_fasta)}, outputs={"result_file": str(results_file)},
                        parameters={"threshold": threshold, "model": model_type, "elapsed_seconds": elapsed, "safe_count": n_safe}
                    )
                except: pass
        elif status_content == "failed":
            st.error("❌ ToxinPred failed. Check logs.")

    st.divider()
    _, col_proceed = st.columns([3, 2])
    with col_proceed:
        st.button("➡️ Proceed to IL2Pred", on_click=set_step, args=("10. IL2Pred",), use_container_width=True)

# ---------------------------------------------------------
# STEP 10: IL2Pred
# ---------------------------------------------------------
elif step == "10. IL2Pred":
    st.header("Step 10: IL2Pred")
    st.markdown("Predicts IL-2 inducing peptides.")

    # --- Input Generation ---
    with st.expander("Generate Input Peptides", expanded=True):
        st.markdown("Cytokine prediction will run on peptides that passed the **safety checks** in the previous step (non-allergenic and non-toxic).")
        algpred_res = INTERMEDIATE_DIR / "algpred" / "algpred" / "algpred_results.csv"
        toxinpred_res = INTERMEDIATE_DIR / "toxinpred" / "toxinpred" / "toxinpred_results.csv"
        cytokine_input_fasta = INTERMEDIATE_DIR / "inputs" / "safe_peptides.fasta"
        safe_proteins_fasta = INTERMEDIATE_DIR / "inputs" / "safe_proteins.fasta"
        can_generate = algpred_res.exists() and toxinpred_res.exists()

        if not can_generate:
            st.warning("⚠️ AlgPred or ToxinPred results not found. Please run them in Step 7 & 8.")

        if st.button("Generate Input FASTA from Safety Results", disabled=not can_generate):
            try:
                try:
                    import csv
                    non_allergens = set()
                    with open(algpred_res, 'r') as f:
                        reader = csv.DictReader(f)
                        alg_id_col = 'Subject' if 'Subject' in reader.fieldnames else 'ID'
                        for row in reader:
                            if row.get('Prediction') == 'Non-Allergen':
                                non_allergens.add(row.get(alg_id_col))
                except Exception as e:
                    st.error(f"❌ Error reading AlgPred results: {e}"); st.stop()
                
                try:
                    non_toxins = set()
                    with open(toxinpred_res, 'r') as f:
                        reader = csv.DictReader(f)
                        tox_id_col = 'ID' if 'ID' in reader.fieldnames else 'Subject'
                        for row in reader:
                            if row.get('Prediction') == 'Non-Toxin':
                                non_toxins.add(row.get(tox_id_col))
                except Exception as e:
                    st.error(f"❌ Error reading ToxinPred results: {e}"); st.stop()

                safe_subjects = non_allergens.intersection(non_toxins)

                if not safe_subjects:
                    st.error("No peptides were predicted as both non-allergenic and non-toxic.")
                else:
                    # Try multiple possible source files in order of priority
                    peptide_sources = [
                        INTERMEDIATE_DIR / "inputs" / "vaxign_peptides.fasta",
                        INTERMEDIATE_DIR / "inputs" / "vaxign_sb_peptides.fasta",
                        INTERMEDIATE_DIR / "inputs" / "SB_peptides.fasta",
                        INTERMEDIATE_DIR / "peptide.fasta",
                    ]
                    seq_map = {}
                    source_file = None
                    for src in peptide_sources:
                        if src.exists():
                            with open(src, 'r') as f:
                                header = None
                                for line in f:
                                    line = line.strip()
                                    if line.startswith('>'): header = line[1:]
                                    elif header: seq_map[header] = line; header = None
                            source_file = src
                            break
                    
                    if not source_file:
                        st.error("No peptide FASTA file found to extract sequences from."); st.stop()
                    
                    safe_pep_seqs = set()
                    with open(cytokine_input_fasta, 'w') as f:
                        count = 0
                        for subject in safe_subjects:
                            if subject in seq_map:
                                f.write(f">{subject}\n{seq_map[subject]}\n")
                                safe_pep_seqs.add(seq_map[subject])
                                count += 1
                        st.success(f"✅ Generated `{cytokine_input_fasta.name}` with {count} safe peptides.")

                    # Generate Safe Proteins (Proteins containing safe peptides)
                    prot_source = INTERMEDIATE_DIR / "inputs" / "SB_proteins.fasta"
                    if not prot_source.exists(): prot_source = INTERMEDIATE_DIR / "master_proteins.fasta"
                    
                    if prot_source.exists() and safe_pep_seqs:
                        prot_count = 0
                        with open(safe_proteins_fasta, 'w') as f_out, open(prot_source, 'r') as f_in:
                            p_header = None
                            p_seq = []
                            
                            def process_protein(h, s):
                                full_s = "".join(s)
                                # Check if protein contains any safe peptide
                                for pep in safe_pep_seqs:
                                    if pep in full_s:
                                        f_out.write(f">{h}\n{full_s}\n")
                                        return 1
                                return 0

                            for line in f_in:
                                line = line.strip()
                                if line.startswith(">"):
                                    if p_header: prot_count += process_protein(p_header, p_seq)
                                    p_header = line[1:]
                                    p_seq = []
                                else: p_seq.append(line)
                            if p_header: prot_count += process_protein(p_header, p_seq)
                        
                        if prot_count > 0:
                            st.success(f"✅ Generated `{safe_proteins_fasta.name}` with {prot_count} proteins containing safe peptides.")
                            show_file_info("Safe Proteins", safe_proteins_fasta)

            except Exception as e:
                st.error(f"❌ Failed to process safety results: {e}")
                traceback.print_exc()

    # --- Cytokine Tool Execution ---
    st.subheader("2. Run Predictions")
    pep_options = [f for f in ["inputs/safe_peptides.fasta", "inputs/vaxign_sb_peptides.fasta", "inputs/vaxign_peptides.fasta", "inputs/SB_peptides.fasta", "inputs/WB_peptides.fasta", "inputs/NB_peptides.fasta", "peptide.fasta"] if (PROJECT_PATH / f).exists() or (INTERMEDIATE_DIR / f).exists()]
    if pep_options:
        selected_input = st.selectbox("Select Input Peptides:", pep_options, key="il2pred_input")
        input_fasta = INTERMEDIATE_DIR / selected_input
    else:
        input_fasta = INTERMEDIATE_DIR / "safe_peptides.fasta"

    il2_dir = INTERMEDIATE_DIR / "il2pred"
    log_file = il2_dir / "il2pred" / "il2pred.log"
    results_file = il2_dir / "il2pred" / "il2pred_results.csv"
    status_file = il2_dir / "status.txt"

    status_content = open(status_file).read().strip() if status_file.exists() else "idle"
    is_running = status_content == "running"
    is_completed = status_content.startswith("completed")

    st.markdown("#### 📂 File Status")
    show_file_info("Input FASTA", input_fasta)
    if (INTERMEDIATE_DIR / "inputs" / "safe_proteins.fasta").exists():
        show_file_info("Safe Proteins", INTERMEDIATE_DIR / "inputs" / "safe_proteins.fasta")
    show_file_info("Log File", log_file)

    if not input_fasta.exists():
        st.info("Please generate the input FASTA file above to run cytokine predictions.")
    else:
        col1, col2, col3 = st.columns(3)
        with col1: threshold = st.slider("Threshold", 0.0, 1.0, 0.6, key="il2_thresh")
        with col2: job_type = st.selectbox("Job Type", [1, 2, 3], format_func=lambda x: {1: "IL-2 vs non-IL-2", 2: "IL-2 vs other cytokine", 3: "IL-2 vs random"}[x], key="il2_job")
        with col3: model_type = st.selectbox("Model", [1, 2], format_func=lambda x: "ET" if x == 1 else "Hybrid", key="il2_model")
        
        il2_dir.mkdir(parents=True, exist_ok=True)
        stop_file = il2_dir / "STOP"

        if is_running:
            st.info("⏳ IL2Pred is running...")
            if st.button("🛑 Stop IL2Pred"):
                with open(stop_file, "w") as f: f.write("stop")
                st.warning("Stop signal sent.")
            if log_file.exists():
                with st.expander("Live Logs", expanded=True):
                    with open(log_file, "r") as f: st.code("".join(f.readlines()[-50:]))
            time.sleep(2); safe_rerun()
        else:
            run_label = "🔄 Re-run IL2Pred" if is_completed else "🚀 Run IL2Pred"
            if st.button(run_label):
                filtered_fasta, valid_count, _ = filter_fasta_by_length(input_fasta, il2_dir)
                if valid_count == 0: st.error(f"No valid sequences found.")
                else:
                    launch_background_worker(status_file, stop_file, pipeline.run_il2pred, fasta_file=filtered_fasta, threshold=threshold, model=model_type, job=job_type)
                    safe_rerun()

        if is_completed:
            elapsed = status_content.split(":")[1] if ":" in status_content else "N/A"
            st.success("✅ IL2Pred Results Available.")
            st.caption(f"⏱️ Elapsed Time: {elapsed} seconds")
            show_file_info("Result CSV", results_file)
            if results_file.exists():
                try:
                    import csv
                    total, n_ind = 0, 0
                    with open(results_file, 'r') as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            total += 1
                            if row.get('Prediction') == 'il2 inducing': n_ind += 1
                    st.info(f"📊 **Result Summary:** Found {n_ind} IL-2 Inducers out of {total}.")
                    config.update_project_metadata(PROJECT_PATH, "il2pred",
                        inputs={"input_file": str(input_fasta)}, outputs={"result_file": str(results_file)},
                        parameters={"threshold": threshold, "model": model_type, "job": job_type, "elapsed_seconds": elapsed, "inducer_count": n_ind}
                    )
                except: pass
        elif status_content == "failed":
            st.error("❌ IL2Pred failed. Check logs.")

    st.divider()
    _, col_proceed = st.columns([3, 2])
    with col_proceed:
        st.button("➡️ Proceed to IFNEpitope", on_click=set_step, args=("11. IFNEpitope",), use_container_width=True)

# ---------------------------------------------------------
# STEP 11: IFNEpitope
# ---------------------------------------------------------
elif step == "11. IFNEpitope":
    st.header("Step 11: IFNEpitope")
    st.markdown("Predicts IFN-γ inducing epitopes.")
    
    pep_options = [f for f in ["inputs/safe_peptides.fasta", "inputs/vaxign_sb_peptides.fasta", "inputs/vaxign_peptides.fasta", "inputs/SB_peptides.fasta", "inputs/WB_peptides.fasta", "inputs/NB_peptides.fasta", "peptide.fasta"] if (PROJECT_PATH / f).exists() or (INTERMEDIATE_DIR / f).exists()]
    if pep_options:
        selected_input = st.selectbox("Select Input Peptides:", pep_options, key="ifn_input")
        input_fasta = INTERMEDIATE_DIR / selected_input
    else:
        input_fasta = INTERMEDIATE_DIR / "safe_peptides.fasta"

    ifn_dir = INTERMEDIATE_DIR / "ifnepitope"
    log_file = ifn_dir / "ifnepitope" / "ifnepitope.log"
    results_file = ifn_dir / "ifnepitope" / "ifnepitope_results.csv"
    status_file = ifn_dir / "status.txt"
    
    status_content = open(status_file).read().strip() if status_file.exists() else "idle"
    is_running = status_content == "running"
    is_completed = status_content.startswith("completed")

    st.markdown("#### 📂 File Status")
    show_file_info("Input FASTA", input_fasta)
    show_file_info("Log File", log_file)

    if not input_fasta.exists():
        st.warning("⚠️ Input FASTA not found. Please run an earlier step to generate it.")
    else:
        col1, col2, col3 = st.columns(3)
        with col1: ifn_job = st.selectbox("Job Type", [1, 3], format_func=lambda x: "1: Predict" if x == 1 else "3: Scan", key="ifn_job")
        with col2: ifn_host = st.selectbox("Host", [1, 2], format_func=lambda x: "Human" if x == 1 else "Mouse", key="ifn_host")
        with col3: ifn_thresh = st.slider("Threshold", 0.0, 1.0, 0.50, key="ifn_thresh")
        ifn_window = 8
        if ifn_job == 3: ifn_window = st.slider("Window Length (Scan Mode)", 8, 20, 8, key="ifn_win")
        
        ifn_dir.mkdir(parents=True, exist_ok=True)
        stop_file = ifn_dir / "STOP"

        if is_running:
            st.info("⏳ IFNEpitope is running...")
            if st.button("🛑 Stop IFNEpitope"):
                with open(stop_file, "w") as f: f.write("stop")
                st.warning("Stop signal sent.")
            if log_file.exists():
                with st.expander("Live Logs", expanded=True):
                    try:
                        with open(log_file, "r") as f: st.code("".join(f.readlines()[-50:]))
                    except: pass
            time.sleep(2); safe_rerun()
        else:
            run_label = "🔄 Re-run IFNEpitope" if is_completed else "🚀 Run IFNEpitope"
            if st.button(run_label):
                filtered_fasta, valid_count, _ = filter_fasta_by_length(input_fasta, ifn_dir, min_len=ifn_window)
                if valid_count == 0:
                    st.error(f"No valid peptides found (Must be >= {ifn_window} AA).")
                else:
                    launch_background_worker(status_file, stop_file, pipeline.run_ifnepitope, fasta_file=filtered_fasta, threshold=ifn_thresh, host=ifn_host, job=ifn_job, window=ifn_window)
                    safe_rerun()

        if is_completed:
            elapsed = status_content.split(":")[1] if ":" in status_content else "N/A"
            st.success("✅ IFNEpitope Results Available.")
            st.caption(f"⏱️ Elapsed Time: {elapsed} seconds")
            show_file_info("Result CSV", results_file)
            
            if results_file.exists():
                try:
                    import csv
                    total, ind = 0, 0
                    with open(results_file, 'r') as f:
                        reader = csv.DictReader(f)
                        for r in reader:
                            total += 1
                            if r.get('Prediction') == 'IFN-γ inducer': ind += 1
                    st.info(f"📊 **Result Summary:** Found {ind} IFN-γ Inducers out of {total}.")
                    config.update_project_metadata(PROJECT_PATH, "ifnepitope",
                        inputs={"input_file": str(input_fasta)}, outputs={"result_file": str(results_file)},
                        parameters={"threshold": ifn_thresh, "host": ifn_host, "job": ifn_job, "window": ifn_window, "elapsed_seconds": elapsed, "inducer_count": ind}
                    )
                except: pass
        elif status_content == "failed":
            st.error("❌ IFNEpitope failed. Check logs.")

    st.divider()
    _, col_proceed = st.columns([3, 2])
    with col_proceed:
        st.button("➡️ Proceed to Publication Figures", on_click=set_step, args=("13. Publication Figures",), use_container_width=True)

# ---------------------------------------------------------
# STEP 12: Results Dashboard
# ---------------------------------------------------------
# elif step == "12. Dashboard":
#     st.warning("⚠️ Dashboard is currently disabled.")
#     # dashboard.render_dashboard()
#     
#     st.divider()
#     _, col_proceed = st.columns([3, 2])
#     with col_proceed:
#         st.button("➡️ Proceed to Publication Figures", on_click=set_step, args=("13. Publication Figures",), use_container_width=True)

# ---------------------------------------------------------
# STEP 13: Publication Figures
# ---------------------------------------------------------
elif step == "13. Publication Figures":
    dashboard.render_publication_figures()
