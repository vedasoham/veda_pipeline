import argparse
import biolib
import sys
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description="DeepTMHMM Sidecar Runner")
    parser.add_argument("--fasta", required=True, help="Path to input FASTA file")
    parser.add_argument("--outdir", required=True, help="Path to output directory")
    args = parser.parse_args()

    try:
        print(f"Loading DeepTMHMM from BioLib...")
        # Load the tool (this requires pybiolib to be installed in the env)
        tmhmm_tool = biolib.load('DTU/DeepTMHMM:1.0.24')
        
        print(f"Running DeepTMHMM on {args.fasta}...")
        # Run without '--local' to use cloud
        job = tmhmm_tool.cli(args=f"--fasta {args.fasta} --outdir {args.outdir}", blocking=True)
        
        print("Saving results...")
        job.save_files(output_dir=args.outdir)
        print("DeepTMHMM Sidecar finished successfully.")
    except Exception as e:
        print(f"Error in Sidecar: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()