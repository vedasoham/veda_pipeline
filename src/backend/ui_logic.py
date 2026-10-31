import streamlit as st
from pathlib import Path
import pandas as pd
import json
from . import parsers

def load_css(css_file: Path):
    """
    Loads a CSS file and injects it into the Streamlit app.
    """
    if not css_file.is_file():
        st.error(f"CSS file not found at {css_file}")
        return

    with open(css_file) as f:
        st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)

def render_netmhc_results(file_path: Path, title="🧬 Step 3: Epitope Discovery (NetMHCpan)"):
    """
    Renders the NetMHCpan results from the JSON file.
    """
    st.subheader(title)
    
    if not file_path.exists():
        st.warning("Result file not found.")
        return
            
        # Protection against Out-of-Memory (OOM) errors in the UI
        file_size_mb = file_path.stat().st_size / (1024 * 1024)
        if file_size_mb > 150:
            st.warning(f"⚠️ Result file is very large ({file_size_mb:.1f} MB). To prevent your server and browser from running out of memory, the interactive table is disabled.")
            st.info("Please use the Statistics Summary in the pipeline steps, or download the full results below.")
            with open(file_path, "rb") as f:
                st.download_button(
                    label="📥 Download Full Results (JSON)",
                    data=f,
                    key=f"dl_netmhc_large_{title.replace(' ', '_').lower()}",
                    file_name="netmhc_results.json",
                    mime="application/json"
                )
            return

    try:
        # Load JSON data
        with open(file_path, 'r') as f:
            data = json.load(f)
        df = pd.DataFrame(data)
        
        if 'rank' in df.columns:
            strong_binders = df[df['rank'] < 0.5]
            
            # 4. Display Metrics
            col1, col2 = st.columns(2)
            col1.metric("Peptides Scanned", len(df))
            col2.metric("Strong Binders Found", len(strong_binders))
            
            # 5. Show Table
            st.markdown("### 🧬 Detailed Results (All Peptides)")
            st.dataframe(df[['protein_id', 'peptide', 'allele', 'rank', 'affinity', 'level']].sort_values('rank'), width='stretch')
            
            # 6. Download Button
            st.download_button(
                label="📥 Download All Results (CSV)",
                data=df.to_csv(index=False).encode('utf-8'),
                key=f"dl_netmhc_{title.replace(' ', '_').lower()}",
                file_name="netmhc_results.csv",
                mime="text/csv"
            )
        else:
            st.error("Could not find 'rank' column. Check file format.")
            with st.expander("Debug Raw Data"):
                st.write(df.head())
            
    except Exception as e:
        st.warning(f"No results yet or file unreadable: {e}")

def render_gibbs_results(output_dir: Path):
    """
    Renders the GibbsCluster raw results table.
    """
    st.subheader("🧩 GibbsCluster Results")
    
    if not output_dir.exists():
        st.info("No results found.")
        return

    # GibbsCluster typically outputs detailed solution files ending in .ds.out
    # e.g., gibbs.1g.ds.out, gibbs.2g.ds.out
    files = sorted(list(output_dir.rglob("*.ds.out")))
    
    if not files:
        st.warning("No solution files (*.ds.out) found in output directory.")
        return
        
    # Allow user to select which cluster solution to view
    file_map = {f.name: f for f in files}
    selected_file_name = st.selectbox("Select Clustering Solution:", list(file_map.keys()))
    
    if selected_file_name:
        file_path = file_map[selected_file_name]
        
        try:
            # Attempt to read the file. GibbsCluster output is whitespace-separated.
            # We let pandas infer the columns first, then try to apply standard headers if they match count.
            df = pd.read_csv(file_path, sep=r'\s+', comment='#', header=None)
            
            # Standard columns based on GibbsCluster 2.0 documentation
            std_cols = ["Gn", "Num", "Sequence", "Core", "of", "IP", "IL", "DP", "DL", "Annotation", "Self", "bgG", "bgScore", "cScore"]
            
            # Adjust headers if column count matches (Annotation is optional)
            if len(df.columns) == len(std_cols):
                df.columns = std_cols
            elif len(df.columns) == len(std_cols) - 1:
                # Annotation likely missing
                cols_no_anno = [c for c in std_cols if c != "Annotation"]
                df.columns = cols_no_anno
            
            st.dataframe(df, width='stretch')
            
            st.download_button(
                label="📥 Download Table (CSV)",
                data=df.to_csv(index=False).encode('utf-8'),
                file_name=f"{selected_file_name}.csv",
                mime="text/csv"
            )
            
        except Exception as e:
            st.error(f"Error parsing file: {e}")
            with open(file_path, 'r') as f:
                st.text(f.read())

