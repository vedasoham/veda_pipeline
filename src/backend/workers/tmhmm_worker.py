import argparse
import sys
import os
import json
import re
from pathlib import Path
import datetime
import time

def log(msg):
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def parse_gff(content):
    """Parses TMRs.gff3 content."""
    proteins = {}
    
    lines = content.splitlines()
    for line in lines:
        line = line.strip()
        if not line: 
            continue
            
        # Parse Comments for Metadata
        if line.startswith("#"):
            # Extract ID and Length
            len_match = re.search(r"#\s+(\w+)\s+Length:\s+(\d+)", line)
            if len_match:
                pid = len_match.group(1)
                if pid not in proteins: proteins[pid] = {"id": pid, "gff_entries": []}
                proteins[pid]["length"] = int(len_match.group(2))
            
            # Extract ID and Predicted TMRs count
            tmr_match = re.search(r"#\s+(\w+)\s+Number of predicted TMRs:\s+(\d+)", line)
            if tmr_match:
                pid = tmr_match.group(1)
                if pid not in proteins: proteins[pid] = {"id": pid, "gff_entries": []}
                proteins[pid]["num_tmrs"] = int(tmr_match.group(2))
            continue
            
        if line.startswith("//"):
            continue

        # Parse Data Lines
        parts = line.split('\t')
        if len(parts) >= 4:
            # Assuming format: ID, Feature, Start, End, ...
            entry = {
                "feature": parts[1],
                "start": int(parts[2]),
                "end": int(parts[3])
            }
            if parts[0] in proteins:
                proteins[parts[0]]["gff_entries"].append(entry)
            else:
                proteins[parts[0]] = {"id": parts[0], "gff_entries": [entry]}
                
    return proteins

def parse_3line(content):
    """Parses predicted_topologies.3line content."""
    data = {}
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith(">"):
            header_parts = line[1:].split('|')
            pid = header_parts[0].strip()
            pclass = header_parts[1].strip() if len(header_parts) > 1 else "UNKNOWN"
            
            seq = lines[i+1].strip()
            topo = lines[i+2].strip()
            
            data[pid] = {
                "class": pclass,
                "sequence": seq,
                "topology": topo
            }
            i += 3
        else:
            i += 1
    return data

