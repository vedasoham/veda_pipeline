import subprocess
import os
import sys
from pathlib import Path
import datetime
import time

def run(fasta_path, output_dir, model="Fast", plot=False, log_file=None):
    def log(msg):
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{timestamp}] {msg}"
        print(entry)
        if log_file:
            with open(log_file, "a") as f:
                f.write(entry + "\n")

    start_time = time.time()
    log(f"⏳ Job Started: DeepLoc 2.0")
    log(f"⚙️ Worker: Running DeepLoc2 (Model: {model}, Plot: {plot})...")
    
    fasta_path = Path(fasta_path).resolve()
    output_dir = Path(output_dir).resolve()
    
    log(f"📄 Input File: {fasta_path}")
    log(f"📂 Output Directory: {output_dir}")
    log(f"📝 Log File: {log_file}")
    
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "deeploc2",
        "-f", str(fasta_path),
        "-o", str(output_dir),
        "-m", model
    ]
    if plot:
        cmd.append("-p")

    try:
        log(f"🚀 Executing: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)

        if result.stdout: log(f"STDOUT:\n{result.stdout.strip()}")
        if result.stderr: log(f"STDERR:\n{result.stderr.strip()}")
            
        if result.returncode == 0:
            elapsed = time.time() - start_time
            # Rename timestamped output to results.csv for consistent GUI access
            for f in output_dir.glob("results_*.csv"):
                f.rename(output_dir / "results.csv")
                log(f"📄 Renamed {f.name} -> results.csv")
            log(f"✅ DeepLoc2 finished. Output in {output_dir}. Elapsed: {elapsed:.2f}s")
            return True
        else:
            elapsed = time.time() - start_time
            log(f"❌ DeepLoc2 Failed (Exit Code {result.returncode}). Elapsed: {elapsed:.2f}s")
            return False

    except Exception as e:
        log(f"❌ Execution Error: {e}")
        return False