def render_vaxign_results(result_path: Path):
    """
    Renders Vaxign-ML results.
    """
    st.subheader("🦠 Vaxign-ML Antigenicity Results")
    
    if not result_path.exists():
        st.warning("Result file not found.")
        return
        
    df = parsers.parse_vaxign_results(result_path)
    
    if not df.empty:
        st.dataframe(df, width='stretch')
        
        st.download_button(
            label="📥 Download Vaxign Results (CSV)",
            data=df.to_csv(index=False).encode('utf-8'),
            file_name="vaxign_results.csv",
            mime="text/csv"
        )
    else:
        st.error("Failed to parse Vaxign results.")

def render_algpred_results(file_path: Path):
    """
    Renders AlgPred 2.0 results.
    """
    st.subheader("🛡️ AlgPred 2.0 Allergenicity Results")
    
    if not file_path.exists():
        st.warning("Result file not found.")
        return

    df = parsers.parse_algpred_results(file_path)
    
    if not df.empty:
        # Filter for Safe Candidates (Prediction == Non-Allergen)
        if 'Prediction' in df.columns:
            safe_candidates = df[df['Prediction'] == 'Non-Allergen']
            st.success(f"✅ Found {len(safe_candidates)} Non-Allergenic (Safe) Candidates out of {len(df)} total.")
            
            # Metrics
            col1, col2 = st.columns(2)
            col1.metric("Total Proteins", len(df))
            col2.metric("Safe Candidates", len(safe_candidates))
        
        st.dataframe(df, width='stretch')
        
        st.download_button(
            label="📥 Download AlgPred Results (CSV)",
            data=df.to_csv(index=False).encode('utf-8'),
            file_name="algpred_results.csv",
            mime="text/csv"
        )
    else:
        st.error("Failed to parse AlgPred results.")

def render_toxinpred_results(file_path: Path):
    """
    Renders ToxinPred 2.0 results.
    """
    st.subheader("☠️ ToxinPred 2.0 Toxicity Results")
    
    if not file_path.exists():
        st.warning("Result file not found.")
        return

    df = parsers.parse_toxinpred_results(file_path)
    
    if not df.empty:
        # Filter for Safe Candidates (Prediction == Non-Toxin)
        if 'Prediction' in df.columns:
            safe_candidates = df[df['Prediction'] == 'Non-Toxin']
            st.success(f"✅ Found {len(safe_candidates)} Non-Toxic (Safe) Candidates out of {len(df)} total.")
            
            col1, col2 = st.columns(2)
            col1.metric("Total Proteins", len(df))
            col2.metric("Safe Candidates", len(safe_candidates))
        
        st.dataframe(df, width='stretch')
        
        st.download_button(
            label="📥 Download ToxinPred Results (CSV)",
            data=df.to_csv(index=False).encode('utf-8'),
            file_name="toxinpred_results.csv",
            mime="text/csv"
        )
    else:
        st.error("Failed to parse ToxinPred results.")

def render_il4pred_results(file_path: Path):
    """
    Renders IL4Pred 2.0 results.
    """
    st.subheader("🧬 IL4Pred Cytokine Induction Results")
    
    if not file_path.exists():
        st.warning("Result file not found.")
        return

    df = parsers.parse_il4pred_results(file_path)
    
    if not df.empty:
        # Filter for Inducers
        if 'Prediction' in df.columns:
            inducers = df[df['Prediction'] == 'IL4-Inducer']
            st.success(f"✅ Found {len(inducers)} IL4-Inducing Candidates out of {len(df)} total.")
            
            col1, col2 = st.columns(2)
            col1.metric("Total Proteins", len(df))
            col2.metric("IL4 Inducers", len(inducers))
        
        st.dataframe(df, width='stretch')
        
        st.download_button(
            label="📥 Download IL4Pred Results (CSV)",
            data=df.to_csv(index=False).encode('utf-8'),
            file_name="il4pred_results.csv",
            mime="text/csv"
        )
    else:
        st.error("Failed to parse IL4Pred results.")

def render_ifnepitope_results(file_path: Path):
    """
    Renders IFNEpitope results.
    """
    st.subheader("🧬 IFNEpitope (IFN-γ) Results")
    
    if not file_path.exists():
        st.warning("Result file not found.")
        return

    df = parsers.parse_ifnepitope_results(file_path)
    
    if not df.empty:
        # Filter for Inducers (Prediction == IFN-γ inducer)
        if 'Prediction' in df.columns:
            inducers = df[df['Prediction'] == 'IFN-γ inducer']
            st.success(f"✅ Found {len(inducers)} IFN-γ Inducing Candidates out of {len(df)} total.")
            
            col1, col2 = st.columns(2)
            col1.metric("Total Peptides/Scans", len(df))
            col2.metric("IFN-γ Inducers", len(inducers))
        
        st.dataframe(df, width='stretch')
        
        st.download_button(
            label="📥 Download IFNEpitope Results (CSV)",
            data=df.to_csv(index=False).encode('utf-8'),
            file_name="ifnepitope_results.csv",
            mime="text/csv"
        )
    else:
        st.error("Failed to parse IFNEpitope results.")