def parse_md_summary(content):
    """Extracts the summary statistics from the markdown file."""
    summary = {"protein_types": {}, "region_types": {}}
    
    # Capture Protein types
    pt_section = re.search(r"Protein types:\n(.*?)\n\n", content, re.DOTALL)
    if pt_section:
        for match in re.finditer(r"(\w+\+?\w*):\s+(\d+)", pt_section.group(1)):
            summary["protein_types"][match.group(1)] = int(match.group(2))

    # Capture Region types
    rt_section = re.search(r"Region types:\n(.*?)(?:\n\n|\Z)", content, re.DOTALL)
    if rt_section:
         for match in re.finditer(r"(\w+):\s+(\d+)", rt_section.group(1)):
            summary["region_types"][match.group(1)] = int(match.group(2))
            
    return summary

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fasta", required=True)
    parser.add_argument("--outdir", required=True)
    args = parser.parse_args()

    start_time = time.time()
    # Import BioLib (inside the isolated environment)
    log("Worker started.")
    try:
        # Patch for Python 3.7 compatibility
        if sys.version_info < (3, 8):
            import typing
            try:
                from typing_extensions import TypedDict
                if not hasattr(typing, "TypedDict"):
                    typing.TypedDict = TypedDict
            except ImportError:
                pass

        import biolib
        log("✅ BioLib imported successfully.")
    except ImportError:
        log("❌ Worker Error: 'biolib' not installed in this environment.")
        sys.exit(1)

    log("⚙️ Loading DeepTMHMM tool (Cloud Mode)...")
    try:
        # Load the tool
        tool = biolib.load('DTU/DeepTMHMM:1.0.24')
        
        log(f"⚙️ Processing {Path(args.fasta).name}...")
        log("🚀 Submitting job to BioLib Cloud. This may take time depending on queue status...")
        log("ℹ️  If this step hangs for >10 minutes, the BioLib cloud queue might be busy.")

        # --- THE CRITICAL FIX IS HERE ---
        # 1. We ONLY send the input file. 
        # 2. We do NOT send '--outdir'. The cloud tool will just write to its own default folder.
        # Use blocking=True to avoid 404 errors on output download (race condition).
        job = tool.cli(args=f"--fasta {args.fasta}", blocking=True)

        log(f"🚀 Job finished (blocking mode).")

        # Check if the cloud job crashed
        if job.get_exit_code() != 0:
            log(f"🔍 DEBUG: Remote Job Exit Code: {job.get_exit_code()}")
            log("❌ Worker: Remote Job FAILED!")
            log("-" * 20 + " REMOTE STDERR " + "-" * 20)
            print(job.get_stderr().decode(), flush=True)
            log("-" * 20 + " REMOTE STDOUT " + "-" * 20)
            print(job.get_stdout().decode(), flush=True)
            log("-" * 50)
            sys.exit(1)
        else:
            log("✅ Remote Job Succeeded.")
            log("-" * 20 + " REMOTE STDERR " + "-" * 20)
            print(job.get_stderr().decode(), flush=True)
            log("-" * 20 + " REMOTE STDOUT " + "-" * 20)
            print(job.get_stdout().decode(), flush=True)
            log("-" * 50)

        # 3. Save Results
        # This grabs the files from the cloud and puts them in your local 'args.outdir'
        log(f"✅ Job finished. Saving results to {args.outdir}...")
        job.save_files(output_dir=args.outdir, overwrite=True)
        
        # 4. Parse to JSON and Cleanup
        try:
            outdir = Path(args.outdir)
            files_map = {
                "gff": outdir / "TMRs.gff3",
                "md": outdir / "deeptmhmm_results.md",
                "3line": outdir / "predicted_topologies.3line",
                "json": outdir / "result.json"
            }

            if all(f.exists() for k, f in files_map.items() if k != "json"):
                log("⚙️ Encoding results to JSON...")
                
                with open(files_map["3line"], 'r') as f:
                    three_line_data = parse_3line(f.read())
                with open(files_map["gff"], 'r') as f:
                    gff_data = parse_gff(f.read())
                with open(files_map["md"], 'r') as f:
                    summary_data = parse_md_summary(f.read())

                # Merge Data
                final_list = []
                for pid, info in three_line_data.items():
                    obj = info
                    obj['id'] = pid
                    if pid in gff_data:
                        obj['length'] = gff_data[pid].get('length', 0)
                        obj['num_tmrs'] = gff_data[pid].get('num_tmrs', 0)
                        obj['gff_entries'] = gff_data[pid].get('gff_entries', [])
                    final_list.append(obj)

                final_json = {
                    "summary": summary_data,
                    "proteins": final_list
                }

                with open(files_map["json"], 'w') as f:
                    json.dump(final_json, f, indent=2)
                log(f"✅ JSON saved to {files_map['json'].name}")

                log("⚙️ Removing raw files...")
                for k in ["gff", "md", "3line"]:
                    if files_map[k].exists():
                        files_map[k].unlink()
                log("✅ Cleanup complete.")
                
        except Exception as e:
            log(f"⚠️ Worker: Encoding/Cleanup failed: {e}")

        log("✅ Files saved successfully.")
        elapsed = time.time() - start_time
        log(f"⏱️ Elapsed Time: {elapsed:.2f} seconds")

    except Exception as e:
        e_str = str(e)
        if "compute_limit_exceeded" in e_str or "400 Error" in e_str:
            log("❌ BioLib Cloud Error: Server is busy (Compute Limit Exceeded).")
            log("ℹ️  This is a temporary BioLib issue. Please try again later.")
            sys.exit(1)
        log(f"❌ Worker Exception: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
