import subprocess
import os
from pathlib import Path
import datetime
import time
import shutil
import config
from backend.seq2logo_runner import run_seq2logo

def run(peptide_file, output_dir, clusters, motif_len, stop_file=None, log_file=None):
    
    # --- Cleanup and Setup ---
    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Re-initialize log file after cleanup
    if log_file:
        log_file = Path(log_file)
        log_file.touch()

    def log(msg):
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{timestamp}] {msg}"
        print(entry)
        if log_file:
            # Use line buffering to ensure logs are written immediately
            with open(log_file, "a", buffering=1) as f:
                f.write(entry + "\n")

    start_time = time.time()
    log(f"⏳ Job Started: GibbsCluster")
    log(f"⚙️ Worker: Running GibbsCluster (Clusters: {clusters}, Length: {motif_len})...")
    
    peptide_file = Path(peptide_file).resolve()
    
    log(f"📄 Input File: {peptide_file}")
    log(f"📂 Output Directory: {output_dir}")
    if log_file: log(f"📝 Log File: {log_file}")

    cmd = [
        "perl",
        str(config.GIBBSCLUSTER_SCRIPT),
        "-f", str(peptide_file),
        "-g", f"1-{clusters}",
        "-l", str(motif_len),
        "-R", str(output_dir),
        "-k", "4", # Number of seeds
        "-T"  # Trash removal
    ]

    # Prepare Environment
    # The tool requires the 'GIBBS' environment variable to find its libraries
    env = os.environ.copy()
    env["GIBBS"] = str(config.GIBBSCLUSTER_DIR)

    try:
        log(f"🚀 Executing: {' '.join(cmd)}")
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', env=env)

        # Stream stdout to the log file in real-time
        with open(log_file, "a", buffering=1) as f:
            for line in iter(process.stdout.readline, ''):
                f.write(line)
                # Check for stop signal
                if stop_file and Path(stop_file).exists():
                    log("🛑 Stop signal received. Terminating GibbsCluster process...")
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    log("🛑 Process terminated.")
                    return False
        
        retcode = process.wait()
        elapsed = time.time() - start_time
        
        if retcode == 0 and any(output_dir.rglob("*.ds.out")):
            log(f"✅ GibbsCluster finished. Output in {output_dir}. Elapsed: {elapsed:.2f}s")
            
            # Auto-run Seq2Logo integration
            log(f"\n🧬 Starting Seq2Logo integration...")
            seq2logo_result = run_seq2logo(str(output_dir), output_subdir="logos")
            
            if seq2logo_result.get("success"):
                log(f"✅ Seq2Logo completed successfully!")
                log(f"   Generated {seq2logo_result.get('total', 0)} logos")
                log(f"   Saved to: {seq2logo_result.get('output_dir')}")
            else:
                log(f"⚠️  Seq2Logo encountered issues: {seq2logo_result.get('error')}")
            
            return True
        else:
            log(f"❌ GibbsCluster Failed (Exit Code {retcode}). Check logs for details. Elapsed: {elapsed:.2f}s")
            return False

    except Exception as e:
        log(f"❌ Execution Error: {e}")
        return False