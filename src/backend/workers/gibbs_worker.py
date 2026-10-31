import subprocess
import os
from pathlib import Path
import sys
import config
import shutil
import datetime
import time

def log(msg, log_file=None):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] {msg}"
    print(entry, flush=True)
    if log_file:
        with open(log_file, "a") as f:
            f.write(entry + "\n")

def run(peptide_file, output_dir, clusters=5, motif_len=9, trash_cluster=True, jobs=1, log_file=None):
    """
    Runs GibbsCluster 2.0 locally.
    """
    start_time = time.time()
    log(f"⏳ Job Started: GibbsCluster", log_file)
    log(f"⚙️ Worker: Running GibbsCluster...", log_file)
    
    peptide_file = Path(peptide_file).resolve()
    output_dir = Path(output_dir).resolve()
    
    log(f"📄 Input File: {peptide_file}", log_file)
    log(f"📂 Output Directory: {output_dir}", log_file)
    log(f"📝 Log File: {log_file}", log_file)
    
    # Clean previous results to avoid mixing runs
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Prepare Environment
    # The tool requires the 'GIBBS' environment variable to find its libraries
    env = os.environ.copy()
    env["GIBBS"] = str(config.GIBBSCLUSTER_DIR)
    
    # Build Command
    # perl GibbsCluster-2.0e_SA.pl -f peptides.txt -g 1-5 -l 9 -R outdir
    cmd = [
        "perl",
        str(config.GIBBSCLUSTER_SCRIPT),
        "-f", str(peptide_file),
        "-g", f"1-{clusters}", # Range 1 to N
        "-l", str(motif_len),
        "-R", str(output_dir),
        "-k", str(jobs) # Multi-threading
    ]
    
    if trash_cluster:
        cmd.append("-T") # Use trash cluster to remove outliers
        
    try:
        log(f"🚀 Executing: {' '.join(cmd)}", log_file)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env
        )
        
        log("--- GibbsCluster STDOUT ---", log_file)
        log(result.stdout, log_file)
        log("--- GibbsCluster STDERR ---", log_file)
        log(result.stderr, log_file)
        
        if result.returncode == 0:
            end_time = time.time()
            elapsed = end_time - start_time
            log(f"✅ GibbsCluster Job Finished! Elapsed: {elapsed:.2f}s", log_file)
            return True
        else:
            end_time = time.time()
            elapsed = end_time - start_time
            log(f"❌ GibbsCluster Failed (Exit Code {result.returncode}). Elapsed: {elapsed:.2f}s", log_file)
            log(f"STDERR: {result.stderr}", log_file)
            return False
            
    except Exception as e:
        log(f"❌ Execution Error: {e}", log_file)
        return False